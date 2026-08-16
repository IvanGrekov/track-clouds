from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.types import PeerChannel, User

import telegram_monitor.service as service_module
from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
    AITemporalRelevance,
)
from telegram_monitor.ai_observer import AIObservationReport
from telegram_monitor.models import AIObservationConfig, MessageSnapshot, MonitorConfig, SourceRule
from telegram_monitor.notifier import NotificationError
from telegram_monitor.openai_client import AIObservationTokenUsage
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
        self.attempted: list[str] = []
        self.sent: list[str] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def send(self, text: str) -> None:
        self.calls += 1
        self.attempted.append(text)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary delivery failure")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


class FakeObserver:
    def __init__(self, outcomes: list[AIObservationReport | Exception]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[MessageSnapshot, str | None]] = []
        self.closed = False

    async def observe(
        self,
        snapshot: MessageSnapshot,
        *,
        trusted_area_context: str | None,
    ) -> AIObservationReport:
        self.calls.append((snapshot, trusted_area_context))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

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


def _success_report(*, token_usage: AIObservationTokenUsage | None = None) -> AIObservationReport:
    return AIObservationReport(
        result=AIObservationResult(
            decision=AIDecision.ACCEPT,
            location="Городоцька, біля цирку",
            event="перекрита права смуга",
            temporal_relevance=AITemporalRelevance.CURRENT,
            reason_code=AIReasonCode.MEETS_ALL_CRITERIA,
            reason="Є актуальне обмеження руху та достатньо конкретна локація.",
        ),
        status=None,
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="abc123",
        elapsed_seconds=0.842,
        api_latency_seconds=0.7,
        attempts=1,
        token_usage=token_usage,
    )


def _semantic_report(result: AIObservationResult) -> AIObservationReport:
    return AIObservationReport(
        result=result,
        status=None,
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="abc123",
        elapsed_seconds=0.842,
        api_latency_seconds=0.7,
        attempts=1,
        token_usage=None,
    )


def _failure_report(
    status: AIObservationTechnicalStatus,
    *,
    response_text: str | None = None,
) -> AIObservationReport:
    return AIObservationReport(
        result=None,
        status=status,
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="abc123",
        elapsed_seconds=30.0,
        api_latency_seconds=None,
        attempts=1,
        token_usage=None,
        response_text=response_text,
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
async def test_validation_sanitizes_but_alert_preserves_original_text() -> None:
    discussion_id = -1001111111111
    original_text = "k8s release) 🚀"
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    notifier = FakeNotifier()
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
    )
    monitor = TelegramMonitor(client, config, notifier)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 1, "k8s done? 🙂)"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "k8s))))))))🙂"))
    await monitor.handle_event(FakeEvent(discussion_id, 3, original_text))
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert original_text in notifier.sent[0]
    await monitor.close()


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


@pytest.mark.asyncio
async def test_observes_once_and_reuses_rendered_alert_for_delivery_retry() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(
            SourceRule(
                peer="@discussion",
                keywords=("k8s",),
                trusted_area_context="Львів",
            ),
        ),
        timezone="UTC",
        delivery_attempts=2,
        delivery_retry_base_seconds=0,
        delivery_retry_max_seconds=0,
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier(failures=1)
    observer = FakeObserver([_success_report()])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()

    event = FakeEvent(discussion_id, 50, "k8s перекрита права смуга")
    await monitor.handle_event(event)
    await monitor.handle_event(event)
    await monitor._queue.join()

    assert len(observer.calls) == 1
    assert observer.calls[0][1] == "Львів"
    assert notifier.calls == 2
    assert notifier.attempted == [notifier.sent[0], notifier.sent[0]]
    assert len(notifier.sent) == 1
    assert "AI review:" in notifier.sent[0]
    assert "Source: Discussion\nTime:" in notifier.sent[0]
    assert "Decision: accept" in notifier.sent[0]
    assert "Model:" not in notifier.sent[0]
    assert "Policy:" not in notifier.sent[0]
    assert "Tokens:" not in notifier.sent[0]

    await monitor.close()
    assert observer.closed is True


@pytest.mark.asyncio
async def test_failed_delivery_does_not_allow_duplicate_to_repeat_observation() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        delivery_attempts=1,
        ai_observation=AIObservationConfig(enabled=True),
    )

    class AlwaysFailingNotifier(FakeNotifier):
        async def send(self, text: str) -> None:
            self.calls += 1
            raise NotificationError("bot blocked", retryable=False)

    notifier = AlwaysFailingNotifier()
    observer = FakeObserver([_success_report()])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()
    event = FakeEvent(discussion_id, 51, "k8s перекрита права смуга")

    await monitor.handle_event(event)
    await monitor._queue.join()
    await monitor.handle_event(event)
    await monitor._queue.join()

    assert len(observer.calls) == 1
    assert notifier.calls == 1
    await monitor.close()


@pytest.mark.asyncio
async def test_technical_observation_is_delivered_and_next_message_is_processed() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier()
    observer = FakeObserver(
        [
            _failure_report(AIObservationTechnicalStatus.TIMEOUT),
            _success_report(),
        ]
    )
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 52, "k8s first message"))
    await monitor.handle_event(FakeEvent(discussion_id, 53, "k8s second message"))
    await monitor._queue.join()

    assert len(observer.calls) == 2
    assert len(notifier.sent) == 2
    assert "Status: timeout" in notifier.sent[0]
    assert "Description:" in notifier.sent[0]
    assert "Decision:" not in notifier.sent[0]
    assert "Decision: accept" in notifier.sent[1]
    await monitor.close()


@pytest.mark.asyncio
async def test_invalid_ai_response_is_logged_but_not_added_to_telegram(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    response_text = '{"decision":"accept",\n"unexpected":"field"}'
    notifier = FakeNotifier()
    observer = FakeObserver(
        [
            _failure_report(
                AIObservationTechnicalStatus.INVALID_RESPONSE,
                response_text=response_text,
            )
        ]
    )
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    caplog.set_level(logging.ERROR, logger="telegram_monitor.service")
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 58, "k8s message"))
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert "Status: invalid_response" in notifier.sent[0]
    assert "unexpected" not in notifier.sent[0]
    assert 'ai_response={"decision":"accept", "unexpected":"field"}' in caplog.text
    await monitor.close()


@pytest.mark.asyncio
async def test_reject_and_review_observations_do_not_change_delivery() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    reject = AIObservationResult(
        decision=AIDecision.REJECT,
        location=None,
        event="особиста думка",
        temporal_relevance=AITemporalRelevance.CURRENT,
        reason_code=AIReasonCode.ONLY_OPINION_OR_EMOTION,
        reason="Немає корисного фактичного повідомлення про маршрут.",
    )
    review = AIObservationResult(
        decision=AIDecision.REVIEW,
        location="Стрийська",
        event="можлива перешкода",
        temporal_relevance=AITemporalRelevance.UNCLEAR,
        reason_code=AIReasonCode.AMBIGUOUS_RECENCY,
        reason="Часова актуальність повідомлення неоднозначна.",
    )
    notifier = FakeNotifier()
    observer = FakeObserver([_semantic_report(reject), _semantic_report(review)])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 56, "k8s перше повідомлення"))
    await monitor.handle_event(FakeEvent(discussion_id, 57, "k8s друге повідомлення"))
    await monitor._queue.join()

    assert len(observer.calls) == 2
    assert len(notifier.sent) == 2
    assert "Decision: reject" in notifier.sent[0]
    assert "Decision: review" in notifier.sent[1]
    await monitor.close()


@pytest.mark.asyncio
async def test_unexpected_observer_error_becomes_safe_api_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier()
    secret_marker = "raw-sdk-secret-marker"
    observer = FakeObserver([RuntimeError(secret_marker)])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    caplog.set_level(logging.ERROR, logger="telegram_monitor.service")
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 54, "k8s message"))
    await monitor._queue.join()

    assert len(notifier.sent) == 1
    assert "Status: api_error" in notifier.sent[0]
    assert secret_marker not in notifier.sent[0]
    assert secret_marker not in caplog.text
    assert "AI observation failed" in caplog.text
    await monitor.close()


@pytest.mark.asyncio
async def test_success_log_preserves_token_usage_without_adding_it_to_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier()
    observer = FakeObserver([_success_report(token_usage=AIObservationTokenUsage(321, 45, 366))])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    caplog.set_level(logging.INFO, logger="telegram_monitor.service")
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 55, "k8s message with usage"))
    await monitor._queue.join()

    assert "input_tokens=321, output_tokens=45, total_tokens=366" in caplog.text
    assert "elapsed_seconds=0.842" in caplog.text
    assert "Tokens:" not in notifier.sent[0]
    await monitor.close()


@pytest.mark.asyncio
async def test_observer_runs_only_after_all_deterministic_checks() -> None:
    discussion_id = -1001111111111
    channel_id = -1002222222222
    client = FakeClient(
        [
            _dialog(discussion_id, "discussion", "Discussion"),
            _dialog(channel_id, "announcements", "Announcements"),
        ]
    )
    config = MonitorConfig(
        sources=(
            SourceRule(
                peer="@discussion",
                keywords=("k8s",),
                keywords_to_skip=("spam",),
            ),
            SourceRule(peer="@announcements", keywords=("k8s",)),
        ),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier()
    observer = FakeObserver([_success_report()])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(-1009999999999, 1, "k8s unknown source"))
    await monitor.handle_event(FakeEvent(discussion_id, 2, "k8s"))
    await monitor.handle_event(FakeEvent(discussion_id, 3, "k8s enough text?"))
    await monitor.handle_event(FakeEvent(discussion_id, 4, "k8s spam advertisement"))
    await monitor.handle_event(
        FakeEvent(
            discussion_id,
            5,
            "k8s automatic forward",
            forwarded_from_channel_id=2_222_222_222,
            sender_id=channel_id,
        )
    )
    valid_event = FakeEvent(discussion_id, 6, "k8s valid road update")
    await monitor.handle_event(valid_event)
    await monitor.handle_event(valid_event)
    await monitor._queue.join()

    assert len(observer.calls) == 1
    assert len(notifier.sent) == 1
    assert "k8s valid road update" in notifier.sent[0]
    await monitor.close()


@pytest.mark.asyncio
async def test_queue_overflow_does_not_start_observation_for_dropped_job() -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        queue_capacity=1,
        ai_observation=AIObservationConfig(enabled=True),
    )
    notifier = FakeNotifier()
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingObserver:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def observe(
            self,
            snapshot: MessageSnapshot,
            *,
            trusted_area_context: str | None,
        ) -> AIObservationReport:
            del trusted_area_context
            self.calls.append(snapshot.message_id)
            if len(self.calls) == 1:
                first_started.set()
                await release_first.wait()
            return _success_report()

        async def close(self) -> None:
            return None

    observer = BlockingObserver()
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()

    await monitor.handle_event(FakeEvent(discussion_id, 61, "k8s first queued update"))
    await first_started.wait()
    await monitor.handle_event(FakeEvent(discussion_id, 62, "k8s second queued update"))
    await monitor.handle_event(FakeEvent(discussion_id, 63, "k8s dropped queued update"))
    release_first.set()
    await monitor._queue.join()

    assert observer.calls == [61, 62]
    assert monitor.dropped_notifications == 1
    assert len(notifier.sent) == 2
    await monitor.close()


@pytest.mark.asyncio
async def test_shutdown_wait_exceeds_one_complete_ai_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discussion_id = -1001111111111
    client = FakeClient([_dialog(discussion_id, "discussion", "Discussion")])
    config = MonitorConfig(
        sources=(SourceRule(peer="@discussion", keywords=("k8s",)),),
        timezone="UTC",
        ai_observation=AIObservationConfig(enabled=True, operation_timeout_seconds=6),
    )
    notifier = FakeNotifier()
    observer = FakeObserver([_failure_report(AIObservationTechnicalStatus.TIMEOUT)])
    monitor = TelegramMonitor(client, config, notifier, ai_observer=observer)
    await monitor.prepare()
    await monitor.handle_event(FakeEvent(discussion_id, 64, "k8s pending timeout"))

    observed_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def capture_wait_for(awaitable: object, timeout: float | None) -> object:
        observed_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module.asyncio, "wait_for", capture_wait_for)
    await monitor.close()

    assert observed_timeouts == [11.0]
    assert len(notifier.sent) == 1
    assert "Status: timeout" in notifier.sent[0]
