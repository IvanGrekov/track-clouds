from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

import telegram_monitor.credentials as credentials_module
import telegram_monitor.openai_client as openai_client_module
from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationTechnicalStatus,
)
from telegram_monitor.models import AIObservationConfig
from telegram_monitor.openai_client import (
    AIObservationFailure,
    AIObservationRequest,
    AIObservationSuccess,
    OpenAIObservationClient,
    build_openai_observation_client,
)
from telegram_monitor.prompt_bundle import PromptBundle


@pytest.fixture(autouse=True)
def _forbid_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credentials_module,
        "load_dotenv",
        lambda: pytest.fail("OpenAI client unit tests must not resolve dotenv files"),
    )


def _response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": "telegram_mobility_observation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "confidence",
                "location",
                "event",
                "temporal_relevance",
                "reason_code",
                "reason",
            ],
            "properties": {},
        },
    }


def _bundle() -> PromptBundle:
    response_format = _response_format()
    return PromptBundle(
        path=Path("/unused/prompts"),
        system_prompt="System prompt.",
        policy_prompt="Private policy.\n",
        prompt_hash="a" * 64,
        _response_format_json=json.dumps(response_format),
    )


def _config(**overrides: object) -> AIObservationConfig:
    values: dict[str, object] = {
        "enabled": True,
        "model": "gpt-5.4-nano-2026-03-17",
        "request_attempts": 2,
        "retry_base_seconds": 0,
        "retry_max_seconds": 0,
        "reasoning_effort": "none",
        "max_output_tokens": 800,
        "store_responses": False,
    }
    values.update(overrides)
    return AIObservationConfig(**values)  # type: ignore[arg-type]


def _request(*, marker: str = "дорогу перекрито") -> AIObservationRequest:
    return AIObservationRequest(
        message_text=f"На Городоцькій зараз {marker}",
        reply_context="Попереднє повідомлення без інструкцій",
        sent_at=datetime(2026, 8, 10, 9, 55, 20, tzinfo=UTC),
        message_age_seconds=8,
        trusted_area_context="Львів",
        matched_keywords=("перекри",),
        notify_all=False,
    )


def _accepted_json() -> str:
    return json.dumps(
        {
            "decision": "accept",
            "confidence": 0.96,
            "location": "Городоцька",
            "event": "перекрита права смуга",
            "temporal_relevance": "current",
            "reason_code": "meets_all_criteria",
            "reason": "Є актуальна подія та конкретна локація.",
        },
        ensure_ascii=False,
    )


def _semantically_invalid_json() -> str:
    payload = json.loads(_accepted_json())
    payload["location"] = None
    return json.dumps(payload, ensure_ascii=False)


def _sdk_response(
    text: str = "",
    *,
    status: str = "completed",
    content_type: str = "output_text",
    usage: object | None = None,
    incomplete_reason: str = "max_output_tokens",
    error_code: str | None = None,
) -> SimpleNamespace:
    content = (
        SimpleNamespace(type="refusal", refusal=text)
        if content_type == "refusal"
        else SimpleNamespace(type="output_text", text=text)
    )
    return SimpleNamespace(
        status=status,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason) if status == "incomplete" else None
        ),
        error=SimpleNamespace(code=error_code) if error_code is not None else None,
        output=[SimpleNamespace(type="message", content=[content])],
        output_text=text if content_type == "output_text" else "",
        usage=usage,
        model="gpt-5.4-nano-2026-03-17",
        _request_id="req_test_123",
    )


class FakeResponses:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            value = outcome()
            if asyncio.iscoroutine(value):
                return await value
            return value
        return outcome


class FakeSDKClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.responses = FakeResponses(outcomes)
        self.option_calls: list[dict[str, object]] = []
        self.close_calls = 0

    def with_options(self, **kwargs: object) -> FakeSDKClient:
        self.option_calls.append(kwargs)
        return self

    async def close(self) -> None:
        self.close_calls += 1

    async def aclose(self) -> None:
        self.close_calls += 1


def _client(
    outcomes: list[object],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    random_float: Callable[[], float] = random.random,
    **config_overrides: object,
) -> tuple[
    OpenAIObservationClient,
    FakeSDKClient,
]:
    sdk = FakeSDKClient(outcomes)
    client = OpenAIObservationClient(
        config=_config(**config_overrides),
        bundle=_bundle(),
        sdk_client=sdk,
        sleep=sleep,
        monotonic=monotonic,
        random_float=random_float,
    )
    return client, sdk


def _rate_limit_error() -> openai.RateLimitError:
    return _rate_limit_error_with()


def _rate_limit_error_with(
    *,
    code: str = "rate_limit_exceeded",
    headers: dict[str, str] | None = None,
) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(429, request=request, headers=headers)
    body = {"code": code, "message": "safe test error"}
    return openai.RateLimitError("rate limited", response=response, body=body)


def _status_error(
    status_code: int,
    *,
    error_type: type[openai.APIStatusError] = openai.APIStatusError,
    headers: dict[str, str] | None = None,
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(status_code, request=request, headers=headers)
    return error_type("safe test error", response=response, body={})


def _timeout_error() -> openai.APITimeoutError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APITimeoutError(request=request)


def _connection_error() -> openai.APIConnectionError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.APIConnectionError(request=request)


def _request_values() -> dict[str, object]:
    return {
        "message_text": "На Городоцькій перекрито рух",
        "sent_at": datetime(2026, 8, 10, 9, 55, 20, tzinfo=UTC),
        "message_age_seconds": 8,
        "trusted_area_context": "Львів",
        "matched_keywords": ("перекри",),
        "notify_all": False,
        "reply_context": None,
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"message_text": "  "}, "message_text"),
        ({"message_text": 42}, "message_text"),
        ({"sent_at": datetime(2026, 8, 10, 9, 55, 20)}, "sent_at"),
        ({"sent_at": "2026-08-10T09:55:20Z"}, "sent_at"),
        ({"message_age_seconds": True}, "message_age_seconds"),
        ({"message_age_seconds": -1}, "message_age_seconds"),
        ({"message_age_seconds": 1.5}, "message_age_seconds"),
        ({"matched_keywords": "перекри"}, "matched_keywords"),
        ({"matched_keywords": (42,)}, "matched keyword"),
        ({"notify_all": 1}, "notify_all"),
        ({"trusted_area_context": 42}, "trusted_area_context"),
        ({"reply_context": 42}, "reply_context"),
    ],
)
def test_request_rejects_invalid_values(
    updates: dict[str, object],
    message: str,
) -> None:
    values = _request_values()
    values.update(updates)

    with pytest.raises(ValueError, match=message):
        AIObservationRequest(**values)  # type: ignore[arg-type]


def test_request_allows_empty_keyword_matches_and_normalizes_optional_text() -> None:
    values = _request_values()
    values.update(
        matched_keywords=[" ", ""],
        # A question may pass the deterministic filter without a keyword or notify_all.
        notify_all=False,
        trusted_area_context="  ",
        reply_context="  ",
    )

    request = AIObservationRequest(**values)  # type: ignore[arg-type]

    assert request.matched_keywords == ()
    assert request.notify_all is False
    assert request.trusted_area_context is None
    assert request.reply_context is None


@pytest.mark.asyncio
async def test_classify_sends_exact_responses_request_and_returns_usage() -> None:
    usage = SimpleNamespace(input_tokens=111, output_tokens=22, total_tokens=133)
    client, sdk = _client([_sdk_response(_accepted_json(), usage=usage)])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationSuccess)
    assert outcome.result.decision is AIDecision.ACCEPT
    assert outcome.model == "gpt-5.4-nano-2026-03-17"
    assert outcome.prompt_hash == "a" * 64
    assert outcome.token_usage is not None
    assert outcome.token_usage.input_tokens == 111
    assert outcome.token_usage.output_tokens == 22
    assert outcome.token_usage.total_tokens == 133
    assert outcome.request_id == "req_test_123"
    assert isinstance(outcome.api_latency_seconds, float)
    assert outcome.api_latency_seconds >= 0
    assert outcome.attempts == 1

    assert len(sdk.responses.calls) == 1
    call = sdk.responses.calls[0]
    assert call["model"] == "gpt-5.4-nano-2026-03-17"
    assert call["reasoning"] == {"effort": "none"}
    assert call["max_output_tokens"] == 800
    assert call["store"] is False
    assert "tools" not in call
    assert call["text"] == {"format": _response_format()}
    assert call["instructions"] == "System prompt."
    assert call["input"][0] == {
        "role": "developer",
        "content": "Private policy.\n",
    }
    assert 0 < call["timeout"] <= 1

    raw_input = call["input"]
    assert isinstance(raw_input, list)
    assert raw_input[-1]["role"] == "user"
    input_payload = json.loads(raw_input[-1]["content"])
    assert input_payload == {
        "message_text": "На Городоцькій зараз дорогу перекрито",
        "reply_context": "Попереднє повідомлення без інструкцій",
        "sent_at": "2026-08-10T09:55:20+00:00",
        "message_age_seconds": 8,
        "trusted_area_context": "Львів",
        "prefilter": {
            "matched_keywords": ["перекри"],
            "notify_all": False,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        SimpleNamespace(input_tokens=True, output_tokens=2, total_tokens=3),
        SimpleNamespace(input_tokens=1, output_tokens=-1, total_tokens=0),
        SimpleNamespace(input_tokens=1, output_tokens="2", total_tokens=3),
        SimpleNamespace(input_tokens=1, output_tokens=2),
    ],
)
async def test_success_keeps_result_when_usage_is_missing_or_malformed(usage: object) -> None:
    client, _ = _client([_sdk_response(_accepted_json(), usage=usage)])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationSuccess)
    assert outcome.result.decision is AIDecision.ACCEPT
    assert outcome.token_usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (
            _sdk_response("unsafe refusal details", content_type="refusal"),
            AIObservationTechnicalStatus.REFUSAL,
        ),
        (
            _sdk_response(_accepted_json(), status="incomplete"),
            AIObservationTechnicalStatus.INVALID_RESPONSE,
        ),
        (
            _sdk_response(
                _accepted_json(),
                status="incomplete",
                incomplete_reason="content_filter",
            ),
            AIObservationTechnicalStatus.REFUSAL,
        ),
        (
            _sdk_response(
                _accepted_json(),
                status="failed",
                error_code="invalid_prompt",
            ),
            AIObservationTechnicalStatus.API_ERROR,
        ),
        (
            _sdk_response(_accepted_json(), status="queued"),
            AIObservationTechnicalStatus.INVALID_RESPONSE,
        ),
        (
            _sdk_response("not valid JSON"),
            AIObservationTechnicalStatus.INVALID_RESPONSE,
        ),
        (
            _sdk_response(_semantically_invalid_json()),
            AIObservationTechnicalStatus.INVALID_RESPONSE,
        ),
        (
            _sdk_response(""),
            AIObservationTechnicalStatus.INVALID_RESPONSE,
        ),
    ],
)
async def test_classify_normalizes_non_successful_outputs(
    response: object,
    expected_status: AIObservationTechnicalStatus,
) -> None:
    client, _ = _client([response])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["server_error", "rate_limit_exceeded"])
async def test_transient_failed_response_retries_then_succeeds(error_code: str) -> None:
    failed = _sdk_response(_accepted_json(), status="failed", error_code=error_code)
    client, sdk = _client([failed, _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationSuccess)
    assert outcome.attempts == 2
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("server_error", AIObservationTechnicalStatus.API_ERROR),
        ("rate_limit_exceeded", AIObservationTechnicalStatus.RATE_LIMITED),
    ],
)
async def test_transient_failed_response_uses_total_attempt_limit(
    error_code: str,
    expected_status: AIObservationTechnicalStatus,
) -> None:
    failures = [
        _sdk_response(_accepted_json(), status="failed", error_code=error_code),
        _sdk_response(_accepted_json(), status="failed", error_code=error_code),
    ]
    client, sdk = _client(failures)

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is expected_status
    assert outcome.attempts == 2
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
async def test_nontransient_failed_response_is_not_retried() -> None:
    failed = _sdk_response(_accepted_json(), status="failed", error_code="invalid_prompt")
    client, sdk = _client([failed, _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1


@pytest.mark.asyncio
async def test_classify_retries_rate_limit_then_returns_success() -> None:
    client, sdk = _client([_rate_limit_error(), _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationSuccess)
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
async def test_classify_reports_rate_limit_after_total_attempts() -> None:
    client, sdk = _client([_rate_limit_error(), _rate_limit_error()])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.RATE_LIMITED
    assert outcome.attempts == 2
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        _timeout_error,
        _connection_error,
        lambda: _status_error(408),
        lambda: _status_error(409),
        lambda: _status_error(500),
    ],
    ids=["sdk-timeout", "connection", "http-408", "http-409", "http-500"],
)
async def test_retryable_sdk_errors_retry_then_succeed(
    error_factory: Callable[[], Exception],
) -> None:
    client, sdk = _client([error_factory(), _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationSuccess)
    assert outcome.attempts == 2
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _status_error(401, error_type=openai.AuthenticationError),
        _status_error(422, error_type=openai.UnprocessableEntityError),
    ],
    ids=["authentication", "unprocessable-entity"],
)
async def test_nonretryable_sdk_status_errors_stop_after_one_attempt(error: Exception) -> None:
    client, sdk = _client([error, _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1


@pytest.mark.asyncio
async def test_quota_rate_limit_is_api_error_and_is_not_retried() -> None:
    quota_error = _rate_limit_error_with(code="insufficient_quota")
    client, sdk = _client([quota_error, _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1


@pytest.mark.asyncio
async def test_exhausted_sdk_timeout_is_normalized_as_timeout() -> None:
    client, sdk = _client([_timeout_error(), _timeout_error()])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.TIMEOUT
    assert outcome.attempts == 2
    assert len(sdk.responses.calls) == 2


@pytest.mark.asyncio
async def test_retry_backoff_is_deterministic_with_injected_clock_and_random() -> None:
    now = 100.0
    delays: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    client, sdk = _client(
        [_connection_error(), _sdk_response(_accepted_json())],
        sleep=sleep,
        monotonic=monotonic,
        random_float=lambda: 0.5,
        retry_base_seconds=1,
        retry_max_seconds=2,
    )

    outcome = await client.classify(_request(), timeout_seconds=10)

    assert isinstance(outcome, AIObservationSuccess)
    assert outcome.attempts == 2
    assert delays == [1.0]
    assert len(sdk.responses.calls) == 2
    assert sdk.responses.calls[0]["timeout"] == 10
    assert sdk.responses.calls[1]["timeout"] == 9


@pytest.mark.asyncio
async def test_retry_after_header_takes_precedence_over_shorter_backoff() -> None:
    now = 100.0
    delays: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(delay: float) -> None:
        nonlocal now
        delays.append(delay)
        now += delay

    rate_limit = _rate_limit_error_with(headers={"retry-after-ms": "1500"})
    client, _ = _client(
        [rate_limit, _sdk_response(_accepted_json())],
        sleep=sleep,
        monotonic=monotonic,
        random_float=lambda: 0.5,
        retry_base_seconds=0.25,
        retry_max_seconds=2,
    )

    outcome = await client.classify(_request(), timeout_seconds=10)

    assert isinstance(outcome, AIObservationSuccess)
    assert delays == [1.5]


@pytest.mark.asyncio
async def test_retry_stops_when_backoff_does_not_fit_remaining_budget() -> None:
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)

    client, sdk = _client(
        [_connection_error(), _sdk_response(_accepted_json())],
        sleep=sleep,
        monotonic=lambda: 100.0,
        random_float=lambda: 0.5,
        retry_base_seconds=2,
        retry_max_seconds=2,
    )

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_retry_after_larger_than_configured_cap_is_not_slept_or_retried() -> None:
    sleep_calls: list[float] = []

    async def sleep(delay: float) -> None:
        sleep_calls.append(delay)

    rate_limit = _rate_limit_error_with(headers={"retry-after": "3"})
    client, sdk = _client(
        [rate_limit, _sdk_response(_accepted_json())],
        sleep=sleep,
        retry_base_seconds=0.25,
        retry_max_seconds=2,
    )

    outcome = await client.classify(_request(), timeout_seconds=10)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.RATE_LIMITED
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_classify_uses_one_timeout_budget_for_the_whole_operation() -> None:
    async def never_finishes() -> object:
        await asyncio.Future()
        raise AssertionError("unreachable")

    client, _ = _client([never_finishes])

    outcome = await client.classify(_request(), timeout_seconds=0.01)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [0, -1])
async def test_nonpositive_timeout_returns_timeout_without_api_attempt(
    timeout_seconds: float,
) -> None:
    client, sdk = _client([_sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=timeout_seconds)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.TIMEOUT
    assert outcome.attempts == 0
    assert sdk.responses.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [True, float("nan"), float("inf"), "1"])
async def test_invalid_timeout_is_rejected_without_api_attempt(timeout_seconds: object) -> None:
    client, sdk = _client([_sdk_response(_accepted_json())])

    with pytest.raises(ValueError, match="timeout_seconds"):
        await client.classify(
            _request(),
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )

    assert sdk.responses.calls == []


@pytest.mark.asyncio
async def test_external_cancellation_is_not_converted_to_timeout() -> None:
    started = asyncio.Event()

    async def never_finishes() -> object:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client, _ = _client([never_finishes])
    task = asyncio.create_task(client.classify(_request(), timeout_seconds=10))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    client, sdk = _client([_sdk_response(_accepted_json())])

    await client.close()
    await client.close()

    assert sdk.close_calls == 1


@pytest.mark.asyncio
async def test_closed_client_returns_api_error_without_new_attempt() -> None:
    client, sdk = _client([_sdk_response(_accepted_json())])
    await client.close()

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 0
    assert sdk.responses.calls == []


@pytest.mark.asyncio
async def test_request_and_failure_repr_do_not_expose_untrusted_text() -> None:
    marker = "DO_NOT_EXPOSE_RAW_MESSAGE_42"
    request = _request(marker=marker)
    client, _ = _client([_sdk_response(marker, content_type="refusal")])

    outcome = await client.classify(request, timeout_seconds=1)

    assert marker not in repr(request)
    assert isinstance(outcome, AIObservationFailure)
    assert marker not in repr(outcome)
    assert "Private policy." not in repr(client)


@pytest.mark.asyncio
async def test_unexpected_exception_details_do_not_reach_outcome_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "DO_NOT_EXPOSE_UNEXPECTED_EXCEPTION_73"
    client, sdk = _client([RuntimeError(marker), _sdk_response(_accepted_json())])

    outcome = await client.classify(_request(), timeout_seconds=1)

    assert isinstance(outcome, AIObservationFailure)
    assert outcome.status is AIObservationTechnicalStatus.API_ERROR
    assert outcome.attempts == 1
    assert len(sdk.responses.calls) == 1
    assert marker not in repr(outcome)
    assert marker not in caplog.text


def test_disabled_factory_avoids_credentials_bundle_and_sdk_setup() -> None:
    config = _config(
        enabled=False,
        prompt_bundle_path=Path("/definitely/missing/prompt-bundle"),
        policy_prompt_path=Path("/definitely/missing/policy-prompt.txt"),
    )

    def unexpected_sdk_factory(**_kwargs: object) -> FakeSDKClient:
        pytest.fail("disabled AI must not construct the OpenAI SDK client")

    assert build_openai_observation_client(config, sdk_factory=unexpected_sdk_factory) is None


def test_factory_disables_sdk_internal_retries_without_live_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, object]] = []
    sdk = FakeSDKClient([_sdk_response(_accepted_json())])

    def fake_async_openai(**kwargs: object) -> FakeSDKClient:
        constructed.append(kwargs)
        return sdk

    monkeypatch.setattr(
        openai_client_module,
        "prepare_ai_observation",
        lambda config: _bundle(),
    )

    client = build_openai_observation_client(_config(), sdk_factory=fake_async_openai)

    assert isinstance(client, OpenAIObservationClient)
    assert constructed == [{"max_retries": 0}]
    assert sdk.responses.calls == []
