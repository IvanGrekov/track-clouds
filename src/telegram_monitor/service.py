from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telethon import events, utils

from .ai_models import AIDecision, AIObservationTechnicalStatus
from .ai_observer import AIObservationReport, AIObserver
from .deduplication import MessageKey, RecentMessageCache
from .formatting import render_notification
from .models import MessageSnapshot, MonitorConfig
from .notifier import NotificationError, Notifier
from .registry import SourceRegistry

LOGGER = logging.getLogger(__name__)
_SHUTDOWN_DELIVERY_GRACE_SECONDS = 5.0
_AI_RESPONSE_LOG_MAX_CHARS = 16_000
_REJECTED_NOTIFICATION_LOG_MAX_CHARS = 4_096


@dataclass(frozen=True, slots=True)
class _PendingNotification:
    key: MessageKey
    snapshot: MessageSnapshot = field(repr=False)
    trusted_area_context: str | None = field(default=None, repr=False)
    skip_ai: bool = False


class TelegramMonitor:
    """Resolve configured dialogs, consume NewMessage events, and emit alerts."""

    def __init__(
        self,
        client: object,
        config: MonitorConfig,
        notifier: Notifier,
        ai_observer: AIObserver | None = None,
        accept_events_since: datetime | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._notifier = notifier
        self._ai_observer = ai_observer
        if accept_events_since is not None and accept_events_since.tzinfo is None:
            raise ValueError("accept_events_since must be timezone-aware")
        self._accept_events_since = accept_events_since
        self._registry: SourceRegistry | None = None
        self._deduplicator = RecentMessageCache(config.deduplication_window)
        self._message_log_deduplicator = RecentMessageCache(config.deduplication_window)
        self._queue: asyncio.Queue[_PendingNotification] = asyncio.Queue(
            maxsize=config.queue_capacity
        )
        self._startup_buffer: deque[object] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._event_builder: object | None = None
        self._accepting_events = False
        self._closed = False
        self._dropped_notifications = 0

    @property
    def dropped_notifications(self) -> int:
        return self._dropped_notifications

    def start_capture(self) -> None:
        """Register a catch-all handler before the client connects.

        Bot mode includes this account's outgoing messages. Saved Messages mode
        remains incoming-only to prevent notification feedback loops.
        """

        if self._event_builder is not None:
            return
        if self._closed:
            raise RuntimeError("Cannot start a closed Telegram monitor")
        self._config.validate_for_run()
        self._event_builder = (
            events.NewMessage()
            if self._config.notification_mode == "bot"
            else events.NewMessage(incoming=True)
        )
        self._accepting_events = True
        self._client.add_event_handler(self.handle_event, self._event_builder)

    async def prepare(self) -> tuple[str, ...]:
        self.start_capture()
        dialogs = await self._client.get_dialogs()
        self._registry = SourceRegistry.from_dialogs(self._config.sources, dialogs)
        await self._notifier.start()
        self._worker = asyncio.create_task(self._notification_worker(), name="notification-worker")

        buffered_events = tuple(self._startup_buffer)
        self._startup_buffer.clear()
        for index, event in enumerate(buffered_events, start=1):
            await self.handle_event(event)
            if index % 100 == 0:
                await asyncio.sleep(0)

        descriptions = tuple(
            f"{source.title} ({source.peer_id})" for source in self._registry.sources
        )
        LOGGER.info(
            "Monitoring %d Telegram sources: %s",
            len(descriptions),
            ", ".join(descriptions),
        )
        return descriptions

    async def close(self, *, discard_pending: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting_events = False
        if self._event_builder is not None:
            self._client.remove_event_handler(self.handle_event, self._event_builder)
            self._event_builder = None
        self._startup_buffer.clear()

        if self._worker is not None:
            if not discard_pending:
                flush_timeout = (
                    self._config.ai_observation.operation_timeout_seconds
                    + _SHUTDOWN_DELIVERY_GRACE_SECONDS
                    if self._ai_observer is not None
                    else _SHUTDOWN_DELIVERY_GRACE_SECONDS
                )
                try:
                    await asyncio.wait_for(self._queue.join(), timeout=flush_timeout)
                except TimeoutError:
                    LOGGER.warning("Timed out while flushing pending notifications")
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
            if discard_pending:
                discarded = 0
                while True:
                    try:
                        pending = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._deduplicator.release(pending.key)
                    self._queue.task_done()
                    discarded += 1
                LOGGER.info(
                    "Cancelled unfinished alert processing for quiet hours (discarded_queued=%d)",
                    discarded,
                )
        try:
            await self._notifier.close()
        finally:
            if self._ai_observer is not None:
                await self._ai_observer.close()

    async def handle_event(self, event: object) -> None:
        """Filter and enqueue without network awaits, keeping memory bounded."""

        if not self._accepting_events:
            return
        if bool(getattr(event, "out", False)) and self._config.notification_mode != "bot":
            return
        registry = self._registry
        if registry is None:
            if len(self._startup_buffer) >= self._config.startup_buffer_capacity:
                self._startup_buffer.popleft()
                self._dropped_notifications += 1
                LOGGER.error(
                    "Startup event buffer is full; dropping its oldest unfiltered Telegram event"
                )
            self._startup_buffer.append(event)
            return

        chat_id = getattr(event, "chat_id", None)
        raw_text = getattr(event, "raw_text", None)
        text = raw_text if isinstance(raw_text, str) else ""
        source = registry.get(chat_id)
        if source is None:
            return
        if self._event_is_outside_active_window(event):
            return
        message_id = getattr(event, "id", None)
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            LOGGER.warning("Ignoring Telegram event without a stable chat/message ID")
            return

        key = (chat_id, message_id)
        matched_keywords = registry.matches(chat_id, text)
        if matched_keywords is None:
            self._log_message(event, key, text=None)
            return
        self._log_message(event, key, text=text)
        if not self._deduplicator.claim(key):
            return

        try:
            if self._should_skip_forward_from_watched_source(event, chat_id):
                self._deduplicator.release(key)
                return

            event_message = getattr(event, "message", None)
            post_author = getattr(event_message, "post_author", None)
            sender = getattr(event, "sender", None)
            sender_name = utils.get_display_name(sender).strip() if sender else ""
            if post_author:
                sender_name = str(post_author).strip()
            if not sender_name:
                sender_id = getattr(event, "sender_id", None)
                sender_name = f"ID {sender_id}" if sender_id is not None else "невідомий автор"
            snapshot = MessageSnapshot(
                source_title=source.title,
                sender_name=sender_name,
                text=text,
                message_id=message_id,
                peer_id=chat_id,
                date=getattr(event, "date", None) or datetime.now(UTC),
                matched_keywords=matched_keywords,
                notify_all=source.rule.notify_all,
                username=source.username,
                has_media=bool(getattr(event_message, "media", None)),
            )
            pending = _PendingNotification(
                key=key,
                snapshot=snapshot,
                trusted_area_context=self._config.trusted_area_context_for(source.rule),
                skip_ai=source.rule.skip_ai,
            )
            try:
                self._queue.put_nowait(pending)
            except asyncio.QueueFull:
                self._deduplicator.release(key)
                self._dropped_notifications += 1
                LOGGER.error(
                    "Notification queue is full; dropping Telegram message %s/%s",
                    chat_id,
                    message_id,
                )
        except asyncio.CancelledError:
            self._deduplicator.release(key)
            raise
        except Exception:
            self._deduplicator.release(key)
            LOGGER.exception("Could not process Telegram message %s/%s", chat_id, message_id)

    def _event_is_outside_active_window(self, event: object) -> bool:
        quiet_hours = self._config.quiet_hours
        if not quiet_hours.enabled and self._accept_events_since is None:
            return False
        message_date = getattr(event, "date", None)
        if not isinstance(message_date, datetime):
            LOGGER.warning("Ignoring Telegram event without a valid quiet-hours timestamp")
            return True
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=UTC)
        if quiet_hours.contains(message_date):
            return True
        return self._accept_events_since is not None and message_date < self._accept_events_since

    def _log_message(
        self,
        event: object,
        key: MessageKey,
        text: str | None,
    ) -> None:
        if not self._message_log_deduplicator.claim(key):
            return
        try:
            message_date = getattr(event, "date", None)
            if not isinstance(message_date, datetime):
                message_date = datetime.now(UTC)
            elif message_date.tzinfo is None:
                message_date = message_date.replace(tzinfo=UTC)
            local_date = message_date.astimezone(ZoneInfo(self._config.timezone))
            local_timestamp = local_date.isoformat(timespec="seconds")
            if text is None:
                LOGGER.info("Skip new message - %s", local_timestamp)
                return

            preview = _safe_log_text(text, max_chars=500)
            if preview == "-":
                event_message = getattr(event, "message", None)
                preview = (
                    "[media without text]"
                    if bool(getattr(event_message, "media", None))
                    else "[empty message]"
                )
            LOGGER.info(
                "Match new message - %s: %s",
                local_timestamp,
                preview,
            )
        except Exception:
            LOGGER.exception("Could not render message decision log for %s/%s", *key)
        finally:
            self._message_log_deduplicator.commit(key)

    async def _notification_worker(self) -> None:
        while True:
            pending = await self._queue.get()
            key = pending.key
            alert_prepared = False
            try:
                ai_observation = await self._observe(pending)
                notification = render_notification(
                    pending.snapshot,
                    timezone_name=self._config.timezone,
                    max_preview_chars=self._config.max_preview_chars,
                    ai_observation=ai_observation,
                )
                # The completed observation and rendered alert are one logical unit.
                # Telegram transport retries must never cause another AI request.
                self._deduplicator.commit(key)
                alert_prepared = True
                if (
                    ai_observation is not None
                    and ai_observation.result is not None
                    and ai_observation.result.decision is AIDecision.REJECT
                ):
                    LOGGER.warning(
                        "AI rejected notification; skipped Telegram delivery "
                        "(message=%s/%s, alert=%s)",
                        *key,
                        _safe_log_text(
                            notification,
                            max_chars=_REJECTED_NOTIFICATION_LOG_MAX_CHARS,
                        ),
                    )
                    continue
                await self._deliver_with_retries(key, notification)
            except asyncio.CancelledError:
                if not alert_prepared:
                    self._deduplicator.release(key)
                raise
            except Exception:
                if not alert_prepared:
                    self._deduplicator.release(key)
                LOGGER.exception("Could not prepare notification for Telegram message %s/%s", *key)
            finally:
                self._queue.task_done()

    async def _observe(self, pending: _PendingNotification) -> AIObservationReport | None:
        if pending.snapshot.notify_all or pending.skip_ai:
            return None

        observer = self._ai_observer
        if observer is None:
            return None

        started = time.monotonic()
        try:
            report = await observer.observe(
                pending.snapshot,
                trusted_area_context=pending.trusted_area_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            report = AIObservationReport(
                result=None,
                status=AIObservationTechnicalStatus.API_ERROR,
                model=self._config.ai_observation.model,
                prompt_hash=None,
                elapsed_seconds=max(0.0, round(time.monotonic() - started, 3)),
                api_latency_seconds=None,
                attempts=0,
                token_usage=None,
            )

        self._log_ai_observation(pending.key, report)
        return report

    @staticmethod
    def _log_ai_observation(key: MessageKey, report: AIObservationReport) -> None:
        model = _safe_log_text(report.model, max_chars=128)
        if report.status is not None:
            if report.status is AIObservationTechnicalStatus.INVALID_RESPONSE:
                response_text = _safe_log_text(
                    report.response_text or "",
                    max_chars=_AI_RESPONSE_LOG_MAX_CHARS,
                )
                LOGGER.error(
                    "AI observation failed (status=%s, model=%s, "
                    "message=%s/%s, elapsed_seconds=%.3f, attempts=%d, ai_response=%s)",
                    report.status.value,
                    model,
                    *key,
                    report.elapsed_seconds,
                    report.attempts,
                    response_text,
                )
                return
            LOGGER.error(
                "AI observation failed (status=%s, model=%s, "
                "message=%s/%s, elapsed_seconds=%.3f, attempts=%d)",
                report.status.value,
                model,
                *key,
                report.elapsed_seconds,
                report.attempts,
            )
            return

        result = report.result
        if result is None:  # pragma: no cover - guarded by AIObservationReport validation.
            return
        decision_fields = f"decision={result.decision.value}"
        if result.reason_code is not None:
            decision_fields += f", reason_code={result.reason_code.value}"
        token_usage = report.token_usage
        if token_usage is not None:
            LOGGER.info(
                "AI observation completed (%s, "
                "model=%s, message=%s/%s, elapsed_seconds=%.3f, attempts=%d, "
                "input_tokens=%d, output_tokens=%d, total_tokens=%d)",
                decision_fields,
                model,
                *key,
                report.elapsed_seconds,
                report.attempts,
                token_usage.input_tokens,
                token_usage.output_tokens,
                token_usage.total_tokens,
            )
            return
        LOGGER.info(
            "AI observation completed (%s, "
            "model=%s, message=%s/%s, elapsed_seconds=%.3f, attempts=%d)",
            decision_fields,
            model,
            *key,
            report.elapsed_seconds,
            report.attempts,
        )

    async def _deliver_with_retries(self, key: MessageKey, notification: str) -> bool:
        for attempt in range(1, self._config.delivery_attempts + 1):
            try:
                await self._notifier.send(notification)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retryable = not isinstance(error, NotificationError) or error.retryable
                if not retryable or attempt >= self._config.delivery_attempts:
                    LOGGER.error(
                        "Could not send notification for Telegram message "
                        "%s/%s after %d attempt(s)",
                        *key,
                        attempt,
                        exc_info=True,
                    )
                    return False

                delay = min(
                    self._config.delivery_retry_base_seconds * (2 ** (attempt - 1)),
                    self._config.delivery_retry_max_seconds,
                )
                retry_after = getattr(error, "retry_after", None)
                flood_wait = getattr(error, "seconds", None)
                if isinstance(retry_after, (int, float)):
                    delay = max(delay, float(retry_after))
                if isinstance(flood_wait, (int, float)):
                    delay = max(delay, float(flood_wait))
                LOGGER.warning(
                    "Notification delivery failed for %s/%s; retrying in %.1fs (%d/%d)",
                    *key,
                    delay,
                    attempt,
                    self._config.delivery_attempts,
                )
                await asyncio.sleep(delay)
        return False  # pragma: no cover - the loop always returns.

    def _should_skip_forward_from_watched_source(self, event: object, chat_id: int) -> bool:
        if not self._config.skip_forwards_from_watched_sources or self._registry is None:
            return False
        event_message = getattr(event, "message", None)
        forward_header = getattr(event_message, "fwd_from", None)
        origin_peer = getattr(forward_header, "from_id", None)
        if origin_peer is None:
            return False
        try:
            origin_id = utils.get_peer_id(origin_peer)
        except (TypeError, ValueError):
            return False
        sender_id = getattr(event, "sender_id", None)
        return (
            origin_id != chat_id
            and sender_id == origin_id
            and self._registry.get(origin_id) is not None
        )


def _safe_log_text(value: str, *, max_chars: int) -> str:
    printable = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in value
    )
    collapsed = " ".join(printable.split()) or "-"
    return collapsed if len(collapsed) <= max_chars else collapsed[: max_chars - 1] + "…"
