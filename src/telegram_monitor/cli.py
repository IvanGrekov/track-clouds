from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from telethon.sessions import StringSession

from . import __version__
from .ai_models import AIObservationTechnicalStatus
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
from .models import AIObservationConfig, ConfigurationError, MonitorConfig
from .openai_client import (
    AIObservationFailure,
    AIObservationRequest,
    AIObservationSuccess,
    build_openai_observation_client,
)

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

_AI_TECHNICAL_EXIT_CODE = 3


class _AIClient(Protocol):
    async def classify(
        self,
        request: AIObservationRequest,
        *,
        timeout_seconds: float,
    ) -> AIObservationSuccess | AIObservationFailure: ...

    async def close(self) -> None: ...


_AIClientFactory = Callable[[AIObservationConfig], _AIClient | None]


def _non_negative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


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
    ai_check = commands.add_parser(
        "ai-check",
        help="make one explicit live OpenAI classification without using Telegram",
    )
    ai_check.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="confirm that message text may be sent to OpenAI and incur API usage",
    )
    ai_check.add_argument("text", nargs="?", help="message text to classify")
    ai_check.add_argument(
        "--stdin",
        action="store_true",
        help="read message text from stdin instead of a positional argument",
    )
    ai_check.add_argument(
        "--matched-keyword",
        dest="matched_keywords",
        action="append",
        default=[],
        help="prefilter keyword that matched; repeat for multiple values",
    )
    ai_check.add_argument(
        "--notify-all",
        action="store_true",
        help="represent a source configured with notify_all=true",
    )
    ai_check.add_argument(
        "--trusted-area-context",
        help="trusted area context; defaults to ai_observation.default_trusted_area_context",
    )
    ai_check.add_argument(
        "--message-age-seconds",
        type=_non_negative_integer,
        default=0,
        help="message age used to derive sent_at (default: 0)",
    )
    return parser


def _configure_logging(*, stdout_is_data: bool = False) -> None:
    configured_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, configured_level, logging.INFO)
    stderr_handler = logging.StreamHandler(sys.stderr)
    if stdout_is_data:
        handlers = (stderr_handler,)
    else:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.addFilter(_BELOW_WARNING_FILTER)
        stderr_handler.setLevel(logging.WARNING)
        handlers = (stdout_handler, stderr_handler)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    updates_logger = logging.getLogger("telethon.client.updates")
    if _DIFFERENCE_LOG_FILTER not in updates_logger.filters:
        updates_logger.addFilter(_DIFFERENCE_LOG_FILTER)
    # The OpenAI and HTTP clients can include request options or bodies in DEBUG logs.
    # Keep private prompts and Telegram text out of application output even when the
    # monitor's own LOG_LEVEL is DEBUG.
    for logger_name in ("openai", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


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


def _read_ai_check_input(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> tuple[str, tuple[str, ...]]:
    has_positional_text = args.text is not None
    if has_positional_text == args.stdin:
        parser.error("ai-check requires exactly one of positional text or --stdin")

    text = sys.stdin.read().rstrip("\r\n") if args.stdin else args.text
    if not isinstance(text, str) or not text.strip():
        parser.error("ai-check message text must not be empty")

    matched_keywords = tuple(
        keyword.strip()
        for keyword in args.matched_keywords
        if isinstance(keyword, str) and keyword.strip()
    )
    if not matched_keywords and not args.notify_all:
        parser.error("ai-check requires --matched-keyword or --notify-all")
    return text, matched_keywords


def _ai_metadata(
    outcome: AIObservationSuccess | AIObservationFailure,
) -> dict[str, object]:
    return {
        "model": outcome.model,
        "prompt_hash": outcome.prompt_hash,
        "api_latency_seconds": outcome.api_latency_seconds,
        "attempts": outcome.attempts,
    }


def _ai_check_payload(
    outcome: AIObservationSuccess | AIObservationFailure,
) -> tuple[dict[str, object], int]:
    metadata = _ai_metadata(outcome)
    if isinstance(outcome, AIObservationSuccess):
        result = outcome.result
        token_usage: dict[str, int] | None = None
        if outcome.token_usage is not None:
            token_usage = {
                "input_tokens": outcome.token_usage.input_tokens,
                "output_tokens": outcome.token_usage.output_tokens,
                "total_tokens": outcome.token_usage.total_tokens,
            }
        metadata["token_usage"] = token_usage
        return (
            {
                "kind": "semantic",
                "decision": result.decision.value,
                "confidence": result.confidence,
                "location": result.location,
                "event": result.event,
                "temporal_relevance": result.temporal_relevance.value,
                "reason_code": result.reason_code.value,
                "reason": result.reason,
                "metadata": metadata,
            },
            0,
        )
    return (
        {
            "kind": "technical_failure",
            "status": outcome.status.value,
            "metadata": metadata,
        },
        _AI_TECHNICAL_EXIT_CODE,
    )


def _unexpected_ai_check_payload(
    config: AIObservationConfig,
    *,
    elapsed_seconds: float,
) -> tuple[dict[str, object], int]:
    return (
        {
            "kind": "technical_failure",
            "status": AIObservationTechnicalStatus.API_ERROR.value,
            "metadata": {
                "model": config.model,
                "prompt_hash": None,
                "api_latency_seconds": elapsed_seconds,
                "attempts": 0,
            },
        },
        _AI_TECHNICAL_EXIT_CODE,
    )


async def _run_ai_check(
    text: str,
    config: MonitorConfig,
    *,
    matched_keywords: tuple[str, ...],
    notify_all: bool,
    trusted_area_context: str | None,
    message_age_seconds: int,
    client_factory: _AIClientFactory = build_openai_observation_client,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    live_config = replace(config.ai_observation, enabled=True)
    context = (
        live_config.default_trusted_area_context
        if trusted_area_context is None
        else trusted_area_context
    )
    try:
        current_time = now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        sent_at = current_time.astimezone(UTC) - timedelta(seconds=message_age_seconds)
        request = AIObservationRequest(
            message_text=text,
            sent_at=sent_at,
            message_age_seconds=message_age_seconds,
            trusted_area_context=context,
            matched_keywords=matched_keywords,
            notify_all=notify_all,
        )
    except (AttributeError, OverflowError, TypeError, ValueError) as error:
        raise ConfigurationError("Invalid ai-check input") from error

    started = monotonic()
    client: _AIClient | None = None
    payload_and_exit_code: tuple[dict[str, object], int] | None = None
    try:
        client = client_factory(live_config)
        if client is None:
            raise ConfigurationError("AI check could not initialize an enabled OpenAI client")
        outcome = await client.classify(
            request,
            timeout_seconds=live_config.operation_timeout_seconds,
        )
        if isinstance(outcome, (AIObservationSuccess, AIObservationFailure)):
            payload_and_exit_code = _ai_check_payload(outcome)
        else:
            payload_and_exit_code = _unexpected_ai_check_payload(
                live_config,
                elapsed_seconds=max(0.0, round(monotonic() - started, 3)),
            )
    except asyncio.CancelledError:
        raise
    except ConfigurationError:
        raise
    except Exception:
        payload_and_exit_code = _unexpected_ai_check_payload(
            live_config,
            elapsed_seconds=max(0.0, round(monotonic() - started, 3)),
        )
    finally:
        if client is not None:
            try:
                await client.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error("AI check client close failed")

    if payload_and_exit_code is None:  # pragma: no cover - defensive invariant.
        payload_and_exit_code = _unexpected_ai_check_payload(
            live_config,
            elapsed_seconds=max(0.0, round(monotonic() - started, 3)),
        )
    payload, exit_code = payload_and_exit_code
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"
    _configure_logging(stdout_is_data=command == "ai-check")

    try:
        ai_check_input: tuple[str, tuple[str, ...]] | None = None
        if command == "ai-check":
            ai_check_input = _read_ai_check_input(parser, args)
        if command == "run":
            asyncio.run(_run_monitor_with_signals(load_config()))
        elif command == "list-chats":
            asyncio.run(_list_chats())
        elif command == "generate-session":
            asyncio.run(_generate_session())
        elif command == "check":
            return _check_text(args.text, load_config())
        elif command == "ai-check":
            if ai_check_input is None:  # pragma: no cover - guarded before logging.
                parser.error("ai-check input is missing")
            text, matched_keywords = ai_check_input
            return asyncio.run(
                _run_ai_check(
                    text,
                    load_config(),
                    matched_keywords=matched_keywords,
                    notify_all=args.notify_all,
                    trusted_area_context=args.trusted_area_context,
                    message_age_seconds=args.message_age_seconds,
                )
            )
        else:  # pragma: no cover - argparse constrains the choices.
            parser.error(f"Unknown command: {command}")
    except ConfigurationError as error:
        LOGGER.error("Configuration error: %s", error)
        return 2
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 130
    return 0
