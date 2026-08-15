"""Isolated asynchronous OpenAI adapter for semantic message observation."""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeAlias

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
)

from .ai_models import (
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIResponseValidationError,
    parse_ai_observation_response,
)
from .models import AIObservationConfig, ConfigurationError
from .prompt_bundle import PromptBundle, prepare_ai_observation

__all__ = [
    "AIObservationFailure",
    "AIObservationOutcome",
    "AIObservationRequest",
    "AIObservationSuccess",
    "AIObservationTokenUsage",
    "OpenAIObservationClient",
    "build_openai_observation_client",
]

_NON_RETRYABLE_RATE_LIMIT_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "credit_balance_exhausted",
        "insufficient_quota",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "project_spend_limit_exceeded",
    }
)


class _ResponsesResource(Protocol):
    def create(self, **kwargs: Any) -> Awaitable[Any]: ...


class _AsyncOpenAIClient(Protocol):
    responses: _ResponsesResource

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AIObservationRequest:
    """Trusted envelope whose text fields remain untrusted model input."""

    message_text: str = field(repr=False)
    sent_at: datetime
    message_age_seconds: int
    trusted_area_context: str | None = field(default=None, repr=False)
    matched_keywords: tuple[str, ...] | list[str] = field(default=(), repr=False)
    notify_all: bool = False
    reply_context: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.message_text, str) or not self.message_text.strip():
            raise ValueError("AI observation message_text must be a non-empty string")
        if not isinstance(self.sent_at, datetime) or self.sent_at.utcoffset() is None:
            raise ValueError("AI observation sent_at must be a timezone-aware datetime")
        if (
            isinstance(self.message_age_seconds, bool)
            or not isinstance(self.message_age_seconds, int)
            or self.message_age_seconds < 0
        ):
            raise ValueError("AI observation message_age_seconds must be a non-negative integer")
        if isinstance(self.matched_keywords, str) or not isinstance(
            self.matched_keywords, (tuple, list)
        ):
            raise ValueError("AI observation matched_keywords must be a tuple or list")
        if any(not isinstance(keyword, str) for keyword in self.matched_keywords):
            raise ValueError("Every AI observation matched keyword must be a string")
        object.__setattr__(
            self,
            "matched_keywords",
            tuple(keyword.strip() for keyword in self.matched_keywords if keyword.strip()),
        )
        if not isinstance(self.notify_all, bool):
            raise ValueError("AI observation notify_all must be boolean")

        for field_name in ("trusted_area_context", "reply_context"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"AI observation {field_name} must be a string or null")
            object.__setattr__(self, field_name, value.strip() or None)


@dataclass(frozen=True, slots=True)
class AIObservationTokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class AIObservationSuccess:
    result: AIObservationResult = field(repr=False)
    model: str
    prompt_hash: str
    api_latency_seconds: float
    attempts: int
    token_usage: AIObservationTokenUsage | None
    request_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AIObservationFailure:
    status: AIObservationTechnicalStatus
    model: str
    prompt_hash: str
    api_latency_seconds: float
    attempts: int
    request_id: str | None = field(default=None, repr=False)


AIObservationOutcome: TypeAlias = AIObservationSuccess | AIObservationFailure


@dataclass(frozen=True, slots=True)
class _RetryDisposition:
    status: AIObservationTechnicalStatus
    retryable: bool
    retry_after_seconds: float | None = None
    request_id: str | None = None


def _member(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_request_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 200:
        return None
    return cleaned


def _response_contains_refusal(response: object) -> bool:
    output = _member(response, "output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes, bytearray)):
        return False
    for item in output:
        if _member(item, "type") != "message":
            continue
        content = _member(item, "content")
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
            continue
        if any(_member(block, "type") == "refusal" for block in content):
            return True
    return False


def _token_usage(response: object) -> AIObservationTokenUsage | None:
    usage = _member(response, "usage")
    values = tuple(
        _member(usage, field_name)
        for field_name in ("input_tokens", "output_tokens", "total_tokens")
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        return None
    input_tokens, output_tokens, total_tokens = values
    return AIObservationTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _retry_after_seconds(error: APIStatusError) -> float | None:
    headers = error.response.headers
    raw_milliseconds = headers.get("retry-after-ms")
    if raw_milliseconds is not None:
        try:
            delay = float(raw_milliseconds) / 1_000
        except (TypeError, ValueError):
            delay = math.nan
        if math.isfinite(delay) and delay >= 0:
            return delay

    raw_retry_after = headers.get("retry-after")
    if raw_retry_after is None:
        return None
    try:
        delay = float(raw_retry_after)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw_retry_after)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
    return delay if math.isfinite(delay) and delay >= 0 else None


def _classify_error(error: Exception) -> _RetryDisposition:
    if isinstance(error, TimeoutError):
        return _RetryDisposition(AIObservationTechnicalStatus.TIMEOUT, retryable=True)
    if isinstance(error, APITimeoutError):
        return _RetryDisposition(AIObservationTechnicalStatus.TIMEOUT, retryable=True)
    if isinstance(error, APIConnectionError):
        return _RetryDisposition(AIObservationTechnicalStatus.API_ERROR, retryable=True)
    if isinstance(error, APIStatusError):
        request_id = _safe_request_id(error.request_id)
        if error.status_code == 429:
            code = error.code if isinstance(error.code, str) else None
            if code in _NON_RETRYABLE_RATE_LIMIT_CODES:
                return _RetryDisposition(
                    AIObservationTechnicalStatus.API_ERROR,
                    retryable=False,
                    request_id=request_id,
                )
            return _RetryDisposition(
                AIObservationTechnicalStatus.RATE_LIMITED,
                retryable=True,
                retry_after_seconds=_retry_after_seconds(error),
                request_id=request_id,
            )
        if error.status_code == 408:
            status = AIObservationTechnicalStatus.TIMEOUT
        else:
            status = AIObservationTechnicalStatus.API_ERROR
        return _RetryDisposition(
            status,
            retryable=error.status_code in {408, 409} or error.status_code >= 500,
            retry_after_seconds=_retry_after_seconds(error),
            request_id=request_id,
        )
    if isinstance(error, OpenAIError):
        return _RetryDisposition(AIObservationTechnicalStatus.API_ERROR, retryable=False)
    return _RetryDisposition(AIObservationTechnicalStatus.API_ERROR, retryable=False)


def _classify_failed_response(response: object) -> _RetryDisposition | None:
    """Return retry metadata for transient failures encoded in a Response object."""

    if _member(response, "status") != "failed" or _response_contains_refusal(response):
        return None
    error = _member(response, "error")
    code = _member(error, "code")
    request_id = _safe_request_id(_member(response, "_request_id"))
    if code == "rate_limit_exceeded":
        return _RetryDisposition(
            AIObservationTechnicalStatus.RATE_LIMITED,
            retryable=True,
            request_id=request_id,
        )
    if code == "server_error":
        return _RetryDisposition(
            AIObservationTechnicalStatus.API_ERROR,
            retryable=True,
            request_id=request_id,
        )
    return None


class OpenAIObservationClient:
    """One reusable async client with bounded, application-owned retries."""

    def __init__(
        self,
        *,
        config: AIObservationConfig,
        bundle: PromptBundle,
        sdk_client: _AsyncOpenAIClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_float: Callable[[], float] = random.random,
    ) -> None:
        if not isinstance(config, AIObservationConfig):
            raise ConfigurationError("config must be an AIObservationConfig")
        if not isinstance(bundle, PromptBundle):
            raise ConfigurationError("bundle must be a PromptBundle")
        self._config = config
        self._bundle = bundle
        self._sdk_client = sdk_client
        self._sleep = sleep
        self._monotonic = monotonic
        self._random_float = random_float
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._config.model!r}, closed={self._closed!r})"

    async def classify(
        self,
        request: AIObservationRequest,
        *,
        timeout_seconds: float,
    ) -> AIObservationOutcome:
        if not isinstance(request, AIObservationRequest):
            raise TypeError("request must be an AIObservationRequest")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be a finite number")

        started = self._monotonic()
        attempts = [0]
        if self._closed or timeout_seconds <= 0:
            status = (
                AIObservationTechnicalStatus.API_ERROR
                if self._closed
                else AIObservationTechnicalStatus.TIMEOUT
            )
            return self._failure(status, started=started, attempts=0)

        budget = min(float(timeout_seconds), self._config.operation_timeout_seconds)
        deadline = started + budget
        try:
            async with asyncio.timeout(budget):
                return await self._classify_with_retries(
                    request,
                    started=started,
                    deadline=deadline,
                    attempts=attempts,
                )
        except TimeoutError:
            return self._failure(
                AIObservationTechnicalStatus.TIMEOUT,
                started=started,
                attempts=attempts[0],
            )

    async def _classify_with_retries(
        self,
        request: AIObservationRequest,
        *,
        started: float,
        deadline: float,
        attempts: list[int],
    ) -> AIObservationOutcome:
        serialized_input = self._serialize_input(request)
        while attempts[0] < self._config.request_attempts:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return self._failure(
                    AIObservationTechnicalStatus.TIMEOUT,
                    started=started,
                    attempts=attempts[0],
                )

            attempts[0] += 1
            try:
                response = await self._sdk_client.responses.create(
                    model=self._config.model,
                    instructions=self._bundle.system_prompt,
                    input=[
                        {"role": "developer", "content": self._bundle.policy_prompt},
                        {"role": "user", "content": serialized_input},
                    ],
                    text={"format": self._bundle.response_format},
                    reasoning={"effort": self._config.reasoning_effort},
                    max_output_tokens=self._config.max_output_tokens,
                    store=self._config.store_responses,
                    timeout=remaining,
                )
            except Exception as error:
                disposition = _classify_error(error)
                terminal_failure = await self._retry_or_failure(
                    disposition,
                    started=started,
                    deadline=deadline,
                    attempts=attempts[0],
                )
                if terminal_failure is not None:
                    return terminal_failure
                continue

            disposition = _classify_failed_response(response)
            if disposition is not None:
                terminal_failure = await self._retry_or_failure(
                    disposition,
                    started=started,
                    deadline=deadline,
                    attempts=attempts[0],
                )
                if terminal_failure is not None:
                    return terminal_failure
                continue

            return self._response_outcome(
                response,
                started=started,
                attempts=attempts[0],
            )

        return self._failure(
            AIObservationTechnicalStatus.API_ERROR,
            started=started,
            attempts=attempts[0],
        )

    async def _retry_or_failure(
        self,
        disposition: _RetryDisposition,
        *,
        started: float,
        deadline: float,
        attempts: int,
    ) -> AIObservationFailure | None:
        if not disposition.retryable or attempts >= self._config.request_attempts:
            return self._failure(
                disposition.status,
                started=started,
                attempts=attempts,
                request_id=disposition.request_id,
            )

        delay = self._retry_delay(attempts)
        if disposition.retry_after_seconds is not None:
            if disposition.retry_after_seconds > self._config.retry_max_seconds:
                return self._failure(
                    disposition.status,
                    started=started,
                    attempts=attempts,
                    request_id=disposition.request_id,
                )
            delay = max(delay, disposition.retry_after_seconds)
        remaining = deadline - self._monotonic()
        if delay >= remaining:
            return self._failure(
                disposition.status,
                started=started,
                attempts=attempts,
                request_id=disposition.request_id,
            )
        if delay > 0:
            await self._sleep(delay)
        return None

    def _response_outcome(
        self,
        response: object,
        *,
        started: float,
        attempts: int,
    ) -> AIObservationOutcome:
        request_id = _safe_request_id(_member(response, "_request_id"))
        try:
            status = _member(response, "status")
            if _response_contains_refusal(response):
                return self._failure(
                    AIObservationTechnicalStatus.REFUSAL,
                    started=started,
                    attempts=attempts,
                    request_id=request_id,
                )
            if status == "incomplete":
                incomplete_details = _member(response, "incomplete_details")
                technical_status = (
                    AIObservationTechnicalStatus.REFUSAL
                    if _member(incomplete_details, "reason") == "content_filter"
                    else AIObservationTechnicalStatus.INVALID_RESPONSE
                )
                return self._failure(
                    technical_status,
                    started=started,
                    attempts=attempts,
                    request_id=request_id,
                )
            response_error = _member(response, "error")
            if status == "failed" or response_error is not None:
                technical_status = (
                    AIObservationTechnicalStatus.RATE_LIMITED
                    if _member(response_error, "code") == "rate_limit_exceeded"
                    else AIObservationTechnicalStatus.API_ERROR
                )
                return self._failure(
                    technical_status,
                    started=started,
                    attempts=attempts,
                    request_id=request_id,
                )
            if status != "completed":
                return self._failure(
                    AIObservationTechnicalStatus.INVALID_RESPONSE,
                    started=started,
                    attempts=attempts,
                    request_id=request_id,
                )

            output_text = _member(response, "output_text")
            if not isinstance(output_text, str) or not output_text.strip():
                return self._failure(
                    AIObservationTechnicalStatus.INVALID_RESPONSE,
                    started=started,
                    attempts=attempts,
                    request_id=request_id,
                )
            result = parse_ai_observation_response(output_text)
        except (AIResponseValidationError, TypeError, ValueError, AttributeError):
            return self._failure(
                AIObservationTechnicalStatus.INVALID_RESPONSE,
                started=started,
                attempts=attempts,
                request_id=request_id,
            )

        return AIObservationSuccess(
            result=result,
            model=self._config.model,
            prompt_hash=self._bundle.prompt_hash,
            api_latency_seconds=self._elapsed_seconds(started),
            attempts=attempts,
            token_usage=_token_usage(response),
            request_id=request_id,
        )

    def _serialize_input(self, request: AIObservationRequest) -> str:
        payload = {
            "message_text": request.message_text,
            "reply_context": request.reply_context,
            "sent_at": request.sent_at.isoformat(),
            "message_age_seconds": request.message_age_seconds,
            "trusted_area_context": request.trusted_area_context,
            "prefilter": {
                "matched_keywords": list(request.matched_keywords),
                "notify_all": request.notify_all,
            },
        }
        return json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _retry_delay(self, attempts_completed: int) -> float:
        unjittered = min(
            self._config.retry_max_seconds,
            self._config.retry_base_seconds * (2 ** (attempts_completed - 1)),
        )
        if unjittered <= 0:
            return 0.0
        random_value = self._random_float()
        if not isinstance(random_value, (int, float)) or not math.isfinite(random_value):
            random_value = 0.5
        random_value = min(1.0, max(0.0, float(random_value)))
        return min(
            self._config.retry_max_seconds,
            unjittered * (0.8 + 0.4 * random_value),
        )

    def _elapsed_seconds(self, started: float) -> float:
        elapsed = self._monotonic() - started
        if not math.isfinite(elapsed):
            return 0.0
        return max(0.0, round(elapsed, 3))

    def _failure(
        self,
        status: AIObservationTechnicalStatus,
        *,
        started: float,
        attempts: int,
        request_id: str | None = None,
    ) -> AIObservationFailure:
        return AIObservationFailure(
            status=status,
            model=self._config.model,
            prompt_hash=self._bundle.prompt_hash,
            api_latency_seconds=self._elapsed_seconds(started),
            attempts=attempts,
            request_id=request_id,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._sdk_client.close()


def build_openai_observation_client(
    config: AIObservationConfig,
    *,
    sdk_factory: Callable[..., _AsyncOpenAIClient] = AsyncOpenAI,
) -> OpenAIObservationClient | None:
    """Build the optional client without making a network request."""

    bundle = prepare_ai_observation(config)
    if bundle is None:
        return None
    sdk_client = sdk_factory(max_retries=0)
    return OpenAIObservationClient(
        config=config,
        bundle=bundle,
        sdk_client=sdk_client,
    )
