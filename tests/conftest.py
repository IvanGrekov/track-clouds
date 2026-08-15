from __future__ import annotations

import pytest

import telegram_monitor.config as config_module
import telegram_monitor.credentials as credentials_module
import telegram_monitor.notifier as notifier_module


@pytest.fixture(autouse=True)
def _isolate_tests_from_local_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic and prevent discovery of an ignored local ``.env``."""

    monkeypatch.setattr(config_module, "load_dotenv", lambda: False)
    monkeypatch.setattr(credentials_module, "load_dotenv", lambda: False)
    monkeypatch.setattr(notifier_module, "load_dotenv", lambda: False)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
