from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
    AITemporalRelevance,
)
from telegram_monitor.formatting import (
    TELEGRAM_MESSAGE_LIMIT,
    build_message_link,
    render_notification,
)
from telegram_monitor.models import MessageSnapshot


def _message(**overrides: object) -> MessageSnapshot:
    values = {
        "source_title": "Cloud discussion",
        "sender_name": "Олена",
        "text": "Kubernetes release",
        "message_id": 42,
        "peer_id": -1001234567890,
        "date": datetime(2026, 8, 6, 12, 30, tzinfo=UTC),
        "matched_keywords": ("kubernetes",),
        "notify_all": False,
        "username": "cloud_chat",
        "has_media": False,
    }
    values.update(overrides)
    return MessageSnapshot(**values)  # type: ignore[arg-type]


def _success_observation(
    *,
    location: str | None = "Городоцька, біля цирку",
    event: str | None = "перекрита права смуга",
    reason: str = "Є актуальне обмеження руху та достатньо конкретна локація.",
    elapsed_seconds: float = 0.842,
) -> SimpleNamespace:
    result = AIObservationResult(
        decision=AIDecision.ACCEPT,
        location=location,
        event=event,
        temporal_relevance=AITemporalRelevance.CURRENT,
        reason_code=AIReasonCode.MEETS_ALL_CRITERIA,
        reason=reason,
    )
    return SimpleNamespace(
        result=result,
        status=None,
        model="gpt-5.4-nano-2026-03-17",
        token_usage=object(),
        elapsed_seconds=elapsed_seconds,
    )


def _technical_observation(status: AIObservationTechnicalStatus) -> SimpleNamespace:
    return SimpleNamespace(
        result=None,
        status=status,
        model="gpt-5.4-nano-2026-03-17",
        elapsed_seconds=30.0,
    )


def test_builds_public_and_private_message_links() -> None:
    assert build_message_link(-1001234567890, 42, "@public_chat") == ("https://t.me/public_chat/42")
    assert build_message_link(-1001234567890, 42, None) == "https://t.me/c/1234567890/42"
    assert build_message_link(-12345, 42, None) is None


def test_renders_plain_text_notification_with_local_time() -> None:
    rendered = render_notification(
        _message(),
        timezone_name="Europe/Kyiv",
        max_preview_chars=1_000,
    )

    assert rendered == (
        "Kubernetes release\n\n"
        "Source: Cloud discussion\n"
        "Time: 2026-08-06 15:30:00 EEST\n"
        "Matches: kubernetes\n\n"
        "Open: https://t.me/cloud_chat/42"
    )
    assert "Автор:" not in rendered


def test_notify_all_media_without_caption_has_useful_fallback() -> None:
    rendered = render_notification(
        _message(text="", matched_keywords=(), notify_all=True, has_media=True),
        timezone_name="UTC",
        max_preview_chars=1_000,
    )

    assert "Filter: усі повідомлення" in rendered
    assert "[медіа без підпису]" in rendered


def test_long_notification_is_truncated_to_telegram_limit() -> None:
    rendered = render_notification(
        _message(
            source_title="source" * 1_000,
            text="x" * 10_000,
            matched_keywords=("keyword" * 1_000,),
        ),
        timezone_name="UTC",
        max_preview_chars=3_500,
    )

    assert len(rendered) <= TELEGRAM_MESSAGE_LIMIT
    assert "…" in rendered


def test_renders_successful_ai_observation_without_experiment_metadata() -> None:
    rendered = render_notification(
        _message(text="На Городоцькій біля цирку перекрита права смуга"),
        timezone_name="UTC",
        max_preview_chars=1_000,
        ai_observation=_success_observation(),  # type: ignore[arg-type]
    )

    assert "\n\nAI review:\n" in rendered
    assert "Decision: accept" in rendered
    assert "Location: Городоцька, біля цирку" in rendered
    assert "Event: перекрита права смуга" in rendered
    assert "Relevance: current" in rendered
    assert "Code reason: meets_all_criteria" in rendered
    assert "Reason: Є актуальне обмеження руху" in rendered
    assert "Delay: 0.842 s" in rendered
    assert "Model:" not in rendered
    assert "Policy:" not in rendered
    assert "Tokens:" not in rendered
    assert rendered.endswith("Open: https://t.me/cloud_chat/42")


@pytest.mark.parametrize(
    ("result", "expected_lines"),
    (
        (
            AIObservationResult(
                decision=AIDecision.REJECT,
                location=None,
                event="особиста думка про погоду",
                temporal_relevance=AITemporalRelevance.CURRENT,
                reason_code=AIReasonCode.ONLY_OPINION_OR_EMOTION,
                reason="Повідомлення не описує корисний стан маршруту.",
            ),
            (
                "Decision: reject",
                "Location: —",
                "Code reason: only_opinion_or_emotion",
            ),
        ),
        (
            AIObservationResult(
                decision=AIDecision.REVIEW,
                location="Стрийська",
                event="можлива перешкода",
                temporal_relevance=AITemporalRelevance.UNCLEAR,
                reason_code=AIReasonCode.AMBIGUOUS_RECENCY,
                reason="Неможливо визначити, коли спостерігалася перешкода.",
            ),
            (
                "Decision: review",
                "Relevance: unclear",
                "Code reason: ambiguous_recency",
            ),
        ),
    ),
)
def test_renders_reject_and_review_as_semantic_results(
    result: AIObservationResult,
    expected_lines: tuple[str, ...],
) -> None:
    observation = SimpleNamespace(
        result=result,
        status=None,
        model="gpt-5.4-nano-2026-03-17",
        token_usage=None,
        elapsed_seconds=0.123,
    )

    rendered = render_notification(
        _message(),
        timezone_name="UTC",
        max_preview_chars=1_000,
        ai_observation=observation,  # type: ignore[arg-type]
    )

    for expected_line in expected_lines:
        assert expected_line in rendered
    assert "Status:" not in rendered
    assert rendered.endswith("Open: https://t.me/cloud_chat/42")


@pytest.mark.parametrize(
    ("status", "description"),
    (
        (
            AIObservationTechnicalStatus.TIMEOUT,
            "AI-оцінювання не завершилося в межах установленого ліміту часу.",
        ),
        (
            AIObservationTechnicalStatus.RATE_LIMITED,
            "OpenAI тимчасово обмежив частоту запитів, тому оцінку не отримано.",
        ),
        (
            AIObservationTechnicalStatus.REFUSAL,
            "Модель відмовилася класифікувати повідомлення відповідно до політики безпеки.",
        ),
        (
            AIObservationTechnicalStatus.API_ERROR,
            "Під час звернення до OpenAI сталася технічна помилка, тому оцінку не отримано.",
        ),
        (
            AIObservationTechnicalStatus.INVALID_RESPONSE,
            "Відповідь AI не відповідала очікуваній схемі або правилам узгодженості.",
        ),
    ),
)
def test_renders_safe_technical_ai_observation(
    status: AIObservationTechnicalStatus,
    description: str,
) -> None:
    rendered = render_notification(
        _message(),
        timezone_name="UTC",
        max_preview_chars=1_000,
        ai_observation=_technical_observation(status),  # type: ignore[arg-type]
    )

    assert f"AI review:\nStatus: {status.value}\nDescription: {description}" in rendered
    assert "Decision:" not in rendered
    assert "Location:" not in rendered
    assert "Event:" not in rendered
    assert "Reason:" not in rendered
    assert "Model:" not in rendered
    assert "Policy:" not in rendered
    assert "Delay:" not in rendered
    assert rendered.endswith("Open: https://t.me/cloud_chat/42")


def test_sanitizes_untrusted_ai_text_and_formats_null_as_dash() -> None:
    rendered = render_notification(
        _message(),
        timezone_name="UTC",
        max_preview_chars=1_000,
        ai_observation=_success_observation(  # type: ignore[arg-type]
            location=None,
            event="аварія\n\r\tбіля\u202e мосту",
            reason="  Рух\nускладнено.\x00  ",
        ),
    )

    assert "Location: —" in rendered
    assert "Event: аварія біля мосту" in rendered
    assert "Reason: Рух ускладнено." in rendered
    assert "\x00" not in rendered
    assert "\u202e" not in rendered


def test_ai_notification_preserves_ai_block_and_link_within_telegram_limit() -> None:
    rendered = render_notification(
        _message(
            source_title="source" * 1_000,
            text="x" * 10_000,
            matched_keywords=("keyword" * 1_000,),
        ),
        timezone_name="UTC",
        max_preview_chars=10_000,
        ai_observation=_success_observation(  # type: ignore[arg-type]
            location="location" * 1_000,
            event="event" * 1_000,
            reason="reason" * 1_000,
        ),
    )

    assert len(rendered) <= TELEGRAM_MESSAGE_LIMIT
    assert "\n\nAI review:\nDecision: accept" in rendered
    assert "Delay: 0.842 s" in rendered
    assert rendered.endswith("Open: https://t.me/cloud_chat/42")
