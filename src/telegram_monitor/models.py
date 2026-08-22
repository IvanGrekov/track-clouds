from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ChatRef: TypeAlias = int | str
NotificationMode: TypeAlias = Literal["saved_messages", "bot"]
AIReasoningEffort: TypeAlias = Literal["none", "low", "medium", "high", "xhigh"]

_AI_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})


class ConfigurationError(ValueError):
    """Raised when local or environment configuration is invalid."""


def _parse_quiet_hours_clock(value: object, field_name: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ConfigurationError(f"quiet_hours.{field_name} must use HH:MM format")
    cleaned = value.strip()
    parts = cleaned.split(":")
    if (
        len(parts) != 2
        or len(parts[0]) != 2
        or len(parts[1]) != 2
        or not all(part.isascii() and part.isdigit() for part in parts)
    ):
        raise ConfigurationError(f"quiet_hours.{field_name} must use HH:MM format")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigurationError(f"quiet_hours.{field_name} must be a valid UTC time")
    return f"{hour:02d}:{minute:02d}", hour * 60 + minute


@dataclass(frozen=True, slots=True)
class QuietHoursConfig:
    """Daily interval during which all external monitor activity is suspended."""

    enabled: bool = False
    start: str = "22:30"
    end: str = "04:00"
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("quiet_hours.enabled must be boolean")

        start, start_minutes = _parse_quiet_hours_clock(self.start, "start")
        end, end_minutes = _parse_quiet_hours_clock(self.end, "end")
        if start_minutes == end_minutes:
            raise ConfigurationError("quiet_hours.start and quiet_hours.end must be different")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise ConfigurationError("quiet_hours.timezone must be a non-empty timezone name")
        timezone_name = self.timezone.strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ConfigurationError(f"Unknown quiet_hours.timezone: {timezone_name}") from error
        object.__setattr__(self, "timezone", timezone_name)

    @property
    def _start_minutes(self) -> int:
        hour, minute = (int(part) for part in self.start.split(":"))
        return hour * 60 + minute

    @property
    def _end_minutes(self) -> int:
        hour, minute = (int(part) for part in self.end.split(":"))
        return hour * 60 + minute

    def contains(self, value: datetime) -> bool:
        if not self.enabled:
            return False
        if value.tzinfo is None:
            raise ValueError("Quiet-hours timestamps must be timezone-aware")
        local = value.astimezone(ZoneInfo(self.timezone))
        current_minutes = local.hour * 60 + local.minute
        start = self._start_minutes
        end = self._end_minutes
        if start < end:
            return start <= current_minutes < end
        return current_minutes >= start or current_minutes < end

    def next_transition(self, value: datetime) -> datetime:
        """Return the next quiet-hours boundary as an aware UTC datetime."""

        if not self.enabled:
            raise ValueError("Quiet hours are disabled")
        if value.tzinfo is None:
            raise ValueError("Quiet-hours timestamps must be timezone-aware")
        zone = ZoneInfo(self.timezone)
        local = value.astimezone(zone)
        target = self.end if self.contains(value) else self.start
        target_hour, target_minute = (int(part) for part in target.split(":"))

        def boundary_for(day: date) -> datetime:
            return datetime.combine(day, time(target_hour, target_minute), tzinfo=zone)

        boundary = boundary_for(local.date())
        if boundary <= local:
            boundary = boundary_for(local.date() + timedelta(days=1))
        return boundary.astimezone(UTC)

    def most_recent_end(self, value: datetime) -> datetime:
        """Return the latest end boundary, used as the active-window replay cutoff."""

        if not self.enabled:
            raise ValueError("Quiet hours are disabled")
        if value.tzinfo is None:
            raise ValueError("Quiet-hours timestamps must be timezone-aware")
        zone = ZoneInfo(self.timezone)
        local = value.astimezone(zone)
        end_hour, end_minute = (int(part) for part in self.end.split(":"))

        def boundary_for(day: date) -> datetime:
            return datetime.combine(day, time(end_hour, end_minute), tzinfo=zone)

        boundary = boundary_for(local.date())
        if boundary > local:
            boundary = boundary_for(local.date() - timedelta(days=1))
        return boundary.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AIObservationConfig:
    """Runtime settings for the optional semantic observer."""

    enabled: bool = False
    model: str = "gpt-5.4-nano-2026-03-17"
    prompt_bundle_path: str | Path = Path("prompts")
    policy_prompt_path: str | Path = Path("policy-prompt.txt")
    default_trusted_area_context: str | None = None
    operation_timeout_seconds: float = 30.0
    request_attempts: int = 2
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 2.0
    reasoning_effort: AIReasoningEffort = "medium"
    max_output_tokens: int = 800
    store_responses: bool = False
    policy_prompt_extended_examples_path: str | Path = Path("policy-prompt-extended-examples.txt")

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationError("ai_observation.enabled must be boolean")

        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigurationError("ai_observation.model must be a non-empty string")
        object.__setattr__(self, "model", self.model.strip())

        for field_name in (
            "prompt_bundle_path",
            "policy_prompt_path",
            "policy_prompt_extended_examples_path",
        ):
            configured_path = getattr(self, field_name)
            if isinstance(configured_path, str):
                if not configured_path.strip():
                    raise ConfigurationError(
                        f"ai_observation.{field_name} must be a non-empty path"
                    )
                configured_path = Path(configured_path.strip()).expanduser()
            elif not isinstance(configured_path, Path):
                raise ConfigurationError(f"ai_observation.{field_name} must be a path")
            object.__setattr__(self, field_name, configured_path)

        if self.default_trusted_area_context is not None:
            if not isinstance(self.default_trusted_area_context, str):
                raise ConfigurationError(
                    "ai_observation.default_trusted_area_context must be a string"
                )
            cleaned_context = self.default_trusted_area_context.strip()
            object.__setattr__(
                self,
                "default_trusted_area_context",
                cleaned_context or None,
            )

        timeout = self.operation_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise ConfigurationError(
                "ai_observation.operation_timeout_seconds must be greater than 0 and at most 30"
            )
        object.__setattr__(self, "operation_timeout_seconds", float(timeout))

        if (
            isinstance(self.request_attempts, bool)
            or not isinstance(self.request_attempts, int)
            or not 1 <= self.request_attempts <= 3
        ):
            raise ConfigurationError("ai_observation.request_attempts must be between 1 and 3")

        retry_base = self.retry_base_seconds
        retry_max = self.retry_max_seconds
        for field_name, value in (
            ("retry_base_seconds", retry_base),
            ("retry_max_seconds", retry_max),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 30
            ):
                raise ConfigurationError(f"ai_observation.{field_name} must be between 0 and 30")
            object.__setattr__(self, field_name, float(value))
        if retry_max < retry_base:
            raise ConfigurationError(
                "ai_observation.retry_max_seconds cannot be smaller than retry_base_seconds"
            )
        if retry_max > timeout:
            raise ConfigurationError(
                "ai_observation.retry_max_seconds cannot exceed operation_timeout_seconds"
            )

        if (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in _AI_REASONING_EFFORTS
        ):
            choices = ", ".join(sorted(_AI_REASONING_EFFORTS))
            raise ConfigurationError(f"ai_observation.reasoning_effort must be one of: {choices}")

        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or not 128 <= self.max_output_tokens <= 2_048
        ):
            raise ConfigurationError(
                "ai_observation.max_output_tokens must be between 128 and 2048"
            )
        if not isinstance(self.store_responses, bool):
            raise ConfigurationError("ai_observation.store_responses must be boolean")


@dataclass(frozen=True, slots=True)
class SourceRule:
    """Filtering policy for one Telegram dialog.

    ``peer`` is either a public username (with or without ``@``) or the numeric
    dialog ID printed by ``telegram-monitor list-chats``. Keyword matching uses
    case-insensitive substring matching, so a fragment may match part of a word.
    ``keywords_to_skip`` is checked after the positive rule and takes precedence.
    ``skip_ai`` bypasses AI observation only after these deterministic filters accept
    the message; it does not replace keyword filtering.
    """

    peer: ChatRef
    keywords: tuple[str, ...] | list[str] = ()
    notify_all: bool = False
    label: str | None = None
    keywords_to_skip: tuple[str, ...] | list[str] = ()
    trusted_area_context: str | None = None
    skip_ai: bool = False

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
        if not isinstance(self.skip_ai, bool):
            raise ConfigurationError("skip_ai must be True or False")
        if not self.notify_all and not cleaned_keywords:
            raise ConfigurationError(
                f"Source {self.peer!r} needs at least one keyword or notify_all=True"
            )
        if self.label is not None:
            cleaned_label = self.label.strip()
            object.__setattr__(self, "label", cleaned_label or None)
        if self.trusted_area_context is not None:
            if not isinstance(self.trusted_area_context, str):
                raise ConfigurationError("trusted_area_context must be a string")
            cleaned_context = self.trusted_area_context.strip()
            object.__setattr__(self, "trusted_area_context", cleaned_context or None)


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
    ai_observation: AIObservationConfig = field(default_factory=AIObservationConfig)
    quiet_hours: QuietHoursConfig = field(default_factory=QuietHoursConfig)

    def trusted_area_context_for(self, source: SourceRule) -> str | None:
        """Return the source-specific context, falling back to the AI default."""

        return (
            source.trusted_area_context
            if source.trusted_area_context is not None
            else self.ai_observation.default_trusted_area_context
        )

    def validate_for_run(self) -> None:
        if not self.sources:
            raise ConfigurationError(
                "No Telegram sources configured. Add [[sources]] entries to config.toml"
            )
        if not isinstance(self.ai_observation, AIObservationConfig):
            raise ConfigurationError("ai_observation must be an AIObservationConfig")
        if not isinstance(self.quiet_hours, QuietHoursConfig):
            raise ConfigurationError("quiet_hours must be a QuietHoursConfig")
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
