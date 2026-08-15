from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .models import ConfigurationError

OPENAI_API_KEY_ENV = "OPENAI_API_KEY"


def validate_openai_api_key(*, required: bool) -> None:
    """Validate presence of an OpenAI key without returning or logging its value."""

    if not required:
        return

    load_dotenv()
    api_key = os.getenv(OPENAI_API_KEY_ENV, "").strip()
    if not api_key or api_key.startswith("replace_"):
        raise ConfigurationError("OPENAI_API_KEY is required when ai_observation.enabled is true")


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    api_id: int
    api_hash: str
    session_string: str | None

    @classmethod
    def from_environment(cls, *, require_session: bool = True) -> TelegramCredentials:
        load_dotenv()
        raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip() or None

        try:
            api_id = int(raw_api_id)
        except ValueError as error:
            raise ConfigurationError("TELEGRAM_API_ID must be an integer") from error

        if api_id <= 0:
            raise ConfigurationError("TELEGRAM_API_ID is missing or invalid")
        if not api_hash or api_hash.startswith("replace_"):
            raise ConfigurationError("TELEGRAM_API_HASH is missing")
        if require_session and (not session_string or session_string.startswith("replace_")):
            raise ConfigurationError(
                "TELEGRAM_SESSION_STRING is missing; run `telegram-monitor generate-session`"
            )
        return cls(api_id=api_id, api_hash=api_hash, session_string=session_string)
