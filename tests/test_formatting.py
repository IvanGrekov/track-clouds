from datetime import UTC, datetime

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

    assert "Джерело: Cloud discussion" in rendered
    assert "Автор:" not in rendered
    assert "Збіги: kubernetes" in rendered
    assert "2026-08-06 15:30:00 EEST" in rendered
    assert "https://t.me/cloud_chat/42" in rendered


def test_notify_all_media_without_caption_has_useful_fallback() -> None:
    rendered = render_notification(
        _message(text="", matched_keywords=(), notify_all=True, has_media=True),
        timezone_name="UTC",
        max_preview_chars=1_000,
    )

    assert "Фільтр: усі повідомлення" in rendered
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
