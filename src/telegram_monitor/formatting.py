from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .ai_models import AIObservationTechnicalStatus
from .models import ConfigurationError, MessageSnapshot

if TYPE_CHECKING:
    from .ai_observer import AIObservationReport

TELEGRAM_MESSAGE_LIMIT = 4_096

_TECHNICAL_STATUS_DESCRIPTIONS = {
    AIObservationTechnicalStatus.TIMEOUT: (
        "AI-оцінювання не завершилося в межах установленого ліміту часу."
    ),
    AIObservationTechnicalStatus.RATE_LIMITED: (
        "OpenAI тимчасово обмежив частоту запитів, тому оцінку не отримано."
    ),
    AIObservationTechnicalStatus.REFUSAL: (
        "Модель відмовилася класифікувати повідомлення відповідно до політики безпеки."
    ),
    AIObservationTechnicalStatus.API_ERROR: (
        "Під час звернення до OpenAI сталася технічна помилка, тому оцінку не отримано."
    ),
    AIObservationTechnicalStatus.INVALID_RESPONSE: (
        "Відповідь AI не відповідала очікуваній схемі або правилам узгодженості."
    ),
}


def build_message_link(peer_id: int, message_id: int, username: str | None) -> str | None:
    """Build a public or private-channel Telegram deep link when possible."""

    if username:
        return f"https://t.me/{username.lstrip('@')}/{message_id}"

    peer_text = str(peer_id)
    if peer_text.startswith("-100") and len(peer_text) > 4:
        return f"https://t.me/c/{peer_text[4:]}/{message_id}"
    return None


def _format_time(value: datetime, timezone_name: str) -> str:
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ConfigurationError(f"Unknown timezone: {timezone_name}") from error

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _sanitize_ai_text(value: str | None, limit: int) -> str:
    """Collapse untrusted model text to one safe, bounded Telegram line."""

    if value is None:
        return "—"
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in value
    )
    cleaned = " ".join(without_controls.split())
    return _truncate(cleaned, limit) if cleaned else "—"


def _render_ai_observation(observation: AIObservationReport) -> str:
    result = observation.result
    if result is not None:
        return "\n".join(
            (
                "AI review:",
                f"Decision: {result.decision.value}",
                f"Location: {_sanitize_ai_text(result.location, 256)}",
                f"Event: {_sanitize_ai_text(result.event, 512)}",
                f"Relevance: {result.temporal_relevance.value}",
                f"Code reason: {result.reason_code.value}",
                f"Reason: {_sanitize_ai_text(result.reason, 240)}",
                f"Delay: {observation.elapsed_seconds:.3f} s",
            )
        )

    status = observation.status
    if status is None:  # Defensive fallback; AIObservationReport validates this invariant.
        status = AIObservationTechnicalStatus.INVALID_RESPONSE
    return "\n".join(
        (
            "AI review:",
            f"Status: {status.value}",
            f"Description: {_TECHNICAL_STATUS_DESCRIPTIONS[status]}",
        )
    )


def render_notification(
    message: MessageSnapshot,
    *,
    timezone_name: str,
    max_preview_chars: int,
    ai_observation: AIObservationReport | None = None,
) -> str:
    """Render a plain-text Telegram notification within Telegram's size limit."""

    match_line = (
        "Matches: " + ", ".join(message.matched_keywords)
        if message.matched_keywords
        else "Filter: усі повідомлення"
    )
    source_title = _truncate(message.source_title, 256)
    match_line = _truncate(match_line, 768)
    details = "\n".join(
        (
            f"Source: {source_title}",
            f"Time: {_format_time(message.date, timezone_name)}",
            match_line,
        )
    )

    link = build_message_link(message.peer_id, message.message_id, message.username)
    footer = f"\n\nOpen: {link}" if link else ""
    fallback = "[медіа без підпису]" if message.has_media else "[повідомлення без тексту]"
    preview = _truncate(message.text.strip() or fallback, max_preview_chars)

    ai_section = (
        f"\n\n{_render_ai_observation(ai_observation)}" if ai_observation is not None else ""
    )
    trailer = f"\n\n{details}{ai_section}{footer}"
    available_preview = TELEGRAM_MESSAGE_LIMIT - len(trailer)
    preview = _truncate(preview, max(1, available_preview))
    return _truncate(preview, TELEGRAM_MESSAGE_LIMIT - len(trailer)) + trailer
