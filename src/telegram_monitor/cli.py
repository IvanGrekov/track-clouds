from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence

from telethon.sessions import StringSession

from . import __version__
from .app import connect_authorized, run_monitor
from .client import create_client, create_login_client
from .config import load_config
from .credentials import TelegramCredentials
from .matcher import (
    MIN_MESSAGE_LENGTH,
    KeywordMatcher,
    ends_with_question_mark,
    has_minimum_message_length,
)
from .models import ConfigurationError, MonitorConfig

LOGGER = logging.getLogger(__name__)


class _SuppressDifferenceLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        channel_difference = (
            record.name == "telethon.client.updates"
            and record.levelno == logging.INFO
            and message.startswith("Got difference for channel ")
            and message.endswith(" updates")
        )
        account_difference = (
            record.name == "telethon.client.updates"
            and record.levelno == logging.INFO
            and message == "Got difference for account updates"
        )
        return not (channel_difference or account_difference)


class _BelowWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.WARNING


_DIFFERENCE_LOG_FILTER = _SuppressDifferenceLogs()
_BELOW_WARNING_FILTER = _BelowWarningFilter()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-monitor",
        description="Watch Telegram dialogs and notify on configured keyword matches.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="start the event-driven monitor (default)")
    commands.add_parser("list-chats", help="list dialogs available to the configured account")
    commands.add_parser("generate-session", help="interactively create a dedicated session string")
    check = commands.add_parser("check", help="test configured rules against text without Telegram")
    check.add_argument("text", help="message text to check")
    return parser


def _configure_logging() -> None:
    configured_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, configured_level, logging.INFO)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.addFilter(_BELOW_WARNING_FILTER)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=(stdout_handler, stderr_handler),
    )
    updates_logger = logging.getLogger("telethon.client.updates")
    if _DIFFERENCE_LOG_FILTER not in updates_logger.filters:
        updates_logger.addFilter(_DIFFERENCE_LOG_FILTER)


async def _generate_session() -> None:
    credentials = TelegramCredentials.from_environment(require_session=False)
    client = create_login_client(credentials)
    try:
        await client.start()
        session_string = StringSession.save(client.session)
        print("\nTELEGRAM_SESSION_STRING=" + session_string)
        print(
            "\nStore this value as a secret. Generate a different session for every concurrent app."
        )
    finally:
        await client.disconnect()


def _dialog_type(dialog: object) -> str:
    if bool(getattr(dialog, "is_channel", False)):
        if bool(getattr(getattr(dialog, "entity", None), "broadcast", False)):
            return "channel"
        return "supergroup"
    if bool(getattr(dialog, "is_group", False)):
        return "group"
    if bool(getattr(dialog, "is_user", False)):
        return "private"
    return "unknown"


def _one_line(value: object) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


async def _list_chats() -> None:
    credentials = TelegramCredentials.from_environment()
    client = create_client(credentials)
    try:
        await connect_authorized(client)
        dialogs = await client.get_dialogs()
        me = await client.get_me()
        print(f"# YOUR_USER_ID={getattr(me, 'id', '-')}")
        print("TYPE\tDIALOG_ID\tUSERNAME\tTITLE")
        for dialog in dialogs:
            entity = getattr(dialog, "entity", None)
            username = getattr(entity, "username", None)
            username_display = f"@{username}" if username else "-"
            print(
                "\t".join(
                    (
                        _dialog_type(dialog),
                        str(getattr(dialog, "id", "-")),
                        _one_line(username_display),
                        _one_line(getattr(dialog, "name", "")),
                    )
                )
            )
    finally:
        await client.disconnect()


async def _run_monitor_with_signals(config: MonitorConfig) -> None:
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    signal_installed = False
    if current_task is not None:
        try:
            loop.add_signal_handler(signal.SIGTERM, current_task.cancel)
            signal_installed = True
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await run_monitor(config)
    except asyncio.CancelledError:
        LOGGER.info("Stopped by signal")
    finally:
        if signal_installed:
            loop.remove_signal_handler(signal.SIGTERM)


def _check_text(text: str, config: MonitorConfig) -> int:
    matched_count = 0
    for source in config.sources:
        if ends_with_question_mark(text):
            print(f"SKIP  {source.label or source.peer}: ends with ?")
            continue
        if not has_minimum_message_length(text):
            print(
                f"SKIP  {source.label or source.peer}: fewer than {MIN_MESSAGE_LENGTH} characters"
            )
            continue
        matches = KeywordMatcher(source.keywords).find_matches(text)
        if source.notify_all or matches:
            skip_matches = KeywordMatcher(source.keywords_to_skip).find_matches(text)
            if skip_matches:
                print(
                    f"SKIP  {source.label or source.peer}: keywords_to_skip="
                    + ", ".join(skip_matches)
                )
                continue
            matched_count += 1
            reason = ", ".join(matches) if matches else "notify_all=True"
            print(f"MATCH {source.label or source.peer}: {reason}")
        else:
            print(f"SKIP  {source.label or source.peer}")
    return 0 if matched_count else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging()

    try:
        command = args.command or "run"
        if command == "run":
            asyncio.run(_run_monitor_with_signals(load_config()))
        elif command == "list-chats":
            asyncio.run(_list_chats())
        elif command == "generate-session":
            asyncio.run(_generate_session())
        elif command == "check":
            return _check_text(args.text, load_config())
        else:  # pragma: no cover - argparse constrains the choices.
            parser.error(f"Unknown command: {command}")
    except ConfigurationError as error:
        LOGGER.error("Configuration error: %s", error)
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 130
    return 0
