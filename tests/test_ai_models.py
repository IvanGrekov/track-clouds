from __future__ import annotations

import json

import pytest

from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
    AIResponseValidationError,
    parse_ai_observation_response,
)


def _accept_payload() -> dict[str, object]:
    """Return the complete strict-schema representation of an ACCEPT result."""

    return {
        "decision": "accept",
        "location": "Городоцька, біля цирку",
        "event": "перекрита права смуга",
        "reason_code": None,
        "reason": None,
    }


def _reject_payload() -> dict[str, object]:
    """Return the complete strict-schema representation of a REJECT result."""

    return {
        "decision": "reject",
        "location": None,
        "event": None,
        "reason_code": "spam_or_scam",
        "reason": "Повідомлення є рекламою шахрайської схеми.",
    }


def test_parse_ai_observation_response_returns_typed_immutable_result() -> None:
    payload = _accept_payload()
    payload["location"] = "  Городоцька, біля цирку  "

    result = parse_ai_observation_response(payload)

    assert result == AIObservationResult(
        decision=AIDecision.ACCEPT,
        location="Городоцька, біля цирку",
        event="перекрита права смуга",
        reason_code=None,
        reason=None,
    )
    with pytest.raises(AttributeError):
        result.location = "changed"  # type: ignore[misc]


def test_parse_ai_observation_response_accepts_both_semantic_decisions() -> None:
    accepted = parse_ai_observation_response(_accept_payload())
    rejected = parse_ai_observation_response(_reject_payload())

    assert accepted.decision is AIDecision.ACCEPT
    assert rejected.decision is AIDecision.REJECT
    assert rejected.reason_code is AIReasonCode.SPAM_OR_SCAM


def test_decision_contract_contains_only_accept_and_reject() -> None:
    assert {decision.value for decision in AIDecision} == {"accept", "reject"}


def test_reason_code_contract_contains_only_reject_reasons() -> None:
    assert {reason_code.value for reason_code in AIReasonCode} == {
        "spam_or_scam",
        "unrelated_content",
        "only_opinion_or_emotion",
        "political_commentary",
    }


def test_unrelated_content_is_a_reject_reason() -> None:
    payload = _reject_payload()
    payload.update(
        reason_code="unrelated_content",
        reason="Повідомлення не стосується стану маршруту.",
    )

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REJECT
    assert result.reason_code is AIReasonCode.UNRELATED_CONTENT


def test_notify_all_accepts_decision_without_a_reason_code() -> None:
    result = parse_ai_observation_response({"decision": "accept"}, notify_all=True)

    assert result == AIObservationResult(decision=AIDecision.ACCEPT)


def test_notify_all_requires_accept_decision() -> None:
    with pytest.raises(AIResponseValidationError, match="notify_all.*decision accept"):
        parse_ai_observation_response({"decision": "reject"}, notify_all=True)


def test_parse_ai_observation_response_accepts_json_text() -> None:
    result = parse_ai_observation_response(json.dumps(_accept_payload(), ensure_ascii=False))

    assert result.decision is AIDecision.ACCEPT


@pytest.mark.parametrize("payload", ["{invalid", '{"decision":NaN}'])
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
    }
    assert not (
        {status.value for status in AIObservationTechnicalStatus}
        & {item.value for item in AIDecision}
    )


@pytest.mark.parametrize("payload", [None, [], 42])
def test_parse_ai_observation_response_requires_object(payload: object) -> None:
    with pytest.raises(AIResponseValidationError, match="JSON object"):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "location": None,
            "event": None,
            "reason_code": None,
            "reason": None,
        },
    ],
)
def test_parse_ai_observation_response_requires_decision(
    payload: dict[str, object],
) -> None:
    with pytest.raises(AIResponseValidationError, match="decision"):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize("decision", ["review", "maybe", 1, None])
def test_parse_ai_observation_response_rejects_invalid_decision(decision: object) -> None:
    payload = _accept_payload()
    payload["decision"] = decision

    with pytest.raises(AIResponseValidationError, match="decision"):
        parse_ai_observation_response(payload)


@pytest.mark.parametrize("decision", ["accept", "reject"])
def test_parse_ai_observation_response_accepts_omitted_auxiliary_fields(
    decision: str,
) -> None:
    result = parse_ai_observation_response({"decision": decision})

    assert result.decision.value == decision
    assert result.location is None
    assert result.event is None
    assert result.reason_code is None
    assert result.reason is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("location", 42),
        ("location", True),
        ("location", "  "),
        ("event", 42),
        ("event", True),
        ("event", "  "),
    ],
)
def test_accept_discards_invalid_location_or_event_without_failing(
    field_name: str,
    value: object,
) -> None:
    payload = _accept_payload()
    payload[field_name] = value

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.ACCEPT
    assert getattr(result, field_name) is None


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("reason_code", "unsupported_reason"),
        ("reason_code", 42),
        ("reason_code", "  "),
        ("reason", 42),
        ("reason", True),
        ("reason", "  "),
    ],
)
def test_reject_discards_invalid_reason_fields_without_failing(
    field_name: str,
    value: object,
) -> None:
    payload = _reject_payload()
    payload[field_name] = value

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REJECT
    assert getattr(result, field_name) is None


def test_parse_ai_observation_response_normalizes_valid_branch_fields() -> None:
    accepted = _accept_payload()
    accepted.update(location="  Стрийська  ", event="  ускладнений рух  ")
    rejected = _reject_payload()
    rejected.update(
        reason_code="political_commentary",
        reason="  Політичний коментар без інформації про маршрут.  ",
    )

    accept_result = parse_ai_observation_response(accepted)
    reject_result = parse_ai_observation_response(rejected)

    assert accept_result.location == "Стрийська"
    assert accept_result.event == "ускладнений рух"
    assert reject_result.reason_code is AIReasonCode.POLITICAL_COMMENTARY
    assert reject_result.reason == "Політичний коментар без інформації про маршрут."


def test_accept_discards_reject_only_fields_without_failing() -> None:
    payload = _accept_payload()
    payload.update(
        reason_code="spam_or_scam",
        reason="Ці поля не належать ACCEPT-відповіді.",
    )

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.ACCEPT
    assert result.reason_code is None
    assert result.reason is None


def test_reject_discards_accept_only_fields_without_failing() -> None:
    payload = _reject_payload()
    payload.update(location="Городоцька", event="перекритий рух")

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REJECT
    assert result.location is None
    assert result.event is None


def test_reject_reason_length_does_not_invalidate_the_decision() -> None:
    payload = _reject_payload()
    payload["reason"] = "п" * 1_000

    result = parse_ai_observation_response(payload)

    assert result.decision is AIDecision.REJECT
    assert result.reason == "п" * 1_000


def test_invalid_auxiliary_fields_do_not_echo_raw_model_content() -> None:
    private_marker = "DO_NOT_ECHO_MODEL_OUTPUT_42"
    payload = _reject_payload()
    payload["reason_code"] = private_marker

    result = parse_ai_observation_response(payload)

    assert result.reason_code is None
    assert private_marker not in repr(result)


def test_validation_error_does_not_echo_raw_model_content() -> None:
    private_marker = "DO_NOT_ECHO_MODEL_OUTPUT_42"
    payload = _accept_payload()
    payload["decision"] = private_marker

    with pytest.raises(AIResponseValidationError) as captured:
        parse_ai_observation_response(payload)

    assert private_marker not in str(captured.value)
