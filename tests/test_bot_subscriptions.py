from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from telethon import types

import telegram_monitor.app as app
from telegram_monitor.event_search import (
    EventSearch,
    EventSearchAccessDenied,
    EventSearchError,
    EventSearchResult,
    TelegramEventSearch,
)
from telegram_monitor.models import ConfigurationError, MonitorConfig, QuietHoursConfig, SourceRule
from telegram_monitor.notifier import TelegramBotNotifier
from telegram_monitor.subscriber_store import SubscriberStore, event_reply_fingerprint


def _update(
    update_id: int,
    chat_id: int,
    text: str,
    *,
    chat_type: str = "private",
    sent_at: datetime | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id,
        "from": {
            "id": chat_id,
            "username": f"user{chat_id}",
            "first_name": f"User {chat_id}",
        },
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }
    if sent_at is not None:
        message["date"] = int(sent_at.timestamp())
    return {
        "update_id": update_id,
        "message": message,
    }


class FakeEventSearch:
    def __init__(
        self, *, count: int = 0, error: Exception | None = None, message_json: str = "{}"
    ) -> None:
        self.count = count
        self.error = error
        self.message_json = message_json
        self.calls: list[tuple[int, str, str]] = []
        self.exclusions: list[frozenset[str]] = []

    async def find_matches(
        self,
        requester_id: int,
        bot_username: str,
        location: str,
        excluded_replies: frozenset[str],
    ) -> EventSearchResult | None:
        self.calls.append((requester_id, bot_username, location))
        self.exclusions.append(excluded_replies)
        if self.error is not None:
            raise self.error
        if not self.count:
            return None
        return EventSearchResult(
            self.count,
            datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
            datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
            self.message_json,
        )


async def _bot_info_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/getMe"):
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 999, "username": "ExampleBot"}}
        )
    assert request.url.path.endswith("/sendMessage")
    return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.mark.asyncio
async def test_start_stop_and_limit_replies(caplog: pytest.LogCaptureFixture) -> None:
    replies: list[tuple[int, str]] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        replies.append((payload["chat_id"], payload["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:", subscriber_limit=2)
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start"))
    await notifier.process_update(_update(2, 102, "/start payload"))
    await notifier.process_update(_update(3, 101, "/start@ExampleBot"))
    await notifier.process_update(_update(4, 103, "/start"))

    assert set(store.list_chat_ids()) == {101, 102}
    assert replies[0] == (101, "✅ Ви підписалися на сповіщення.")
    assert replies[2] == (101, "✅ Ви вже підписані на сповіщення.")
    assert replies[3] == (
        103,
        "❌ Максимальна кількість користувачів перевищена (ліміт: 2).",
    )

    await notifier.process_update(_update(5, 101, "/stop"))
    await notifier.process_update(_update(6, 103, "/start"))
    assert set(store.list_chat_ids()) == {102, 103}
    assert replies[-2:] == [
        (101, "🔕 Ви відписалися від сповіщень."),
        (103, "✅ Ви підписалися на сповіщення."),
    ]

    new_user_logs = [record.getMessage() for record in caplog.records if "New user" in record.msg]
    removed_user_logs = [
        record.getMessage() for record in caplog.records if "Removed user" in record.msg
    ]
    assert len(new_user_logs) == 3
    assert "chat_id=101" in new_user_logs[0]
    assert "username=@user101" in new_user_logs[0]
    assert removed_user_logs == [
        "Removed user (chat_id=101, user_id=101, username=@user101, "
        "first_name=User 101, reason=/stop)"
    ]
    await notifier.close()


@pytest.mark.asyncio
async def test_non_commands_and_group_commands_are_ignored() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start", chat_type="group"))
    await notifier.process_update(_update(2, 101, "hello"))
    await notifier.process_update(_update(3, 101, "/events галицьке", chat_type="group"))

    assert calls == 0
    assert store.list_chat_ids() == ()
    await notifier.close()


@pytest.mark.asyncio
async def test_events_logs_location_and_username_or_user_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    search = FakeEventSearch(count=12)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(_bot_info_handler),
        event_search=search,
    )

    await notifier.process_update(_update(1, 101, "/events галицьке"))
    update = _update(2, 102, ' /events@ExampleBot  Нове Місто "центр"\nдорога ')
    message = update["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": 102}
    await notifier.process_update(update)
    update = _update(3, 103, "/events " + "а" * 150)
    message = update["message"]
    assert isinstance(message, dict)
    message["from"] = {"id": 103}
    await notifier.process_update(update)

    assert [record.getMessage() for record in caplog.records] == [
        'User("user101") requested events for location("галицьке"). Matches: 12',
        'User("102") requested events for location("Нове Місто \\"центр\\" дорога"). Matches: 12',
        f'User("103") requested events for location("{"а" * 150}"). Matches: 12',
    ]
    assert search.calls == [
        (101, "ExampleBot", "галицьке"),
        (102, "ExampleBot", 'Нове Місто "центр"\nдорога'),
        (103, "ExampleBot", "а" * 150),
    ]
    await notifier.close()


@pytest.mark.asyncio
async def test_events_without_location_replies_with_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies: list[tuple[int, str]] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        payload = json.loads(request.content)
        replies.append((payload["chat_id"], payload["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
    )
    await notifier.process_update(_update(1, 101, "/events"))
    await notifier.process_update(_update(2, 102, "/events \u200b"))

    assert replies == [
        (101, "Вкажіть місце, наприклад: /events галицьке"),
        (102, "Вкажіть місце, наприклад: /events галицьке"),
    ]
    assert not any("requested events" in record.getMessage() for record in caplog.records)
    await notifier.close()


@pytest.mark.asyncio
async def test_events_does_not_change_subscriptions(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        store=store,
        transport=httpx.MockTransport(_bot_info_handler),
        event_search=FakeEventSearch(),
    )

    await notifier.process_update(_update(1, 101, "/events галицьке"))

    assert store.list_chat_ids() == ()
    assert 'User("user101") requested events for location("галицьке"). Matches: 0' in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_events_sends_result_and_zero_matches_without_logging_message_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies: list[str] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        replies.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    search = FakeEventSearch(count=1, message_json='{\n  "message": "приватний текст"\n}')
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        event_search=search,
        timezone_name="Europe/Kyiv",
    )
    await notifier.process_update(_update(1, 101, "/events галицьке"))
    search.count = 0
    await notifier.process_update(_update(2, 101, "/events інше"))

    assert replies == [
        "Кількість співпадінь за період 2026-08-06 15:30:00 EEST – "
        '2026-08-07 15:30:00 EEST: 1. Last message:\n{\n  "message": "приватний текст"\n}',
        "Кількість співпадінь за останні 10 днів: 0. Last message: відсутнє",
    ]
    assert "приватний текст" not in caplog.text
    assert event_reply_fingerprint(replies[0]) in search.exclusions[1]
    await notifier.close()


@pytest.mark.asyncio
async def test_events_excludes_every_multipart_reply_after_restart(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    original = types.Message(
        id=1,
        peer_id=types.PeerUser(999),
        from_id=types.PeerUser(999),
        date=now - timedelta(hours=1),
        message="галицьке 😀 " * 1200,
    )
    history = [original]
    replies: list[str] = []

    class HistoryClient:
        async def get_me(self) -> SimpleNamespace:
            return SimpleNamespace(id=101)

        async def iter_messages(
            self, bot_username: str, *, limit: int | None, search: str
        ) -> AsyncIterator[types.Message]:
            assert bot_username == "ExampleBot"
            assert limit is None
            for message in history:
                if search in message.message:
                    yield message

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        text = json.loads(request.content)["text"]
        replies.append(text)
        history.insert(
            0,
            types.Message(
                id=len(replies) + 1,
                peer_id=types.PeerUser(999),
                from_id=types.PeerUser(999),
                date=now,
                message=text,
            ),
        )
        return httpx.Response(200, json={"ok": True, "result": {}})

    database = str(tmp_path / "subscribers.sqlite3")
    search = TelegramEventSearch(HistoryClient(), now=lambda: now)
    notifier = TelegramBotNotifier(
        "123:secret", database, transport=httpx.MockTransport(handler), event_search=search
    )
    await notifier.process_update(_update(1, 101, "/events галицьке"))
    first_parts = replies.copy()
    assert len(first_parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in first_parts)
    await notifier.close()

    notifier = TelegramBotNotifier(
        "123:secret", database, transport=httpx.MockTransport(handler), event_search=search
    )
    await notifier.process_update(_update(2, 101, "/events галицьке"))
    assert replies[len(first_parts) :] == first_parts
    await notifier.close()


@pytest.mark.asyncio
async def test_events_reports_partial_delivery_and_records_failed_part(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempted: list[str] = []
    caplog.set_level(logging.ERROR, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        attempted.append(json.loads(request.content)["text"])
        assert event_reply_fingerprint(attempted[-1]) in store.event_reply_fingerprints(101)
        if len(attempted) == 2:
            return httpx.Response(
                400, json={"ok": False, "error_code": 400, "description": "bad request"}
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        delivery_attempts=1,
        event_search=FakeEventSearch(count=1, message_json=json.dumps({"message": "a" * 10000})),
    )
    await notifier.process_update(_update(1, 101, "/events галицьке"))
    assert len(attempted) == 3
    assert attempted[-1] == "Не вдалося повністю надіслати результат пошуку. Спробуйте ще раз."
    assert {event_reply_fingerprint(text) for text in attempted} <= store.event_reply_fingerprints(
        101
    )
    assert "Bot command reply was not delivered" in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_events_does_not_send_if_reply_fingerprint_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        sent = True
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")

    def fail_recording(chat_id: int, text: str) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(store, "record_event_reply", fail_recording)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        event_search=FakeEventSearch(count=1),
    )
    with pytest.raises(OSError, match="storage unavailable"):
        await notifier.process_update(_update(1, 101, "/events галицьке"))
    assert not sent
    await notifier.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reply"),
    [
        (
            EventSearchAccessDenied(),
            "Пошук доступний лише власнику підключеного Telegram акаунта.",
        ),
        (EventSearchError("not available"), "Не вдалося виконати пошук. Спробуйте пізніше."),
    ],
)
async def test_events_search_failure_does_not_log_a_count(
    error: Exception,
    reply: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies: list[str] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        replies.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        event_search=FakeEventSearch(error=error),
    )
    await notifier.process_update(_update(1, 101, "/events галицьке"))

    assert replies == [reply]
    assert "Matches:" not in caplog.text
    assert "not available" not in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_events_rejects_missing_or_mismatched_sender() -> None:
    replies: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        replies.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    search = FakeEventSearch()
    notifier = TelegramBotNotifier(
        "123:secret", ":memory:", transport=httpx.MockTransport(handler), event_search=search
    )
    for sender in (None, {"id": 102}):
        update = _update(1, 101, "/events галицьке")
        message = update["message"]
        assert isinstance(message, dict)
        message["from"] = sender
        await notifier.process_update(update)

    assert search.calls == []
    assert replies == ["Не вдалося підтвердити ваш Telegram акаунт."] * 2
    await notifier.close()


@pytest.mark.asyncio
async def test_commands_with_quiet_hours_timestamps_are_processed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    replies: list[int] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        payload = json.loads(request.content)
        replies.append(payload["chat_id"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        event_search=FakeEventSearch(count=3),
    )

    await notifier.process_update(
        _update(1, 101, "/start", sent_at=datetime(2026, 8, 22, 23, 0, tzinfo=UTC))
    )
    await notifier.process_update(
        _update(2, 102, "/start", sent_at=datetime(2026, 8, 23, 4, 0, tzinfo=UTC))
    )
    await notifier.process_update(
        _update(3, 103, "/events галицьке", sent_at=datetime(2026, 8, 22, 23, 0, tzinfo=UTC))
    )
    await notifier.process_update(_update(4, 101, "/stop"))

    assert store.list_chat_ids() == (102,)
    assert replies == [101, 102, 103, 101]
    assert 'User("user103") requested events for location("галицьке"). Matches: 3' in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_events_search_continues_across_quiet_hours_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    started = asyncio.Event()
    finish = asyncio.Event()
    current = datetime(2026, 8, 22, 22, 29, tzinfo=UTC)

    class SlowSearch(FakeEventSearch):
        async def find_matches(
            self,
            requester_id: int,
            bot_username: str,
            location: str,
            excluded_replies: frozenset[str],
        ) -> EventSearchResult | None:
            started.set()
            await finish.wait()
            return await super().find_matches(
                requester_id, bot_username, location, excluded_replies
            )

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(_bot_info_handler),
        event_search=SlowSearch(count=4),
    )
    task = asyncio.create_task(
        notifier.process_update(_update(1, 101, "/events галицьке", sent_at=current))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    current = datetime(2026, 8, 22, 22, 30, tzinfo=UTC)
    finish.set()
    await asyncio.wait_for(task, timeout=1)

    assert 'User("user101") requested events for location("галицьке"). Matches: 4' in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_all_supported_commands_work_with_quiet_hours_timestamps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    current = datetime(2026, 8, 22, 23, 0, tzinfo=UTC)
    search = FakeEventSearch(count=2)
    replies: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getMe"):
            return await _bot_info_handler(request)
        replies.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        event_search=search,
    )
    await notifier.process_update(_update(1, 101, "/start", sent_at=current))
    await notifier.process_update(_update(2, 101, "/stop", sent_at=current))
    await notifier.process_update(_update(3, 101, "/events галицьке", sent_at=current))
    await notifier.process_update(_update(4, 101, "/events місто"))

    assert search.calls == [(101, "ExampleBot", "галицьке"), (101, "ExampleBot", "місто")]
    assert 'User("user101") requested events for location("галицьке"). Matches: 2' in caplog.text
    assert replies[:2] == ["✅ Ви підписалися на сповіщення.", "🔕 Ви відписалися від сповіщень."]
    assert len(replies) == 4
    assert all("Кількість співпадінь" in reply for reply in replies[2:])
    assert store.list_chat_ids() == ()
    await notifier.close()


@pytest.mark.asyncio
async def test_failed_command_reply_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error_code": 500, "description": "temporary outage"},
        )

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        delivery_attempts=1,
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start"))
    assert store.list_chat_ids() == (101,)
    await notifier.close()

    assert (
        "Could not send Bot API /start command reply to chat_id=101 after 1 attempt(s) "
        "(error_code=500, retryable=True, retry_after=None)" in caplog.text
    )
    assert (
        "Bot command reply was not delivered (command=/start, chat_id=101); "
        "no further automatic retry is scheduled" in caplog.text
    )
    assert "✅ Ви підписалися на сповіщення." not in caplog.text
    assert "123:secret" not in caplog.text


@pytest.mark.asyncio
async def test_poller_processes_command_and_persists_next_offset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    second_poll_started = asyncio.Event()
    keep_polling = asyncio.Event()
    get_updates_payloads: list[Mapping[str, object]] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 999, "username": "ExampleBot"}}
            )
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {}})
        assert method == "getUpdates"
        get_updates_payloads.append(payload)
        if len(get_updates_payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        _update(42, 101, "/start"),
                        _update(43, 101, "/events галицьке"),
                    ],
                },
            )
        second_poll_started.set()
        await keep_polling.wait()
        return httpx.Response(200, json={"ok": True, "result": []})

    store = SubscriberStore(":memory:")
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        event_search=FakeEventSearch(count=3),
    )

    await notifier.start()
    await asyncio.wait_for(second_poll_started.wait(), timeout=1)

    assert store.list_chat_ids() == (101,)
    assert store.get_next_update_offset() == 44
    assert "offset" not in get_updates_payloads[0]
    assert get_updates_payloads[1]["offset"] == 44
    assert 'User("user101") requested events for location("галицьке"). Matches: 3' in caplog.text
    assert (
        "Bot command polling started; Accepting /start, /stop and /events "
        "(subscriber limit: 10); Current subscribers: 1" in caplog.text
    )
    assert "Bot subscribers:" not in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_poller_processes_all_commands_with_quiet_hours_enabled(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    current = datetime(2026, 8, 22, 23, 0, tzinfo=UTC)
    next_poll = asyncio.Event()
    methods: list[str] = []
    replies: list[str] = []
    search = FakeEventSearch(count=5)

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getMe":
            return await _bot_info_handler(request)
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "sendMessage":
            replies.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True, "result": {}})
        assert method == "getUpdates"
        if methods.count("getUpdates") == 1:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        _update(10, 101, "/start", sent_at=current),
                        _update(11, 101, "/events галицьке", sent_at=current),
                        _update(12, 101, "/stop", sent_at=current),
                    ],
                },
            )
        next_poll.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    store = SubscriberStore(":memory:")
    config = MonitorConfig(
        notification_mode="bot",
        sources=(SourceRule(peer="@source", keywords=("road",)),),
        quiet_hours=QuietHoursConfig(enabled=True),
    )

    class Client:
        async def disconnect(self) -> None:
            return None

    def create_notifier(
        database_path: str,
        *,
        subscriber_limit: int,
        delivery_attempts: int,
        delivery_retry_base_seconds: float,
        delivery_retry_max_seconds: float,
        on_polling_fatal: Callable[[], Awaitable[object]] | None,
        event_search: EventSearch,
        timezone_name: str,
    ) -> TelegramBotNotifier:
        return TelegramBotNotifier(
            "123:secret",
            database_path,
            transport=httpx.MockTransport(handler),
            store=store,
            subscriber_limit=subscriber_limit,
            delivery_attempts=delivery_attempts,
            delivery_retry_base_seconds=delivery_retry_base_seconds,
            delivery_retry_max_seconds=delivery_retry_max_seconds,
            on_polling_fatal=on_polling_fatal,
            event_search=search,
            timezone_name=timezone_name,
        )

    monkeypatch.setattr(TelegramBotNotifier, "from_environment", create_notifier)
    notifier = app.build_notifier(Client(), config)
    await notifier.start()
    await asyncio.wait_for(next_poll.wait(), timeout=1)

    assert store.list_chat_ids() == ()
    assert store.get_next_update_offset() == 13
    assert search.calls == [(101, "ExampleBot", "галицьке")]
    assert 'User("user101") requested events for location("галицьке"). Matches: 5' in caplog.text
    assert replies[0] == "✅ Ви підписалися на сповіщення."
    assert "Кількість співпадінь" in replies[1]
    assert replies[2] == "🔕 Ви відписалися від сповіщень."
    await notifier.close()


@pytest.mark.asyncio
async def test_active_webhook_prevents_long_polling() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999}})
        assert method == "getWebhookInfo"
        return httpx.Response(
            200,
            json={"ok": True, "result": {"url": "https://example.test/bot-hook"}},
        )

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ConfigurationError, match="active webhook"):
        await notifier.start()
    await notifier.close()

    assert methods == ["getMe", "getWebhookInfo"]


@pytest.mark.asyncio
async def test_competing_get_updates_consumer_fails_startup() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        assert method == "getUpdates"
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request",
            },
        )

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ConfigurationError, match="polling stopped permanently"):
        await notifier.start()
    await notifier.close()

    assert methods == ["getMe", "getWebhookInfo", "getUpdates"]
