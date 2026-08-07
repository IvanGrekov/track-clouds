from __future__ import annotations

import logging

import pytest

import telegram_monitor.cli as cli
from telegram_monitor.cli import _DIFFERENCE_LOG_FILTER, _configure_logging
from telegram_monitor.models import MonitorConfig, SourceRule


def test_configure_logging_hides_only_channel_difference_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates_logger = logging.getLogger("telethon.client.updates")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    _configure_logging()

    matching_record = logging.LogRecord(
        "telethon.client.updates",
        logging.INFO,
        __file__,
        1,
        "Got difference for channel %d updates",
        (1_918_321_848,),
        None,
    )
    other_info_record = logging.LogRecord(
        "telethon.client.updates",
        logging.INFO,
        __file__,
        1,
        "Other useful Telethon information",
        (),
        None,
    )
    account_difference_record = logging.LogRecord(
        "telethon.client.updates",
        logging.INFO,
        __file__,
        1,
        "Got difference for account updates",
        (),
        None,
    )
    matching_warning_record = logging.LogRecord(
        "telethon.client.updates",
        logging.WARNING,
        __file__,
        1,
        "Got difference for channel %d updates",
        (1_918_321_848,),
        None,
    )

    assert _DIFFERENCE_LOG_FILTER in updates_logger.filters
    assert _DIFFERENCE_LOG_FILTER.filter(matching_record) is False
    assert _DIFFERENCE_LOG_FILTER.filter(account_difference_record) is False
    assert _DIFFERENCE_LOG_FILTER.filter(other_info_record) is True
    assert _DIFFERENCE_LOG_FILTER.filter(matching_warning_record) is True


def test_check_text_applies_keywords_to_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "CONFIG",
        MonitorConfig(
            sources=(
                SourceRule(
                    peer="@chat",
                    keywords=("k8s",),
                    keywords_to_skip=("spam",),
                ),
            )
        ),
    )

    assert cli._check_text("k8s spam post") == 1
    assert capsys.readouterr().out == "SKIP  @chat: keywords_to_skip=spam\n"
    assert cli._check_text("  k8s  ") == 1
    assert capsys.readouterr().out == "SKIP  @chat: fewer than 10 characters\n"
    assert cli._check_text("k8s release") == 0
    assert capsys.readouterr().out == "MATCH @chat: k8s\n"
