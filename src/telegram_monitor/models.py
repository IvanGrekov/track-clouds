from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ChatRef: TypeAlias = int | str
NotificationMode: TypeAlias = Literal["saved_messages", "bot"]


class ConfigurationError(ValueError):
    """Raised when local or environment configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SourceRule:
    """Filtering policy for one Telegram dialog.

    ``peer`` is either a public username (with or without ``@``) or the numeric
    dialog ID printed by ``telegram-monitor list-chats``. Keyword matching uses
    case-insensitive substring matching, so a fragment may match part of a word.
    ``keywords_to_skip`` is checked after the positive rule and takes precedence.
    """

    peer: ChatRef
    keywords: tuple[str, ...] | list[str] = ()
    notify_all: bool = False
    label: str | None = None
    keywords_to_skip: tuple[str, ...] | list[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.peer, bool) or not isinstance(self.peer, (int, str)):
            raise ConfigurationError("Source peer must be a Telegram username or numeric dialog ID")
        if isinstance(self.peer, str) and not self.peer.strip():
            raise ConfigurationError("Source peer cannot be empty")

        if isinstance(self.keywords, str) or not isinstance(self.keywords, (tuple, list)):
            raise ConfigurationError("keywords must be a tuple or list, for example ('aws',)")
        if any(not isinstance(keyword, str) for keyword in self.keywords):
            raise ConfigurationError("Every keyword must be a string")
        cleaned_keywords = tuple(keyword.strip() for keyword in self.keywords if keyword.strip())
        object.__setattr__(self, "keywords", cleaned_keywords)

        if isinstance(self.keywords_to_skip, str) or not isinstance(
            self.keywords_to_skip, (tuple, list)
        ):
            raise ConfigurationError(
                "keywords_to_skip must be a tuple or list, for example ('spam',)"
            )
        if any(not isinstance(keyword, str) for keyword in self.keywords_to_skip):
            raise ConfigurationError("Every keywords_to_skip value must be a string")
        cleaned_keywords_to_skip = tuple(
            keyword.strip() for keyword in self.keywords_to_skip if keyword.strip()
        )
        object.__setattr__(self, "keywords_to_skip", cleaned_keywords_to_skip)

        if not isinstance(self.notify_all, bool):
            raise ConfigurationError("notify_all must be True or False")
        if not self.notify_all and not cleaned_keywords:
            raise ConfigurationError(
                f"Source {self.peer!r} needs at least one keyword or notify_all=True"
            )
        if self.label is not None:
            cleaned_label = self.label.strip()
            object.__setattr__(self, "label", cleaned_label or None)


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    sources: tuple[SourceRule, ...]
    notify_to: ChatRef = "me"
    notification_mode: NotificationMode = "saved_messages"
    bot_subscriber_limit: int = 10
    bot_subscriber_database: str = ".state/bot_subscribers.sqlite3"
    timezone: str = "Europe/Kyiv"
    max_preview_chars: int = 2_400
    deduplication_window: int = 2_048
    queue_capacity: int = 1_000
    startup_buffer_capacity: int = 5_000
    delivery_attempts: int = 5
    delivery_retry_base_seconds: float = 1.0
    delivery_retry_max_seconds: float = 30.0
    skip_forwards_from_watched_sources: bool = True

    def validate_for_run(self) -> None:
        if not self.sources:
            raise ConfigurationError(
                "No Telegram sources configured. Add SourceRule entries to config.py"
            )
        if isinstance(self.notify_to, bool) or not isinstance(self.notify_to, (int, str)):
            raise ConfigurationError("notify_to must be a Telegram username or numeric dialog ID")
        if isinstance(self.notify_to, str) and not self.notify_to.strip():
            raise ConfigurationError("notify_to cannot be empty")
        if self.notification_mode not in ("saved_messages", "bot"):
            raise ConfigurationError("notification_mode must be 'saved_messages' or 'bot'")
        if (
            isinstance(self.bot_subscriber_limit, bool)
            or not isinstance(self.bot_subscriber_limit, int)
            or not 1 <= self.bot_subscriber_limit <= 10
        ):
            raise ConfigurationError("bot_subscriber_limit must be between 1 and 10")
        if (
            not isinstance(self.bot_subscriber_database, str)
            or not self.bot_subscriber_database.strip()
        ):
            raise ConfigurationError("bot_subscriber_database must be a non-empty path")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(f"Unknown timezone: {self.timezone}") from error
        if not 100 <= self.max_preview_chars <= 3_500:
            raise ConfigurationError("max_preview_chars must be between 100 and 3500")
        if self.deduplication_window < 1:
            raise ConfigurationError("deduplication_window must be positive")
        if self.queue_capacity < 1:
            raise ConfigurationError("queue_capacity must be positive")
        if self.startup_buffer_capacity < 1:
            raise ConfigurationError("startup_buffer_capacity must be positive")
        if self.delivery_attempts < 1:
            raise ConfigurationError("delivery_attempts must be positive")
        if self.delivery_retry_base_seconds < 0:
            raise ConfigurationError("delivery_retry_base_seconds cannot be negative")
        if self.delivery_retry_max_seconds < self.delivery_retry_base_seconds:
            raise ConfigurationError(
                "delivery_retry_max_seconds cannot be smaller than delivery_retry_base_seconds"
            )
        if not isinstance(self.skip_forwards_from_watched_sources, bool):
            raise ConfigurationError("skip_forwards_from_watched_sources must be boolean")


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    rule: SourceRule
    peer_id: int
    title: str
    username: str | None
    entity: object


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    source_title: str
    sender_name: str
    text: str
    message_id: int
    peer_id: int
    date: datetime
    matched_keywords: tuple[str, ...]
    notify_all: bool
    username: str | None = None
    has_media: bool = False
