from __future__ import annotations

import asyncio
import logging
import unicodedata
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from telethon import events, utils

from .deduplication import MessageKey, RecentMessageCache
from .formatting import render_notification
from .models import MessageSnapshot, MonitorConfig
from .notifier import NotificationError, Notifier
from .registry import SourceRegistry

LOGGER = logging.getLogger(__name__)


class TelegramMonitor:
    """Resolve configured dialogs, consume NewMessage events, and emit alerts."""

    def __init__(self, client: object, config: MonitorConfig, notifier: Notifier) -> None:
        self._client = client
        self._config = config
        self._notifier = notifier
        self._registry: SourceRegistry | None = None
        self._deduplicator = RecentMessageCache(config.deduplication_window)
        self._message_log_deduplicator = RecentMessageCache(config.deduplication_window)
        self._queue: asyncio.Queue[tuple[MessageKey, str]] = asyncio.Queue(
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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._accepting_events = False
        if self._event_builder is not None:
            self._client.remove_event_handler(self.handle_event, self._event_builder)
            self._event_builder = None
        self._startup_buffer.clear()

        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except TimeoutError:
                LOGGER.warning("Timed out while flushing pending notifications")
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        await self._notifier.close()

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
            notification = render_notification(
                snapshot,
                timezone_name=self._config.timezone,
                max_preview_chars=self._config.max_preview_chars,
            )
            try:
                self._queue.put_nowait((key, notification))
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
            key, notification = await self._queue.get()
            try:
                delivered = await self._deliver_with_retries(key, notification)
            except asyncio.CancelledError:
                self._deduplicator.release(key)
                raise
            else:
                if delivered:
                    self._deduplicator.commit(key)
                else:
                    self._deduplicator.release(key)
            finally:
                self._queue.task_done()

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
