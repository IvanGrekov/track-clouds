from pathlib import Path

import pytest

from telegram_monitor.config import load_config
from telegram_monitor.models import ConfigurationError


def _write_config(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_load_config_builds_monitor_and_source_rules(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
notification_mode = "bot"
bot_subscriber_limit = 7
timezone = "Europe/Kyiv"

[[sources]]
peer = "@updates"
notify_all = true
label = "Updates"

[[sources]]
peer = -1001234567890
keywords = [" release ", "incident"]
keywords_to_skip = ["spam"]
""",
    )

    config = load_config(path)

    assert config.notification_mode == "bot"
    assert config.bot_subscriber_limit == 7
    assert config.sources[0].peer == "@updates"
    assert config.sources[0].notify_all is True
    assert config.sources[1].peer == -1001234567890
    assert config.sources[1].keywords == ("release", "incident")
    assert config.sources[1].keywords_to_skip == ("spam",)


def test_load_config_reports_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigurationError, match="Configuration file not found"):
        load_config(path)


def test_load_config_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path, 'notification_mode = "bot" trailing')

    with pytest.raises(ConfigurationError, match="Invalid TOML"):
        load_config(path)


def test_load_config_rejects_unknown_options(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(
        path,
        """
unknown_option = true

[[sources]]
peer = "@updates"
notify_all = true
""",
    )

    with pytest.raises(ConfigurationError, match="unknown_option"):
        load_config(path)


def test_load_config_requires_sources_tables(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write_config(path, 'notification_mode = "saved_messages"')

    with pytest.raises(ConfigurationError, match=r"\[\[sources\]\]"):
        load_config(path)
