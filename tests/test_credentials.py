from __future__ import annotations

import pytest

import telegram_monitor.credentials as credentials_module
from telegram_monitor.credentials import validate_openai_api_key
from telegram_monitor.models import ConfigurationError


def test_openai_api_key_is_not_required_when_ai_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        credentials_module,
        "load_dotenv",
        lambda: pytest.fail("disabled AI must not load credential files"),
    )

    validate_openai_api_key(required=False)


def test_openai_api_key_accepts_an_opaque_non_placeholder_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "opaque-project-key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials_module, "load_dotenv", lambda: None)

    validate_openai_api_key(required=True)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "replace_with_your_openai_project_api_key"],
)
def test_openai_api_key_rejects_missing_or_placeholder_values_without_echoing_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    value: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", value)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(credentials_module, "load_dotenv", lambda: None)

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY") as raised:
        validate_openai_api_key(required=True)

    if value.strip():
        assert value not in str(raised.value)
