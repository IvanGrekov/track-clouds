from __future__ import annotations

import json
import math

import pytest

from telegram_monitor.ai_models import (
    AI_REASON_MAX_LENGTH,
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
    AIResponseValidationError,
    AITemporalRelevance,
    parse_ai_observation_response,
)


def _accept_payload() -> dict[str, object]:
    return {
        "decision": "accept",
        "confidence": 0.96,
        "location": "Городоцька, біля цирку",
        "event": "перекрита права смуга",
        "temporal_relevance": "current",
        "reason_code": "meets_all_criteria",
        "reason": "Є актуальна подія та достатньо конкретна локація.",
    }


def test_parse_ai_observation_response_returns_typed_immutable_result() -> None:
    payload = _accept_payload()
    payload["location"] = "  Городоцька, біля цирку  "

    result = parse_ai_observation_response(payload)

    assert result == AIObservationResult(
        decision=AIDecision.ACCEPT,
        confidence=0.96,
        location="Городоцька, біля цирку",
        event="перекрита права смуга",
        temporal_relevance=AITemporalRelevance.CURRENT,
        reason_code=AIReasonCode.MEETS_ALL_CRITERIA,
        reason="Є актуальна подія та достатньо конкретна локація.",
    )
    with pytest.raises(AttributeError):
        result.reason = "changed"  # type: ignore[misc]


def test_parse_ai_observation_response_accepts_reject_and_review() -> None:
    rejected = _accept_payload()
    rejected.update(
        decision="reject",
        location=None,
        reason_code="no_location",
        reason="Локація відсутня.",
    )
    review = _accept_payload()
    review.update(
        decision="review",
        location=None,
        temporal_relevance="unclear",
        reason_code="ambiguous_location",
        reason="Локація може міститися у відсутньому контексті.",
    )

    assert parse_ai_observation_response(rejected).decision is AIDecision.REJECT
    assert parse_ai_observation_response(review).decision is AIDecision.REVIEW


def test_reason_code_contract_includes_unrelated_content() -> None:
    assert {reason_code.value for reason_code in AIReasonCode} == {
        "meets_all_criteria",
        "spam_or_scam",
        "no_location",
        "no_event",
        "unrelated_content",
        "only_opinion_or_emotion",
        "political_commentary",
        "ambiguous_location",
        "ambiguous_event",
        "ambiguous_recency",
        "ambiguous_context",
        "historical_context",
    }


def test_unrelated_content_is_a_reject_reason() -> None:
    payload = _accept_payload()
    payload.update(
        decision="reject",
        reason_code="unrelated_content",
        reason="Повідомлення не стосується стану маршруту.",
    )

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REJECT
    assert result.reason_code is AIReasonCode.UNRELATED_CONTENT


@pytest.mark.parametrize("decision", ["review", "reject"])
@pytest.mark.parametrize(
    ("reason_code", "null_field"),
    [("no_location", "location"), ("no_event", "event")],
)
def test_missing_required_component_may_be_review_or_reject(
    decision: str,
    reason_code: str,
    null_field: str,
) -> None:
    payload = _accept_payload()
    payload.update(
        decision=decision,
        reason_code=reason_code,
        temporal_relevance="unclear" if decision == "review" else "current",
        reason="Обов’язковий компонент відсутній або залежить від контексту.",
    )
    payload[null_field] = None

    result = parse_ai_observation_response(payload)

    assert result.decision.value == decision
    assert result.reason_code.value == reason_code
    assert getattr(result, null_field) is None


def test_historical_context_remains_review_with_historical_relevance() -> None:
    payload = _accept_payload()
    payload.update(
        decision="review",
        temporal_relevance="historical",
        reason_code="historical_context",
        reason="Повідомлення може бути корисним для подальшого аналізу.",
    )

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REVIEW
    assert result.temporal_relevance is AITemporalRelevance.HISTORICAL
    assert result.reason_code is AIReasonCode.HISTORICAL_CONTEXT


def test_parse_ai_observation_response_accepts_json_text() -> None:
    result = parse_ai_observation_response(json.dumps(_accept_payload(), ensure_ascii=False))

    assert result.decision is AIDecision.ACCEPT


@pytest.mark.parametrize("payload", ["{invalid", '{"confidence":NaN}'])
def test_parse_ai_observation_response_rejects_invalid_json(payload: str) -> None:
    with pytest.raises(AIResponseValidationError, match="valid JSON"):
        parse_ai_observation_response(payload)


def test_technical_statuses_are_separate_from_semantic_decisions() -> None:
    assert {status.value for status in AIObservationTechnicalStatus} == {
        "timeout",
        "rate_limited",
        "refusal",
        "api_error",
        "invalid_response",
        "reply_context_error",
    }
    assert not (
        {status.value for status in AIObservationTechnicalStatus}
        & {item.value for item in AIDecision}
    )


@pytest.mark.parametrize("payload", [None, [], 42])
def test_parse_ai_observation_response_requires_object(payload: object) -> None:
    with pytest.raises(AIResponseValidationError, match="JSON object"):
        parse_ai_observation_response(payload)


def test_parse_ai_observation_response_requires_exact_fields() -> None:
    missing = _accept_payload()
    missing.pop("reason")
    extra = _accept_payload()
    extra["unexpected"] = "value"

    for payload in (missing, extra):
        with pytest.raises(AIResponseValidationError, match="exactly"):
            parse_ai_observation_response(payload)


@pytest.mark.parametrize("confidence", [True, "0.9", None, math.nan, math.inf, -0.01, 1.01])
def test_parse_ai_observation_response_rejects_invalid_confidence(confidence: object) -> None:
    payload = _accept_payload()
    payload["confidence"] = confidence

    with pytest.raises(AIResponseValidationError, match="confidence"):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("decision", "maybe"),
        ("decision", 1),
        ("temporal_relevance", "stale"),
        ("reason_code", "unsupported_reason"),
    ],
)
def test_parse_ai_observation_response_rejects_invalid_enums(
    field_name: str,
    value: object,
) -> None:
    payload = _accept_payload()
    payload[field_name] = value

    with pytest.raises(AIResponseValidationError, match=field_name):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize("field_name", ["location", "event"])
@pytest.mark.parametrize("value", [42, True, "  "])
def test_parse_ai_observation_response_rejects_invalid_nullable_text(
    field_name: str,
    value: object,
) -> None:
    payload = _accept_payload()
    payload[field_name] = value

    with pytest.raises(AIResponseValidationError, match=field_name):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize("reason", [None, 42, "", "  ", "а" * (AI_REASON_MAX_LENGTH + 1)])
def test_parse_ai_observation_response_rejects_invalid_reason(reason: object) -> None:
    payload = _accept_payload()
    payload["reason"] = reason

    with pytest.raises(AIResponseValidationError, match="reason"):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"location": None}, "location and event"),
        ({"event": None}, "location and event"),
        ({"temporal_relevance": "historical"}, "current"),
        (
            {"reason_code": "no_location", "location": None},
            "reason_code|accept|reject or review",
        ),
        ({"reason_code": "no_event", "event": None}, "reason_code|accept|review or reject"),
        ({"reason_code": "unrelated_content"}, "reject reason_code"),
        (
            {
                "decision": "reject",
                "location": None,
                "reason_code": "meets_all_criteria",
            },
            "only for accept",
        ),
    ],
)
def test_parse_ai_observation_response_enforces_accept_consistency(
    updates: dict[str, object],
    message: str,
) -> None:
    payload = _accept_payload()
    payload.update(updates)

    with pytest.raises(AIResponseValidationError, match=message):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"decision": "reject", "reason_code": "ambiguous_context"},
            "review reason_code",
        ),
        (
            {"decision": "review", "reason_code": "spam_or_scam"},
            "reject reason_code",
        ),
        (
            {"decision": "review", "reason_code": "unrelated_content"},
            "reject reason_code",
        ),
        (
            {"decision": "reject", "reason_code": "no_location"},
            "null location",
        ),
        (
            {"decision": "review", "reason_code": "no_location"},
            "null location",
        ),
        (
            {"decision": "reject", "reason_code": "no_event"},
            "null event",
        ),
        (
            {"decision": "review", "reason_code": "no_event"},
            "null event",
        ),
        (
            {
                "decision": "review",
                "reason_code": "historical_context",
                "temporal_relevance": "current",
            },
            "historical temporal",
        ),
        (
            {
                "decision": "reject",
                "reason_code": "historical_context",
                "temporal_relevance": "historical",
            },
            "review reason_code",
        ),
        (
            {
                "decision": "review",
                "reason_code": "ambiguous_recency",
                "temporal_relevance": "current",
            },
            "unclear temporal",
        ),
    ],
)
def test_parse_ai_observation_response_enforces_reason_code_consistency(
    updates: dict[str, object],
    message: str,
) -> None:
    payload = _accept_payload()
    payload.update(updates)

    with pytest.raises(AIResponseValidationError, match=message):
        parse_ai_observation_response(payload)


def test_validation_error_does_not_echo_raw_model_content() -> None:
    private_marker = "DO_NOT_ECHO_MODEL_OUTPUT_42"
    payload = _accept_payload()
    payload["decision"] = private_marker

    with pytest.raises(AIResponseValidationError) as captured:
        parse_ai_observation_response(payload)

    assert private_marker not in str(captured.value)
