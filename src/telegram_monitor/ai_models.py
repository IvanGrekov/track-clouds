"""Typed contracts for semantic AI-observation results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeVar

__all__ = [
    "AI_RESPONSE_FIELD_ORDER",
    "AI_RESPONSE_FIELDS",
    "AIDecision",
    "AIObservationResult",
    "AIObservationTechnicalStatus",
    "AIReasonCode",
    "AIResponseValidationError",
    "parse_ai_observation_response",
]

AI_RESPONSE_FIELD_ORDER: Final = (
    "decision",
    "location",
    "event",
    "reason_code",
    "reason",
)
AI_RESPONSE_FIELDS: Final = frozenset(AI_RESPONSE_FIELD_ORDER)


class AIDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"


class AIReasonCode(StrEnum):
    SPAM_OR_SCAM = "spam_or_scam"
    UNRELATED_CONTENT = "unrelated_content"
    ONLY_OPINION_OR_EMOTION = "only_opinion_or_emotion"
    POLITICAL_COMMENTARY = "political_commentary"


class AIObservationTechnicalStatus(StrEnum):
    """Technical outcomes kept separate from semantic accept/reject decisions."""

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    REFUSAL = "refusal"
    API_ERROR = "api_error"
    INVALID_RESPONSE = "invalid_response"


class AIResponseValidationError(ValueError):
    """Raised when model output violates the response or semantic contract."""


@dataclass(frozen=True, slots=True)
class AIObservationResult:
    decision: AIDecision
    location: str | None = None
    event: str | None = None
    reason_code: AIReasonCode | None = None
    reason: str | None = None


_EnumT = TypeVar("_EnumT", bound=StrEnum)


def _reject_nonstandard_json_constant(_value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _parse_enum(value: object, enum_type: type[_EnumT], *, field_name: str) -> _EnumT:
    if not isinstance(value, str):
        raise AIResponseValidationError(f"AI response field {field_name!r} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise AIResponseValidationError(
            f"AI response field {field_name!r} contains an unsupported value"
        ) from error


def _normalize_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_optional_reason_code(value: object) -> AIReasonCode | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return AIReasonCode(cleaned)
    except ValueError:
        return None


def _validate_semantics(
    result: AIObservationResult,
    *,
    notify_all: bool,
) -> None:
    if notify_all and result.decision is not AIDecision.ACCEPT:
        raise AIResponseValidationError("AI notify_all response requires decision accept")


def parse_ai_observation_response(
    payload: object,
    *,
    notify_all: bool = False,
) -> AIObservationResult:
    """Parse one Structured Outputs payload, requiring only a valid decision.

    Auxiliary values are best-effort metadata: missing or unusable values normalize
    to ``None``, and values irrelevant to the selected decision are discarded. Error
    messages never echo the raw model output.
    """

    if isinstance(payload, str):
        try:
            payload = json.loads(
                payload,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise AIResponseValidationError("AI response must contain valid JSON") from error

    if not isinstance(payload, Mapping):
        raise AIResponseValidationError("AI response must be a JSON object")
    decision = _parse_enum(payload.get("decision"), AIDecision, field_name="decision")

    if decision is AIDecision.ACCEPT:
        location = _normalize_optional_text(payload.get("location"))
        event = _normalize_optional_text(payload.get("event"))
        reason_code = None
        reason = None
    else:
        location = None
        event = None
        reason_code = _normalize_optional_reason_code(payload.get("reason_code"))
        reason = _normalize_optional_text(payload.get("reason"))

    result = AIObservationResult(
        decision=decision,
        location=location,
        event=event,
        reason_code=reason_code,
        reason=reason,
    )
    _validate_semantics(result, notify_all=notify_all)
    return result
