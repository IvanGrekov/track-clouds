from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.types import PeerChannel, User

from telegram_monitor.models import MonitorConfig, SourceRule
from telegram_monitor.notifier import NotificationError
from telegram_monitor.service import TelegramMonitor


class FakeClient:
    def __init__(self, dialogs: list[object]) -> None:
        self.dialogs = dialogs
        self.handler = None
        self.builder = None
        self.removed = False

    async def get_dialogs(self) -> list[object]:
        return self.dialogs

    def add_event_handler(self, handler: object, builder: object) -> None:
        self.handler = handler
        self.builder = builder

    def remove_event_handler(self, handler: object, builder: object) -> None:
        assert handler == self.handler
        assert builder == self.builder
        self.removed = True


class FakeNotifier:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0
        self.sent: list[str] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def send(self, text: str) -> None:
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary delivery failure")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


class FakeEvent:
    def __init__(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        media: bool = False,
        outgoing: bool = False,
        forwarded_from_channel_id: int | None = None,
        post_author: str | None = None,
        sender_id: int = 7,
    ) -> None:
        self.chat_id = chat_id
        self.id = message_id
        self.raw_text = text
        self.out = outgoing
        self.sender = User(id=7, first_name="Олена")
        self.sender_id = sender_id
        self.message = SimpleNamespace(
            media=object() if media else None,
            post_author=post_author,
            fwd_from=(
                SimpleNamespace(from_id=PeerChannel(forwarded_from_channel_id))
                if forwarded_from_channel_id is not None
                else None
            ),
        )
        self.date = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)


def _dialog(peer_id: int, username: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=peer_id,
        name=name,
        entity=SimpleNamespace(username=username),
    )


def _config() -> MonitorConfig:
    return MonitorConfig(
        sources=(
            SourceRule(peer="@discussion", keywords=("ваканс", "k8s")),
            SourceRule(peer="@announcements", notify_all=True),
        ),
        timezone="UTC",
    )


@pytest.mark.asyncio
async def test_event_flow_filters_enqueues_notifies_and_deduplicates() -> None:
    discussion_id = -1001111111111
    announcements_id = -1002222222222
    client = FakeClient(
        [
            _dialog(discussion_id, "discussion", "Discussion"),
            _dialog(announcements_id, "announcements", "Announcements"),
        ]
    )
    notifier = FakeNotifier()
    monitor = TelegramMonitor(client, _config(), notifier)

    descriptions = await monitor.prepare()
    assert descriptions == (
        "Discussion (-1001111111111)",
        "Announcements (-1002222222222)",
    )
    assert client.handler is not None
    assert client.builder is not None
    assert client.builder.incoming is True
    assert client.builder.outgoing is False
    assert notifier.started is True

    await monitor.handle_event(FakeEvent(discussion_id, 1, "звичайний spam"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "Нова ВАКАНСІЯ для K8S"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "Нова ВАКАНСІЯ для K8S"))
    await monitor.handle_event(FakeEvent(announcements_id, 3, "", media=True))
    await monitor.handle_event(FakeEvent(-1009999999999, 4, "k8s"))
    await monitor.handle_event(FakeEvent(discussion_id, 5, "k8s", outgoing=True))
    await monitor.handle_event(FakeEvent(discussion_id, 6, "Нова вакансія для k8s?  \n"))
    await monitor.handle_event(FakeEvent(announcements_id, 7, "Що нового?"))
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert "Matches: ваканс, k8s" in notifier.sent[0]
    assert "Нова ВАКАНСІЯ для K8S" in notifier.sent[0]

    await monitor.close()
    assert client.removed is True
    assert notifier.closed is True


@pytest.mark.asyncio
async def test_notification_failure_does_not_stop_worker() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        delivery_attempts=2,
        delivery_retry_base_seconds=0,
        delivery_retry_max_seconds=0,
    )
    notifier = FakeNotifier(failures=1)
    monitor = TelegramMonitor(client, config, notifier)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 1, "k8s release one"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "k8s release two"))
    await monitor._queue.join()

    assert notifier.calls == 3
    assert len(notifier.sent) == 2
    assert "k8s release one" in notifier.sent[0]
    assert "k8s release two" in notifier.sent[1]

    await monitor.close()
    await monitor.close()


@pytest.mark.asyncio
async def test_permanent_notification_error_is_not_retried() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        delivery_attempts=5,
        delivery_retry_base_seconds=0,
        delivery_retry_max_seconds=0,
    )

    class PermanentFailureNotifier(FakeNotifier):
        async def send(self, text: str) -> None:
            self.calls += 1
            raise NotificationError("bot blocked", retryable=False)

    notifier = PermanentFailureNotifier()
    monitor = TelegramMonitor(client, config, notifier)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 1, "k8s release"))
    await monitor._queue.join()

    assert notifier.calls == 1
    await monitor.close()


@pytest.mark.asyncio
async def test_capture_buffers_matching_event_while_dialogs_are_resolving() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
    )
    notifier = FakeNotifier()
    monitor = TelegramMonitor(client, config, notifier)

    monitor.start_capture()
    await monitor.handle_event(FakeEvent(discussion_id, 1, "k8s during startup"))
    assert notifier.sent == []

    await monitor.prepare()
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert "k8s during startup" in notifier.sent[0]
    await monitor.close()


@pytest.mark.asyncio
async def test_bounded_queue_drops_overflow_with_observable_counter() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        queue_capacity=1,
    )
    notifier = FakeNotifier()
    monitor = TelegramMonitor(client, config, notifier)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 1, "k8s first release"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "k8s overflow"))

    assert monitor.dropped_notifications == 1
    await monitor.close()
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_skips_automatic_discussion_copy_but_keeps_manual_user_forward() -> None:
    discussion_id = -1001111111111
    channel_id = -1002222222222
    client = FakeClient(
        [
            _dialog(discussion_id, "discussion", "Discussion"),
            _dialog(channel_id, "announcements", "Announcements"),
        ]
    )
    notifier = FakeNotifier()
    monitor = TelegramMonitor(client, _config(), notifier)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(channel_id, 10, "k8s release", post_author="Editor"))
    await monitor.handle_event(
        FakeEvent(
            discussion_id,
            11,
            "k8s release",
            forwarded_from_channel_id=2_222_222_222,
            sender_id=channel_id,
        )
    )
    await monitor.handle_event(
        FakeEvent(
            discussion_id,
            12,
            "user manually forwarded k8s release",
            forwarded_from_channel_id=2_222_222_222,
        )
    )
    await monitor._queue.join()

    assert len(notifier.sent) == 2
    assert "Source: Announcements" in notifier.sent[0]
    assert "k8s release" in notifier.sent[0]
    assert "user manually forwarded k8s release" in notifier.sent[1]
    await monitor.close()


@pytest.mark.asyncio
async def test_logs_message_content_only_after_keyword_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    notifier = FakeNotifier()
    config = MonitorConfig(
        sources=(
            SourceRule(
                peer="@discussion",
                keywords=("k8s",),
                keywords_to_skip=("spam",),
            ),
        ),
        timezone="UTC",
    )
    monitor = TelegramMonitor(client, config, notifier)
    caplog.set_level(logging.INFO, logger="telegram_monitor.service")
    await monitor.prepare()

    unsafe_text = "ordinary spam\nsecond line\x1b[31m\u202e"
    await monitor.handle_event(FakeEvent(discussion_id, 1, unsafe_text))
    await monitor.handle_event(FakeEvent(discussion_id, 1, unsafe_text))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "k8s spam advertisement"))
    await monitor.handle_event(FakeEvent(discussion_id, 3, "k8s"))
    await monitor.handle_event(FakeEvent(discussion_id, 4, "k8s release"))
    await monitor.handle_event(FakeEvent(-1009999999999, 5, "k8s unrelated source"))
    await monitor.handle_event(FakeEvent(discussion_id, 6, "k8s outgoing", outgoing=True))
    await monitor._queue.join()

    decision_logs = [
        record.getMessage()
        for record in caplog.records
        if "Match new message" in record.msg or "Skip new message" in record.msg
    ]
    assert decision_logs == [
        "Skip new message - 2026-08-06T12:30:00+00:00",
        "Skip new message - 2026-08-06T12:30:00+00:00",
        "Skip new message - 2026-08-06T12:30:00+00:00",
        "Match new message - 2026-08-06T12:30:00+00:00: k8s release",
    ]
    assert "ordinary spam" not in caplog.text
    assert "second line" not in caplog.text
    assert "k8s spam advertisement" not in caplog.text
    assert "\x1b" not in caplog.text
    assert "\u202e" not in caplog.text
    assert len(notifier.sent) == 1
    await monitor.close()


@pytest.mark.asyncio
async def test_bot_mode_filters_and_delivers_own_outgoing_message_once() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        notification_mode="bot",
        timezone="UTC",
    )
    notifier = FakeNotifier()
    monitor = TelegramMonitor(client, config, notifier)
    await monitor.prepare()

    assert client.builder.incoming is None
    assert client.builder.outgoing is None

    await monitor.handle_event(FakeEvent(discussion_id, 10, "my own k8s post", outgoing=True))
    await monitor.handle_event(FakeEvent(discussion_id, 10, "my own k8s post", outgoing=True))
    await monitor.handle_event(FakeEvent(discussion_id, 11, "my ordinary post", outgoing=True))
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert "my own k8s post" in notifier.sent[0]
    await monitor.close()
