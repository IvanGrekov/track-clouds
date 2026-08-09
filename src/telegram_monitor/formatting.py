from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ConfigurationError, MessageSnapshot

TELEGRAM_MESSAGE_LIMIT = 4_096


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


def render_notification(
    message: MessageSnapshot,
    *,
    timezone_name: str,
    max_preview_chars: int,
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

    trailer = f"\n\n{details}{footer}"
    available_preview = TELEGRAM_MESSAGE_LIMIT - len(trailer)
    preview = _truncate(preview, max(1, available_preview))
    return _truncate(preview, TELEGRAM_MESSAGE_LIMIT - len(trailer)) + trailer
