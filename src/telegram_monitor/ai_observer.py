"""End-to-end orchestration for optional semantic AI observation."""

from __future__ import annotations

import asyncio
import logging
import math
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypeAlias

from .ai_models import AIObservationResult, AIObservationTechnicalStatus
from .models import AIObservationConfig, ConfigurationError, MessageSnapshot
from .openai_client import (
    AIObservationFailure,
    AIObservationSuccess,
    AIObservationTokenUsage,
    OpenAIObservationClient,
    build_openai_observation_client,
)

__all__ = [
    "AIObservationReport",
    "AIObserver",
    "OpenAIMessageObserver",
    "UnavailableAIObserver",
    "build_ai_observer",
]

LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_log_value(value: str, *, max_chars: int = 128) -> str:
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in value
    )
    cleaned = " ".join(without_controls.split())
    return (cleaned or "-")[:max_chars]


@dataclass(frozen=True, slots=True)
class AIObservationReport:
    """One normalized observation result with end-to-end timing metadata.

    Exactly one of ``result`` and ``status`` is present. The report deliberately
    excludes raw prompts, Telegram text, API responses, exceptions and API keys.
    """

    result: AIObservationResult | None = field(repr=False)
    status: AIObservationTechnicalStatus | None
    model: str
    prompt_hash: str | None = field(repr=False)
    elapsed_seconds: float
    api_latency_seconds: float | None
    attempts: int
    token_usage: AIObservationTokenUsage | None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.status is None):
            raise ValueError("AI observation report must contain exactly one result or status")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("AI observation report model must be a non-empty string")
        if self.prompt_hash is not None and (
            not isinstance(self.prompt_hash, str) or not self.prompt_hash.strip()
        ):
            raise ValueError("AI observation report prompt_hash must be non-empty or null")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, (int, float))
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("AI observation report elapsed_seconds must be non-negative")
        object.__setattr__(self, "elapsed_seconds", float(self.elapsed_seconds))
        if self.api_latency_seconds is not None:
            if (
                isinstance(self.api_latency_seconds, bool)
                or not isinstance(self.api_latency_seconds, (int, float))
                or not math.isfinite(self.api_latency_seconds)
                or self.api_latency_seconds < 0
            ):
                raise ValueError(
                    "AI observation report api_latency_seconds must be non-negative or null"
                )
            object.__setattr__(self, "api_latency_seconds", float(self.api_latency_seconds))
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise ValueError("AI observation report attempts must be non-negative")


class AIObserver(Protocol):
    """Reusable optional observer owned by the monitor lifecycle."""

    async def observe(
        self,
        snapshot: MessageSnapshot,
        *,
        trusted_area_context: str | None,
    ) -> AIObservationReport: ...

    async def close(self) -> None: ...


_ClientFactory: TypeAlias = Callable[[AIObservationConfig], OpenAIObservationClient | None]


class OpenAIMessageObserver:
    """Run one bounded OpenAI classification for the current message."""

    def __init__(
        self,
        *,
        config: AIObservationConfig,
        client: OpenAIObservationClient,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(config, AIObservationConfig):
            raise ConfigurationError("config must be an AIObservationConfig")
        if not config.enabled:
            raise ConfigurationError("OpenAIMessageObserver requires enabled AI observation")
        if not isinstance(client, OpenAIObservationClient):
            raise ConfigurationError("client must be an OpenAIObservationClient")
        self._config = config
        self._client = client
        self._monotonic = monotonic
        self._now = now
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._config.model!r}, closed={self._closed!r})"

    async def observe(
        self,
        snapshot: MessageSnapshot,
        *,
        trusted_area_context: str | None,
    ) -> AIObservationReport:
        started = self._monotonic()
        if self._closed:
            return self._local_failure(
                AIObservationTechnicalStatus.API_ERROR,
                started=started,
            )

        timeout_seconds = self._config.operation_timeout_seconds
        deadline = started + timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return self._local_failure(
                        AIObservationTechnicalStatus.TIMEOUT,
                        started=started,
                    )

                try:
                    request = self._build_request(
                        snapshot,
                        trusted_area_context=trusted_area_context,
                    )
                    outcome = await self._client.classify(
                        request,
                        timeout_seconds=min(remaining, timeout_seconds),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return self._local_failure(
                        AIObservationTechnicalStatus.API_ERROR,
                        started=started,
                    )

                if deadline - self._monotonic() <= 0:
                    return self._local_failure(
                        AIObservationTechnicalStatus.TIMEOUT,
                        started=started,
                    )
                return self._from_client_outcome(outcome, started=started)
        except TimeoutError:
            return self._local_failure(
                AIObservationTechnicalStatus.TIMEOUT,
                started=started,
            )

    def _build_request(
        self,
        snapshot: MessageSnapshot,
        *,
        trusted_area_context: str | None,
    ):
        # Local import avoids exposing an input-bearing object through observer reprs.
        from .openai_client import AIObservationRequest

        sent_at = snapshot.date
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        message_age_seconds = max(
            0,
            int((now.astimezone(UTC) - sent_at.astimezone(UTC)).total_seconds()),
        )
        return AIObservationRequest(
            message_text=snapshot.text,
            sent_at=sent_at,
            message_age_seconds=message_age_seconds,
            trusted_area_context=trusted_area_context,
            matched_keywords=snapshot.matched_keywords,
            notify_all=snapshot.notify_all,
        )

    def _from_client_outcome(
        self,
        outcome: AIObservationSuccess | AIObservationFailure,
        *,
        started: float,
    ) -> AIObservationReport:
        elapsed_seconds = self._elapsed_seconds(started)
        if isinstance(outcome, AIObservationSuccess):
            return AIObservationReport(
                result=outcome.result,
                status=None,
                model=outcome.model,
                prompt_hash=outcome.prompt_hash,
                elapsed_seconds=elapsed_seconds,
                api_latency_seconds=outcome.api_latency_seconds,
                attempts=outcome.attempts,
                token_usage=outcome.token_usage,
            )
        if isinstance(outcome, AIObservationFailure):
            return AIObservationReport(
                result=None,
                status=outcome.status,
                model=outcome.model,
                prompt_hash=outcome.prompt_hash,
                elapsed_seconds=elapsed_seconds,
                api_latency_seconds=outcome.api_latency_seconds,
                attempts=outcome.attempts,
                token_usage=None,
            )
        return self._local_failure(
            AIObservationTechnicalStatus.API_ERROR,
            started=started,
        )

    def _local_failure(
        self,
        status: AIObservationTechnicalStatus,
        *,
        started: float,
    ) -> AIObservationReport:
        return AIObservationReport(
            result=None,
            status=status,
            model=self._config.model,
            prompt_hash=None,
            elapsed_seconds=self._elapsed_seconds(started),
            api_latency_seconds=None,
            attempts=0,
            token_usage=None,
        )

    def _elapsed_seconds(self, started: float) -> float:
        elapsed = self._monotonic() - started
        if not math.isfinite(elapsed):
            return 0.0
        return max(0.0, round(elapsed, 3))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()


class UnavailableAIObserver:
    """Fail-open observer used when enabled AI setup cannot be completed."""

    def __init__(
        self,
        *,
        config: AIObservationConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._monotonic = monotonic
        self._closed = False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._config.model!r}, closed={self._closed!r})"

    async def observe(
        self,
        snapshot: MessageSnapshot,
        *,
        trusted_area_context: str | None,
    ) -> AIObservationReport:
        del snapshot, trusted_area_context
        started = self._monotonic()
        return AIObservationReport(
            result=None,
            status=AIObservationTechnicalStatus.API_ERROR,
            model=self._config.model,
            prompt_hash=None,
            elapsed_seconds=max(0.0, round(self._monotonic() - started, 3)),
            api_latency_seconds=None,
            attempts=0,
            token_usage=None,
        )

    async def close(self) -> None:
        self._closed = True


def build_ai_observer(
    config: AIObservationConfig,
    *,
    client_factory: _ClientFactory = build_openai_observation_client,
) -> AIObserver | None:
    """Build one optional observer without a live API probe.

    A valid disabled configuration performs no credential, prompt-bundle or SDK
    work. Setup failures for enabled observation are normalized fail-open so the
    core Telegram monitor can continue delivering alerts.
    """

    if not isinstance(config, AIObservationConfig):
        raise ConfigurationError("config must be an AIObservationConfig")
    if not config.enabled:
        return None

    try:
        client = client_factory(config)
    except Exception:
        LOGGER.error(
            "AI observation setup failed (status=%s, model=%s)",
            AIObservationTechnicalStatus.API_ERROR.value,
            _safe_log_value(config.model),
        )
        return UnavailableAIObserver(config=config)

    if client is None:
        LOGGER.error(
            "AI observation setup failed (status=%s, model=%s)",
            AIObservationTechnicalStatus.API_ERROR.value,
            _safe_log_value(config.model),
        )
        return UnavailableAIObserver(config=config)
    return OpenAIMessageObserver(config=config, client=client)
