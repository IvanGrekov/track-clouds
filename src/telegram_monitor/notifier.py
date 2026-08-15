from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import ParamSpec, Protocol, TypeVar

import httpx
from dotenv import load_dotenv

from .models import ChatRef, ConfigurationError
from .subscriber_store import Subscriber, SubscriberStore, SubscriptionResult

LOGGER = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _run_blocking(operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run blocking work without leaving it orphaned when the caller is cancelled."""

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # asyncio cancellation cannot stop a thread that is already inside sqlite3.
        # Wait for it before allowing notifier shutdown to close the connection.
        with suppress(Exception):
            await task
        raise


class NotificationError(RuntimeError):
    """Raised when the configured notification transport rejects an alert."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
        error_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.error_code = error_code


class Notifier(Protocol):
    async def start(self) -> None: ...

    async def send(self, text: str) -> None: ...

    async def close(self) -> None: ...


class TelegramDialogNotifier:
    """Writes alerts to Saved Messages or another configured Telegram dialog."""

    def __init__(self, client: object, target: ChatRef) -> None:
        self._client = client
        self._target = target

    async def start(self) -> None:
        return None

    async def send(self, text: str) -> None:
        await self._client.send_message(
            self._target,
            text,
            parse_mode=None,
            link_preview=False,
        )

    async def close(self) -> None:
        return None


class TelegramBotApi:
    """Minimal, token-safe async client for the Bot API methods used here."""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Bot API authentication lives in the URL. Silence HTTPX's request logging so
        # INFO/DEBUG application logs can never disclose the token.
        logging.getLogger("httpx").setLevel(logging.CRITICAL + 1)
        logging.getLogger("httpcore").setLevel(logging.CRITICAL + 1)
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/",
            timeout=httpx.Timeout(40, connect=10),
            transport=transport,
        )

    async def get_webhook_info(self) -> Mapping[str, object]:
        result = await self._request("getWebhookInfo", {})
        if not isinstance(result, Mapping):
            raise NotificationError("Telegram Bot API returned invalid webhook information")
        return result

    async def get_me(self) -> Mapping[str, object]:
        result = await self._request("getMe", {})
        if not isinstance(result, Mapping):
            raise NotificationError("Telegram Bot API returned invalid bot information")
        return result

    async def get_updates(
        self,
        offset: int | None,
        *,
        timeout_seconds: int,
    ) -> tuple[Mapping[str, object], ...]:
        request: dict[str, object] = {
            "timeout": timeout_seconds,
            "limit": 100,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            request["offset"] = offset
        result = await self._request("getUpdates", request)
        if not isinstance(result, list) or any(not isinstance(item, Mapping) for item in result):
            raise NotificationError("Telegram Bot API returned an invalid update list")
        return tuple(result)

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "link_preview_options": {"is_disabled": True},
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, request: Mapping[str, object]) -> object:
        try:
            response = await self._client.post(method, json=request)
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            # HTTPX exception strings may contain the request URL, which embeds the bot token.
            raise NotificationError("Telegram Bot API request failed") from None

        if not isinstance(payload, Mapping):
            raise NotificationError("Telegram Bot API returned an invalid response")
        if response.is_error or payload.get("ok") is not True:
            description = str(payload.get("description", "unknown Bot API error"))
            raw_error_code = payload.get("error_code", response.status_code)
            error_code = raw_error_code if isinstance(raw_error_code, int) else None
            parameters = payload.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
            retryable = error_code == 429 or (error_code is not None and error_code >= 500)
            raise NotificationError(
                f"Telegram Bot API rejected the request: {description}",
                retryable=retryable,
                retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
                error_code=error_code,
            )
        return payload.get("result")


class TelegramBotNotifier:
    """Registers bot users through commands and broadcasts alerts to all subscribers."""

    def __init__(
        self,
        token: str,
        database_path: str,
        *,
        subscriber_limit: int = 10,
        delivery_attempts: int = 5,
        delivery_retry_base_seconds: float = 1.0,
        delivery_retry_max_seconds: float = 30.0,
        poll_timeout_seconds: int = 25,
        transport: httpx.AsyncBaseTransport | None = None,
        store: SubscriberStore | None = None,
        on_polling_fatal: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._api = TelegramBotApi(token, transport=transport)
        self._store = store or SubscriberStore(database_path, subscriber_limit)
        self._subscriber_limit = self._store.subscriber_limit
        self._delivery_attempts = delivery_attempts
        self._delivery_retry_base_seconds = delivery_retry_base_seconds
        self._delivery_retry_max_seconds = delivery_retry_max_seconds
        self._poll_timeout_seconds = poll_timeout_seconds
        self._on_polling_fatal = on_polling_fatal
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_ready = asyncio.Event()
        self._closed = False

    @classmethod
    def from_environment(
        cls,
        database_path: str,
        *,
        subscriber_limit: int = 10,
        delivery_attempts: int = 5,
        delivery_retry_base_seconds: float = 1.0,
        delivery_retry_max_seconds: float = 30.0,
        on_polling_fatal: Callable[[], Awaitable[object]] | None = None,
    ) -> TelegramBotNotifier:
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token.startswith("replace_"):
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required when notification_mode='bot'")
        return cls(
            token=token,
            database_path=database_path,
            subscriber_limit=subscriber_limit,
            delivery_attempts=delivery_attempts,
            delivery_retry_base_seconds=delivery_retry_base_seconds,
            delivery_retry_max_seconds=delivery_retry_max_seconds,
            on_polling_fatal=on_polling_fatal,
        )

    async def start(self) -> None:
        if self._poll_task is not None:
            return
        if self._closed:
            raise RuntimeError("Cannot start a closed bot notifier")
        await _run_blocking(self._store.open)
        try:
            bot_user = await self._api.get_me()
            webhook = await self._api.get_webhook_info()
        except NotificationError as error:
            if not error.retryable:
                raise ConfigurationError(f"Could not initialize Telegram bot: {error}") from None
            raise
        bot_id = bot_user.get("id")
        if isinstance(bot_id, bool) or not isinstance(bot_id, int):
            raise ConfigurationError("Telegram Bot API getMe response has no valid bot ID")
        await _run_blocking(self._store.select_bot, bot_id)
        webhook_url = webhook.get("url")
        if isinstance(webhook_url, str) and webhook_url:
            raise ConfigurationError(
                "The Telegram bot has an active webhook. Remove it before running this "
                "getUpdates-based monitor; Telegram does not allow both at once."
            )
        self._poll_task = asyncio.create_task(
            self._poll_updates(),
            name="telegram-bot-command-poller",
        )
        poll_task = self._poll_task
        ready_waiter = asyncio.create_task(self._poll_ready.wait())
        try:
            done, _ = await asyncio.wait(
                (poll_task, ready_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if poll_task in done:
                self._poll_task = None
                await poll_task
                raise RuntimeError("Telegram bot command poller stopped during startup")
        finally:
            ready_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await ready_waiter
        subscriber_count = len(await _run_blocking(self._store.list_chat_ids))
        LOGGER.info(
            "Bot command polling started; Accepting /start and /stop "
            "(subscriber limit: %d); Current subscribers: %d",
            self._subscriber_limit,
            subscriber_count,
        )

    async def send(self, text: str) -> None:
        await _run_blocking(self._store.open)
        chat_ids = await _run_blocking(self._store.list_chat_ids)
        if not chat_ids:
            LOGGER.warning(
                "Matched alert was not delivered because there are no active bot subscribers"
            )
            return

        total = len(chat_ids)
        LOGGER.info("Bot alert broadcast started (total=%d)", total)
        delivered = 0
        failed_chat_ids: list[int] = []
        for position, chat_id in enumerate(chat_ids, start=1):
            try:
                was_delivered = await self._send_with_retries(
                    chat_id,
                    text,
                    purpose="alert",
                    remove_invalid_subscriber=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not bubble an unexpected per-recipient failure to the monitor's
                # whole-alert retry loop. Subscribers processed earlier in this broadcast
                # may already have received the alert, so repeating the broadcast could
                # create duplicates for them. Exception details are intentionally omitted:
                # a transport error can contain request data.
                LOGGER.error(
                    "Bot alert delivery failed unexpectedly; continuing broadcast "
                    "(chat_id=%s, delivered=%d, failed=%d, total=%d, remaining=%d)",
                    chat_id,
                    delivered,
                    len(failed_chat_ids) + 1,
                    total,
                    total - position,
                )
                failed_chat_ids.append(chat_id)
                continue
            if was_delivered:
                delivered += 1
            else:
                failed_chat_ids.append(chat_id)

        if failed_chat_ids:
            status = "failed" if delivered == 0 else "partial"
            LOGGER.error(
                "Bot alert delivery incomplete "
                "(status=%s, delivered=%d, failed=%d, total=%d, failed_chat_ids=%s); "
                "per-recipient retries are exhausted and no further automatic retry is scheduled",
                status,
                delivered,
                len(failed_chat_ids),
                total,
                ",".join(str(chat_id) for chat_id in failed_chat_ids),
            )
        else:
            LOGGER.info("Bot alert delivered to %d/%d subscriber(s)", delivered, total)

    async def process_update(self, update: Mapping[str, object]) -> None:
        """Handle one Bot API update. Public for deterministic offline testing."""

        message = update.get("message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("type") != "private":
            return
        chat_id = chat.get("id")
        text = message.get("text")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int) or not isinstance(text, str):
            return

        command_token = text.strip().split(maxsplit=1)[0].casefold() if text.strip() else ""
        command = command_token.split("@", maxsplit=1)[0]
        if command not in ("/start", "/stop"):
            return

        await _run_blocking(self._store.open)
        sender = message.get("from")
        sender_data = sender if isinstance(sender, Mapping) else {}
        subscriber = Subscriber(
            chat_id=chat_id,
            user_id=_optional_int(sender_data.get("id")),
            username=_optional_text(sender_data.get("username")),
            first_name=_optional_text(sender_data.get("first_name")),
        )
        if command == "/start":
            result = await _run_blocking(self._store.subscribe, subscriber)
            if result is SubscriptionResult.ADDED:
                LOGGER.info("New user (%s)", _subscriber_log_details(subscriber))
                reply = "✅ Ви підписалися на сповіщення."
            elif result is SubscriptionResult.ALREADY_SUBSCRIBED:
                reply = "✅ Ви вже підписані на сповіщення."
            else:
                reply = (
                    "❌ Максимальна кількість користувачів перевищена "
                    f"(ліміт: {self._subscriber_limit})."
                )
        else:
            removed_subscriber = await _run_blocking(self._store.remove_subscriber, chat_id)
            removed = removed_subscriber is not None
            if removed_subscriber is not None:
                LOGGER.info(
                    "Removed user (%s, reason=/stop)",
                    _subscriber_log_details(removed_subscriber),
                )
            reply = (
                "🔕 Ви відписалися від сповіщень."
                if removed
                else "ℹ️ Ви ще не були підписані на сповіщення."
            )

        reply_delivered = await self._send_with_retries(
            chat_id,
            reply,
            purpose=f"{command} command reply",
            remove_invalid_subscriber=False,
        )
        if not reply_delivered:
            LOGGER.error(
                "Bot command reply was not delivered "
                "(command=%s, chat_id=%s); no further automatic retry is scheduled",
                command,
                chat_id,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        poll_error: Exception | None = None
        if self._poll_task is not None:
            if not self._poll_task.done():
                self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                poll_error = error
            self._poll_task = None
        try:
            await self._api.close()
        finally:
            await _run_blocking(self._store.close)
        if poll_error is not None:
            raise poll_error

    async def _poll_updates(self) -> None:
        offset = await _run_blocking(self._store.get_next_update_offset)
        error_delay = max(self._delivery_retry_base_seconds, 1.0)
        while True:
            try:
                updates = await self._api.get_updates(
                    offset,
                    timeout_seconds=self._poll_timeout_seconds if self._poll_ready.is_set() else 0,
                )
                error_delay = max(self._delivery_retry_base_seconds, 1.0)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, bool) or not isinstance(update_id, int):
                        LOGGER.warning("Ignoring Bot API update without an integer update_id")
                        continue
                    await self.process_update(update)
                    offset = max(offset or 0, update_id + 1)
                    await _run_blocking(self._store.set_next_update_offset, offset)
                self._poll_ready.set()
            except asyncio.CancelledError:
                raise
            except NotificationError as error:
                if not error.retryable:
                    detail = (
                        "another process is already consuming getUpdates for this bot token"
                        if error.error_code == 409
                        else str(error)
                    )
                    fatal_error = ConfigurationError(
                        f"Telegram bot command polling stopped permanently: {detail}"
                    )
                    LOGGER.critical("%s", fatal_error)
                    if self._poll_ready.is_set() and self._on_polling_fatal is not None:
                        try:
                            await self._on_polling_fatal()
                        except Exception:
                            LOGGER.exception("Could not stop the monitor after bot polling failed")
                    raise fatal_error from None
                LOGGER.exception(
                    "Could not poll Telegram bot commands; retrying in %.1fs",
                    error_delay,
                )
                await asyncio.sleep(error_delay)
                error_delay = min(
                    error_delay * 2,
                    max(self._delivery_retry_max_seconds, 1.0),
                )
            except Exception:
                LOGGER.exception(
                    "Could not poll or process Telegram bot commands; retrying in %.1fs",
                    error_delay,
                )
                await asyncio.sleep(error_delay)
                error_delay = min(
                    error_delay * 2,
                    max(self._delivery_retry_max_seconds, 1.0),
                )

    async def _send_with_retries(
        self,
        chat_id: int,
        text: str,
        *,
        purpose: str,
        remove_invalid_subscriber: bool,
    ) -> bool:
        for attempt in range(1, self._delivery_attempts + 1):
            try:
                await self._api.send_message(chat_id, text)
                return True
            except asyncio.CancelledError:
                raise
            except NotificationError as error:
                invalid_recipient = error.error_code == 403 or (
                    error.error_code == 400 and "chat not found" in str(error).casefold()
                )
                if invalid_recipient and remove_invalid_subscriber:
                    LOGGER.warning(
                        "Bot API %s was not delivered to an unreachable subscriber "
                        "(chat_id=%s, error_code=%s); removing the subscriber",
                        purpose,
                        chat_id,
                        error.error_code,
                    )
                    try:
                        removed_subscriber = await _run_blocking(
                            self._store.remove_subscriber,
                            chat_id,
                        )
                    except Exception:
                        LOGGER.exception("Could not remove unreachable bot subscriber %s", chat_id)
                        return False
                    if removed_subscriber is not None:
                        LOGGER.info(
                            "Removed user (%s, reason=unreachable)",
                            _subscriber_log_details(removed_subscriber),
                        )
                    return False
                if not error.retryable or attempt >= self._delivery_attempts:
                    LOGGER.error(
                        "Could not send Bot API %s to chat_id=%s after %d attempt(s) "
                        "(error_code=%s, retryable=%s, retry_after=%s): %s; "
                        "no further automatic retry is scheduled",
                        purpose,
                        chat_id,
                        attempt,
                        error.error_code,
                        error.retryable,
                        error.retry_after,
                        error,
                    )
                    return False

                delay = min(
                    self._delivery_retry_base_seconds * (2 ** (attempt - 1)),
                    self._delivery_retry_max_seconds,
                )
                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                LOGGER.warning(
                    "Bot API %s delivery to chat_id=%s failed "
                    "(error_code=%s, retry_after=%s); retrying in %.1fs (%d/%d)",
                    purpose,
                    chat_id,
                    error.error_code,
                    error.retry_after,
                    delay,
                    attempt,
                    self._delivery_attempts,
                )
                await asyncio.sleep(delay)
        return False  # pragma: no cover - the loop always returns.


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _subscriber_log_details(subscriber: Subscriber) -> str:
    username = _safe_log_value(subscriber.username)
    if username != "-" and not username.startswith("@"):
        username = "@" + username
    return ", ".join(
        (
            f"chat_id={subscriber.chat_id}",
            f"user_id={subscriber.user_id if subscriber.user_id is not None else '-'}",
            f"username={username}",
            f"first_name={_safe_log_value(subscriber.first_name)}",
        )
    )


def _safe_log_value(value: str | None, max_chars: int = 100) -> str:
    if not value:
        return "-"
    printable = "".join(
        character if not unicodedata.category(character).startswith("C") else " "
        for character in value
    )
    collapsed = " ".join(printable.split()) or "-"
    return collapsed if len(collapsed) <= max_chars else collapsed[: max_chars - 1] + "…"
