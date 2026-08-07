from __future__ import annotations

import logging

from telethon import TelegramClient

from .client import create_client
from .config import CONFIG
from .credentials import TelegramCredentials
from .models import ConfigurationError, MonitorConfig
from .notifier import Notifier, TelegramBotNotifier, TelegramDialogNotifier
from .service import TelegramMonitor

LOGGER = logging.getLogger(__name__)


def build_notifier(client: TelegramClient, config: MonitorConfig) -> Notifier:
    if config.notification_mode == "bot":
        return TelegramBotNotifier.from_environment(
            config.bot_subscriber_database,
            subscriber_limit=config.bot_subscriber_limit,
            delivery_attempts=config.delivery_attempts,
            delivery_retry_base_seconds=config.delivery_retry_base_seconds,
            delivery_retry_max_seconds=config.delivery_retry_max_seconds,
            on_polling_fatal=client.disconnect,
        )
    return TelegramDialogNotifier(client, config.notify_to)


async def connect_authorized(client: TelegramClient) -> None:
    await client.connect()
    if not await client.is_user_authorized():
        raise ConfigurationError(
            "Telegram session is not authorized; generate a fresh TELEGRAM_SESSION_STRING"
        )


async def run_monitor(config: MonitorConfig = CONFIG) -> None:
    config.validate_for_run()
    credentials = TelegramCredentials.from_environment()
    client = create_client(credentials)
    monitor = TelegramMonitor(
        client=client,
        config=config,
        notifier=build_notifier(client, config),
    )

    try:
        monitor.start_capture()
        await connect_authorized(client)
        await monitor.prepare()
        me = await client.get_me()
        LOGGER.info("Connected as Telegram account %s", getattr(me, "id", "unknown"))
        LOGGER.info("Monitor is ready; press Ctrl+C to stop")
        await client.run_until_disconnected()
    finally:
        try:
            await monitor.close()
        finally:
            await client.disconnect()
