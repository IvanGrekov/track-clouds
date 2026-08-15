"""Typed contracts for semantic AI-observation results."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeVar

__all__ = [
    "AI_REASON_MAX_LENGTH",
    "AI_RESPONSE_FIELD_ORDER",
    "AI_RESPONSE_FIELDS",
    "AIDecision",
    "AIObservationResult",
    "AIObservationTechnicalStatus",
    "AIReasonCode",
    "AIResponseValidationError",
    "AITemporalRelevance",
    "parse_ai_observation_response",
]

AI_REASON_MAX_LENGTH: Final = 240
AI_RESPONSE_FIELD_ORDER: Final = (
    "decision",
    "confidence",
    "location",
    "event",
    "temporal_relevance",
    "reason_code",
    "reason",
)
AI_RESPONSE_FIELDS: Final = frozenset(AI_RESPONSE_FIELD_ORDER)


class AIDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    REVIEW = "review"


class AITemporalRelevance(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    UNCLEAR = "unclear"


class AIReasonCode(StrEnum):
    MEETS_ALL_CRITERIA = "meets_all_criteria"
    SPAM_OR_SCAM = "spam_or_scam"
    UNRELATED_CONTENT = "unrelated_content"
    NO_LOCATION = "no_location"
    NO_EVENT = "no_event"
    ONLY_OPINION_OR_EMOTION = "only_opinion_or_emotion"
    POLITICAL_COMMENTARY = "political_commentary"
    AMBIGUOUS_LOCATION = "ambiguous_location"
    AMBIGUOUS_EVENT = "ambiguous_event"
    AMBIGUOUS_RECENCY = "ambiguous_recency"
    AMBIGUOUS_CONTEXT = "ambiguous_context"
    HISTORICAL_CONTEXT = "historical_context"


class AIObservationTechnicalStatus(StrEnum):
    """Technical outcomes that must never be represented as semantic ``review``."""

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
    confidence: float
    location: str | None
    event: str | None
    temporal_relevance: AITemporalRelevance
    reason_code: AIReasonCode
    reason: str


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


def _parse_nullable_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AIResponseValidationError(
            f"AI response field {field_name!r} must be a string or null"
        )
    cleaned = value.strip()
    if not cleaned:
        raise AIResponseValidationError(
            f"AI response field {field_name!r} cannot be an empty string"
        )
    return cleaned


def _validate_semantics(result: AIObservationResult) -> None:
    review_codes = {
        AIReasonCode.AMBIGUOUS_LOCATION,
        AIReasonCode.AMBIGUOUS_EVENT,
        AIReasonCode.AMBIGUOUS_RECENCY,
        AIReasonCode.AMBIGUOUS_CONTEXT,
        AIReasonCode.HISTORICAL_CONTEXT,
    }
    reject_codes = {
        AIReasonCode.SPAM_OR_SCAM,
        AIReasonCode.UNRELATED_CONTENT,
        AIReasonCode.ONLY_OPINION_OR_EMOTION,
        AIReasonCode.POLITICAL_COMMENTARY,
    }
    missing_component_codes = {
        AIReasonCode.NO_LOCATION,
        AIReasonCode.NO_EVENT,
    }

    if result.reason_code in review_codes and result.decision is not AIDecision.REVIEW:
        raise AIResponseValidationError("AI review reason_code requires decision review")
    if result.reason_code in reject_codes and result.decision is not AIDecision.REJECT:
        raise AIResponseValidationError("AI reject reason_code requires decision reject")
    if result.reason_code in missing_component_codes and result.decision not in {
        AIDecision.REJECT,
        AIDecision.REVIEW,
    }:
        raise AIResponseValidationError(
            "AI missing-component reason_code requires decision reject or review"
        )

    if result.decision is AIDecision.ACCEPT:
        if result.reason_code is not AIReasonCode.MEETS_ALL_CRITERIA:
            raise AIResponseValidationError(
                "AI accept response must use reason_code meets_all_criteria"
            )
        if result.location is None or result.event is None:
            raise AIResponseValidationError(
                "AI accept response must include both location and event"
            )
        if result.temporal_relevance is not AITemporalRelevance.CURRENT:
            raise AIResponseValidationError(
                "AI accept response must have current temporal relevance"
            )
    elif result.reason_code is AIReasonCode.MEETS_ALL_CRITERIA:
        raise AIResponseValidationError(
            "AI reason_code meets_all_criteria is valid only for accept"
        )

    if result.reason_code is AIReasonCode.NO_LOCATION and result.location is not None:
        raise AIResponseValidationError("AI no_location response must have a null location")
    if result.reason_code is AIReasonCode.NO_EVENT and result.event is not None:
        raise AIResponseValidationError("AI no_event response must have a null event")
    if (
        result.reason_code is AIReasonCode.HISTORICAL_CONTEXT
        and result.temporal_relevance is not AITemporalRelevance.HISTORICAL
    ):
        raise AIResponseValidationError(
            "AI historical_context response must have historical temporal relevance"
        )
    if (
        result.reason_code is AIReasonCode.AMBIGUOUS_RECENCY
        and result.temporal_relevance is not AITemporalRelevance.UNCLEAR
    ):
        raise AIResponseValidationError(
            "AI ambiguous_recency response must have unclear temporal relevance"
        )


def parse_ai_observation_response(payload: object) -> AIObservationResult:
    """Parse one strict Structured Outputs payload without coercing field values.

    Error messages intentionally describe only the violated contract and never echo
    the raw model output.
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
    if frozenset(payload) != AI_RESPONSE_FIELDS:
        raise AIResponseValidationError("AI response must contain exactly the expected fields")

    decision = _parse_enum(payload["decision"], AIDecision, field_name="decision")

    raw_confidence = payload["confidence"]
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        raise AIResponseValidationError("AI response field 'confidence' must be a number")
    confidence = float(raw_confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AIResponseValidationError(
            "AI response field 'confidence' must be finite and between 0 and 1"
        )

    location = _parse_nullable_text(payload["location"], field_name="location")
    event = _parse_nullable_text(payload["event"], field_name="event")
    temporal_relevance = _parse_enum(
        payload["temporal_relevance"],
        AITemporalRelevance,
        field_name="temporal_relevance",
    )
    reason_code = _parse_enum(
        payload["reason_code"],
        AIReasonCode,
        field_name="reason_code",
    )

    raw_reason = payload["reason"]
    if not isinstance(raw_reason, str):
        raise AIResponseValidationError("AI response field 'reason' must be a string")
    reason = raw_reason.strip()
    if not reason:
        raise AIResponseValidationError("AI response field 'reason' cannot be empty")
    if len(reason) > AI_REASON_MAX_LENGTH:
        raise AIResponseValidationError(
            f"AI response field 'reason' cannot exceed {AI_REASON_MAX_LENGTH} characters"
        )

    result = AIObservationResult(
        decision=decision,
        confidence=confidence,
        location=location,
        event=event,
        temporal_relevance=temporal_relevance,
        reason_code=reason_code,
        reason=reason,
    )
    _validate_semantics(result)
    return result
