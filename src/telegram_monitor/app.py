from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime

from telethon import TelegramClient

from .ai_observer import AIObserver, build_ai_observer
from .client import create_client
from .credentials import TelegramCredentials
from .models import ConfigurationError, MonitorConfig
from .notifier import Notifier, TelegramBotNotifier, TelegramDialogNotifier
from .service import TelegramMonitor

LOGGER = logging.getLogger(__name__)

_Now = Callable[[], datetime]
_Sleep = Callable[[float], Awaitable[object]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _sleep_until(target: datetime, *, now: _Now, sleep: _Sleep) -> None:
    while True:
        remaining = (target - now().astimezone(UTC)).total_seconds()
        if remaining <= 0:
            return
        await sleep(remaining)


def build_notifier(client: TelegramClient, config: MonitorConfig) -> Notifier:
    if config.notification_mode == "bot":
        return TelegramBotNotifier.from_environment(
            config.bot_subscriber_database,
            subscriber_limit=config.bot_subscriber_limit,
            delivery_attempts=config.delivery_attempts,
            delivery_retry_base_seconds=config.delivery_retry_base_seconds,
            delivery_retry_max_seconds=config.delivery_retry_max_seconds,
            on_polling_fatal=client.disconnect,
            quiet_hours=config.quiet_hours,
        )
    return TelegramDialogNotifier(client, config.notify_to)


async def connect_authorized(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        raise ConfigurationError(
            "Telegram session is not authorized; generate a fresh TELEGRAM_SESSION_STRING"
        )


async def _run_active_monitor(
    config: MonitorConfig,
    *,
    stop_at: datetime | None,
    accept_events_since: datetime | None,
    now: _Now,
    sleep: _Sleep,
) -> bool:
    """Run one connected monitor session; return true after a quiet-hours stop."""

    credentials = TelegramCredentials.from_environment()
    client = create_client(credentials)
    notifier: Notifier | None = None
    ai_observer: AIObserver | None = None
    monitor: TelegramMonitor | None = None
    run_task: asyncio.Task[None] | None = None
    transition_task: asyncio.Task[None] | None = None
    discard_pending = False

    try:
        notifier = build_notifier(client, config)
        ai_observer = build_ai_observer(config.ai_observation)
        monitor = TelegramMonitor(
            client=client,
            config=config,
            notifier=notifier,
            ai_observer=ai_observer,
            accept_events_since=accept_events_since,
        )
        monitor.start_capture()
        await connect_authorized(client)
        await monitor.prepare()
        me = await client.get_me()
        LOGGER.info("Connected as Telegram account %s", getattr(me, "id", "unknown"))
        LOGGER.info("Monitor is ready; press Ctrl+C to stop")
        if stop_at is None:
            await client.run_until_disconnected()
            return False

        run_task = asyncio.create_task(
            client.run_until_disconnected(),
            name="telegram-monitor-connection",
        )
        transition_task = asyncio.create_task(
            _sleep_until(stop_at, now=now, sleep=sleep),
            name="quiet-hours-start-timer",
        )
        done, _ = await asyncio.wait(
            (run_task, transition_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if run_task in done:
            await run_task
            return False

        await transition_task
        discard_pending = True
        quiet_hours = config.quiet_hours
        LOGGER.info(
            "Quiet hours started (%s-%s %s); cancelling unfinished alerts",
            quiet_hours.start,
            quiet_hours.end,
            quiet_hours.timezone,
        )
        return True
    finally:
        if transition_task is not None and not transition_task.done():
            transition_task.cancel()
            with suppress(asyncio.CancelledError):
                await transition_task
        try:
            if monitor is not None:
                # TelegramMonitor owns both the notifier and the optional observer
                # once construction succeeds. Its close order stops the worker before
                # closing either network client.
                if discard_pending:
                    await monitor.close(discard_pending=True)
                else:
                    await monitor.close()
            else:
                # Construction can fail after one or both resources are built. Close
                # unowned resources here without allowing one close failure to leak the
                # other resource.
                try:
                    if notifier is not None:
                        await notifier.close()
                finally:
                    if ai_observer is not None:
                        await ai_observer.close()
        finally:
            try:
                await client.disconnect()
            finally:
                if run_task is not None:
                    if not run_task.done():
                        run_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await run_task


async def run_monitor(
    config: MonitorConfig,
    *,
    now: _Now = _utc_now,
    sleep: _Sleep = asyncio.sleep,
) -> None:
    config.validate_for_run()
    quiet_hours = config.quiet_hours
    if not quiet_hours.enabled:
        await _run_active_monitor(
            config,
            stop_at=None,
            accept_events_since=None,
            now=now,
            sleep=sleep,
        )
        return

    while True:
        current = now().astimezone(UTC)
        transition = quiet_hours.next_transition(current)
        if quiet_hours.contains(current):
            LOGGER.info(
                "Quiet hours active; Telegram and OpenAI clients are offline until %s",
                transition.isoformat().replace("+00:00", "Z"),
            )
            await _sleep_until(transition, now=now, sleep=sleep)
            LOGGER.info("Quiet hours ended; reconnecting")
            continue

        stopped_for_quiet_hours = await _run_active_monitor(
            config,
            stop_at=transition,
            accept_events_since=quiet_hours.most_recent_end(current),
            now=now,
            sleep=sleep,
        )
        if not stopped_for_quiet_hours:
            return
