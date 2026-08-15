from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
    AITemporalRelevance,
)
from telegram_monitor.ai_observer import (
    AIObservationReport,
    OpenAIMessageObserver,
    UnavailableAIObserver,
    build_ai_observer,
)
from telegram_monitor.models import AIObservationConfig, ConfigurationError, MessageSnapshot
from telegram_monitor.openai_client import (
    AIObservationFailure,
    AIObservationRequest,
    AIObservationSuccess,
    AIObservationTokenUsage,
    OpenAIObservationClient,
)


def _config(**overrides: object) -> AIObservationConfig:
    values: dict[str, object] = {
        "enabled": True,
        "operation_timeout_seconds": 30,
        "request_attempts": 2,
        "retry_base_seconds": 0,
        "retry_max_seconds": 0,
    }
    values.update(overrides)
    return AIObservationConfig(**values)  # type: ignore[arg-type]


def _snapshot(*, text: str = "На Городоцькій зараз перекрита смуга") -> MessageSnapshot:
    return MessageSnapshot(
        source_title="Source",
        sender_name="Sender",
        text=text,
        message_id=456,
        peer_id=-100123,
        date=datetime(2026, 8, 10, 9, 55, 20, tzinfo=UTC),
        matched_keywords=("перекри",),
        notify_all=False,
    )


def _result() -> AIObservationResult:
    return AIObservationResult(
        decision=AIDecision.ACCEPT,
        confidence=0.96,
        location="Городоцька",
        event="перекрита смуга",
        temporal_relevance=AITemporalRelevance.CURRENT,
        reason_code=AIReasonCode.MEETS_ALL_CRITERIA,
        reason="Є актуальна подія та конкретна локація.",
    )


def _success() -> AIObservationSuccess:
    return AIObservationSuccess(
        result=_result(),
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="a" * 64,
        api_latency_seconds=0.7,
        attempts=1,
        token_usage=AIObservationTokenUsage(300, 50, 350),
    )


class StubClient(OpenAIObservationClient):
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[AIObservationRequest] = []
        self.timeouts: list[float] = []
        self.close_calls = 0

    async def classify(
        self,
        request: AIObservationRequest,
        *,
        timeout_seconds: float,
    ) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            value = outcome()
            if asyncio.iscoroutine(value):
                return await value
            return value
        return outcome

    async def close(self) -> None:
        self.close_calls += 1


class ReplyMessage:
    def __init__(self, *, reply_to_msg_id: int | None, outcome: object = None) -> None:
        self.reply_to_msg_id = reply_to_msg_id
        self.outcome = outcome
        self.calls = 0

    async def get_reply_message(self) -> object:
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            value = self.outcome()
            if asyncio.iscoroutine(value):
                return await value
            return value
        return self.outcome


def _observer(
    client: StubClient,
    *,
    config: AIObservationConfig | None = None,
    monotonic: Any = None,
    now: Any = None,
) -> OpenAIMessageObserver:
    kwargs: dict[str, object] = {
        "config": config or _config(),
        "client": client,
    }
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if now is not None:
        kwargs["now"] = now
    return OpenAIMessageObserver(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_observe_without_reply_builds_request_at_call_time() -> None:
    client = StubClient([_success()])
    now = datetime(2026, 8, 10, 9, 56, 5, tzinfo=UTC)
    observer = _observer(client, now=lambda: now)
    telegram_message = ReplyMessage(reply_to_msg_id=None)

    report = await observer.observe(
        _snapshot(),
        telegram_message=telegram_message,
        trusted_area_context="Львів",
    )

    assert report.result == _result()
    assert report.status is None
    assert report.api_latency_seconds == 0.7
    assert report.token_usage == AIObservationTokenUsage(300, 50, 350)
    assert telegram_message.calls == 0
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.reply_context is None
    assert request.message_age_seconds == 45
    assert request.trusted_area_context == "Львів"
    assert request.matched_keywords == ("перекри",)
    assert request.notify_all is False


@pytest.mark.asyncio
async def test_observe_sends_only_trimmed_reply_raw_text() -> None:
    marker = "  Контекст попереднього повідомлення  "
    client = StubClient([_success()])
    observer = _observer(client)
    telegram_message = ReplyMessage(
        reply_to_msg_id=12,
        outcome=SimpleNamespace(
            raw_text=marker,
            sender_id=999,
            username="must-not-be-copied",
            media=object(),
        ),
    )

    await observer.observe(
        _snapshot(),
        telegram_message=telegram_message,
        trusted_area_context=None,
    )

    assert telegram_message.calls == 1
    assert client.requests[0].reply_context == marker.strip()
    assert "must-not-be-copied" not in repr(client.requests[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_text", [None, "", "   "])
async def test_media_or_empty_reply_is_successful_null_context(raw_text: object) -> None:
    client = StubClient([_success()])
    observer = _observer(client)
    telegram_message = ReplyMessage(
        reply_to_msg_id=12,
        outcome=SimpleNamespace(raw_text=raw_text, media=object()),
    )

    report = await observer.observe(
        _snapshot(),
        telegram_message=telegram_message,
        trusted_area_context=None,
    )

    assert report.status is None
    assert client.requests[0].reply_context is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "telegram_message",
    [
        ReplyMessage(reply_to_msg_id=12, outcome=None),
        ReplyMessage(reply_to_msg_id=12, outcome=RuntimeError("private marker")),
        SimpleNamespace(reply_to_msg_id=12),
    ],
)
async def test_reply_lookup_failure_skips_openai_and_is_safely_normalized(
    telegram_message: object,
) -> None:
    client = StubClient([_success()])
    observer = _observer(client)

    report = await observer.observe(
        _snapshot(text="private message marker"),
        telegram_message=telegram_message,
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.REPLY_CONTEXT_ERROR
    assert report.result is None
    assert report.attempts == 0
    assert client.requests == []
    assert "private" not in repr(report)


@pytest.mark.asyncio
async def test_reply_lookup_and_api_share_one_remaining_deadline() -> None:
    moments = iter((100.0, 101.25, 101.75, 102.0))
    client = StubClient([_success()])
    observer = _observer(client, monotonic=lambda: next(moments))

    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(
            reply_to_msg_id=12,
            outcome=SimpleNamespace(raw_text="Контекст"),
        ),
        trusted_area_context="Львів",
    )

    assert client.timeouts == [pytest.approx(28.75)]
    assert report.elapsed_seconds == 2.0


@pytest.mark.asyncio
async def test_expired_budget_after_reply_skips_openai() -> None:
    moments = iter((100.0, 131.0, 131.0))
    client = StubClient([_success()])
    observer = _observer(client, monotonic=lambda: next(moments))

    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(
            reply_to_msg_id=12,
            outcome=SimpleNamespace(raw_text="Контекст"),
        ),
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.TIMEOUT
    assert client.requests == []


@pytest.mark.asyncio
async def test_deadline_interrupts_blocked_reply_lookup() -> None:
    waiting = asyncio.Event()

    async def block_forever() -> object:
        await waiting.wait()
        return SimpleNamespace(raw_text="late")

    client = StubClient([_success()])
    observer = _observer(client, config=_config(operation_timeout_seconds=0.01))

    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(reply_to_msg_id=12, outcome=block_forever),
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.TIMEOUT
    assert client.requests == []


@pytest.mark.asyncio
async def test_external_cancellation_propagates() -> None:
    started = asyncio.Event()
    waiting = asyncio.Event()

    async def block_forever() -> object:
        started.set()
        await waiting.wait()
        return SimpleNamespace(raw_text="late")

    observer = _observer(StubClient([_success()]))
    task = asyncio.create_task(
        observer.observe(
            _snapshot(),
            telegram_message=ReplyMessage(reply_to_msg_id=12, outcome=block_forever),
            trusted_area_context=None,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_unexpected_client_error_is_api_error_without_raw_data() -> None:
    marker = "secret raw response"
    client = StubClient([RuntimeError(marker)])
    observer = _observer(client)

    report = await observer.observe(
        _snapshot(text=marker),
        telegram_message=ReplyMessage(reply_to_msg_id=None),
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.API_ERROR
    assert report.api_latency_seconds is None
    assert marker not in repr(report)


@pytest.mark.asyncio
async def test_client_failure_metadata_is_preserved() -> None:
    failure = AIObservationFailure(
        status=AIObservationTechnicalStatus.RATE_LIMITED,
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="b" * 64,
        api_latency_seconds=0.25,
        attempts=2,
    )
    observer = _observer(StubClient([failure]))

    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(reply_to_msg_id=None),
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.RATE_LIMITED
    assert report.prompt_hash == "b" * 64
    assert report.api_latency_seconds == 0.25
    assert report.attempts == 2


@pytest.mark.asyncio
async def test_result_completed_after_deadline_is_discarded() -> None:
    moments = iter((100.0, 100.0, 131.0, 131.0))
    client = StubClient([_success()])
    observer = _observer(client, monotonic=lambda: next(moments))

    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(reply_to_msg_id=None),
        trusted_area_context=None,
    )

    assert report.status is AIObservationTechnicalStatus.TIMEOUT
    assert report.result is None


@pytest.mark.asyncio
async def test_observer_close_is_idempotent_and_post_close_observe_fails_open() -> None:
    client = StubClient([_success()])
    observer = _observer(client)

    await observer.close()
    await observer.close()
    report = await observer.observe(
        _snapshot(),
        telegram_message=ReplyMessage(reply_to_msg_id=None),
        trusted_area_context=None,
    )

    assert client.close_calls == 1
    assert report.status is AIObservationTechnicalStatus.API_ERROR
    assert client.requests == []


def test_report_requires_exactly_one_semantic_or_technical_result() -> None:
    values: dict[str, object] = {
        "result": None,
        "status": None,
        "model": "model",
        "prompt_hash": None,
        "elapsed_seconds": 0.0,
        "api_latency_seconds": None,
        "attempts": 0,
        "token_usage": None,
    }
    with pytest.raises(ValueError, match="exactly one"):
        AIObservationReport(**values)  # type: ignore[arg-type]

    values["result"] = _result()
    values["status"] = AIObservationTechnicalStatus.API_ERROR
    with pytest.raises(ValueError, match="exactly one"):
        AIObservationReport(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"elapsed_seconds": True}, "elapsed_seconds"),
        ({"elapsed_seconds": -0.1}, "elapsed_seconds"),
        ({"elapsed_seconds": math.nan}, "elapsed_seconds"),
        ({"api_latency_seconds": True}, "api_latency_seconds"),
        ({"api_latency_seconds": -0.1}, "api_latency_seconds"),
        ({"api_latency_seconds": math.inf}, "api_latency_seconds"),
    ),
)
def test_report_rejects_invalid_public_durations(
    updates: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "result": _result(),
        "status": None,
        "model": "model",
        "prompt_hash": "a" * 64,
        "elapsed_seconds": 0.0,
        "api_latency_seconds": 0.25,
        "attempts": 1,
        "token_usage": None,
    }
    values.update(updates)

    with pytest.raises(ValueError, match=message):
        AIObservationReport(**values)  # type: ignore[arg-type]


def test_report_normalizes_public_durations_to_float_seconds() -> None:
    report = AIObservationReport(
        result=_result(),
        status=None,
        model="model",
        prompt_hash="a" * 64,
        elapsed_seconds=2,
        api_latency_seconds=1,
        attempts=1,
        token_usage=None,
    )

    assert report.elapsed_seconds == 2.0
    assert isinstance(report.elapsed_seconds, float)
    assert report.api_latency_seconds == 1.0
    assert isinstance(report.api_latency_seconds, float)


def test_disabled_factory_does_no_setup() -> None:
    calls = 0

    def client_factory(_config: AIObservationConfig) -> OpenAIObservationClient | None:
        nonlocal calls
        calls += 1
        return StubClient([_success()])

    observer = build_ai_observer(_config(enabled=False), client_factory=client_factory)

    assert observer is None
    assert calls == 0


def test_enabled_factory_builds_openai_observer() -> None:
    client = StubClient([_success()])

    observer = build_ai_observer(_config(), client_factory=lambda _config: client)

    assert isinstance(observer, OpenAIMessageObserver)


@pytest.mark.parametrize("factory_result", [None, RuntimeError("secret setup marker")])
def test_factory_setup_failure_is_fail_open_and_logs_no_raw_error(
    factory_result: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def client_factory(_config: AIObservationConfig) -> OpenAIObservationClient | None:
        if isinstance(factory_result, Exception):
            raise factory_result
        return None

    with caplog.at_level(logging.ERROR):
        observer = build_ai_observer(_config(), client_factory=client_factory)

    assert isinstance(observer, UnavailableAIObserver)
    assert "status=api_error" in caplog.text
    assert "secret setup marker" not in caplog.text


def test_factory_setup_log_sanitizes_metadata_controls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _config(model="model\nforged-model-line")

    with caplog.at_level(logging.ERROR):
        observer = build_ai_observer(config, client_factory=lambda _config: None)

    assert isinstance(observer, UnavailableAIObserver)
    message = caplog.records[-1].getMessage()
    assert "\n" not in message
    assert "\r" not in message
    assert "model=model forged-model-line" in message


@pytest.mark.asyncio
async def test_unavailable_observer_returns_api_error_and_is_reusable() -> None:
    observer = UnavailableAIObserver(config=_config())

    first = await observer.observe(
        _snapshot(text="first private marker"),
        telegram_message=object(),
        trusted_area_context="private context",
    )
    second = await observer.observe(
        _snapshot(text="second private marker"),
        telegram_message=object(),
        trusted_area_context=None,
    )
    await observer.close()
    await observer.close()

    assert first.status is AIObservationTechnicalStatus.API_ERROR
    assert second.status is AIObservationTechnicalStatus.API_ERROR
    assert "private" not in repr(first)


def test_observer_rejects_invalid_construction() -> None:
    with pytest.raises(ConfigurationError, match="requires enabled"):
        OpenAIMessageObserver(config=_config(enabled=False), client=StubClient([]))

    with pytest.raises(ConfigurationError, match="config"):
        build_ai_observer(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_naive_telegram_timestamp_is_treated_as_utc() -> None:
    snapshot = _snapshot()
    snapshot = MessageSnapshot(
        source_title=snapshot.source_title,
        sender_name=snapshot.sender_name,
        text=snapshot.text,
        message_id=snapshot.message_id,
        peer_id=snapshot.peer_id,
        date=snapshot.date.replace(tzinfo=None),
        matched_keywords=snapshot.matched_keywords,
        notify_all=snapshot.notify_all,
    )
    now = datetime(2026, 8, 10, 9, 55, 20, tzinfo=UTC) + timedelta(seconds=8)
    client = StubClient([_success()])
    observer = _observer(client, now=lambda: now)

    await observer.observe(
        snapshot,
        telegram_message=ReplyMessage(reply_to_msg_id=None),
        trusted_area_context=None,
    )

    assert client.requests[0].sent_at.tzinfo is UTC
    assert client.requests[0].message_age_seconds == 8
