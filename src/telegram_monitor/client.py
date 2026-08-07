from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession

from .credentials import TelegramCredentials


def create_client(credentials: TelegramCredentials) -> TelegramClient:
    if credentials.session_string is None:
        raise ValueError("A session string is required to create the monitor client")
    return TelegramClient(
        StringSession(credentials.session_string),
        credentials.api_id,
        credentials.api_hash,
        device_model="Telegram Keyword Monitor",
        system_version="1.0",
        app_version="0.1.0",
        auto_reconnect=True,
        catch_up=True,
        connection_retries=10,
        retry_delay=2,
    )


def create_login_client(credentials: TelegramCredentials) -> TelegramClient:
    return TelegramClient(
        StringSession(),
        credentials.api_id,
        credentials.api_hash,
        device_model="Telegram Keyword Monitor",
        system_version="1.0",
        app_version="0.1.0",
    )
