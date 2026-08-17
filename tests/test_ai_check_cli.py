from __future__ import annotations

import io
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import telegram_monitor.cli as cli
import telegram_monitor.config as config_module
import telegram_monitor.credentials as credentials_module
from telegram_monitor.ai_models import (
    AIDecision,
    AIObservationResult,
    AIObservationTechnicalStatus,
    AIReasonCode,
)
from telegram_monitor.models import (
    AIObservationConfig,
    ConfigurationError,
    MonitorConfig,
    SourceRule,
)
from telegram_monitor.openai_client import (
    AIObservationFailure,
    AIObservationRequest,
    AIObservationSuccess,
    AIObservationTokenUsage,
)


@pytest.fixture(autouse=True)
def _forbid_dotenv_and_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_dotenv() -> None:
        pytest.fail("AI CLI unit tests must not resolve dotenv files")

    def forbidden_telegram(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("ai-check must not construct a Telegram client")

    monkeypatch.setattr(config_module, "load_dotenv", forbidden_dotenv)
    monkeypatch.setattr(credentials_module, "load_dotenv", forbidden_dotenv)
    monkeypatch.setattr(cli, "create_client", forbidden_telegram)
    monkeypatch.setattr(cli, "create_login_client", forbidden_telegram)


def _config(**ai_overrides: object) -> MonitorConfig:
    values: dict[str, object] = {
        "enabled": False,
        "default_trusted_area_context": "Львів",
        "operation_timeout_seconds": 17,
    }
    values.update(ai_overrides)
    return MonitorConfig(
        sources=(SourceRule(peer="@source", keywords=("road",)),),
        ai_observation=AIObservationConfig(**values),  # type: ignore[arg-type]
    )


def _result(decision: AIDecision) -> AIObservationResult:
    if decision is AIDecision.ACCEPT:
        return AIObservationResult(
            decision=decision,
            location="Городоцька",
            event="перекрито рух",
        )
    return AIObservationResult(
        decision=decision,
        reason_code=AIReasonCode.UNRELATED_CONTENT,
        reason="Повідомлення явно не стосується стану маршруту.",
    )


def _success(decision: AIDecision) -> AIObservationSuccess:
    return AIObservationSuccess(
        result=_result(decision),
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="a" * 64,
        api_latency_seconds=0.321,
        attempts=1,
        token_usage=AIObservationTokenUsage(100, 20, 120),
    )


def _notify_all_success() -> AIObservationSuccess:
    return AIObservationSuccess(
        result=AIObservationResult(
            decision=AIDecision.ACCEPT,
        ),
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="a" * 64,
        api_latency_seconds=0.321,
        attempts=1,
        token_usage=AIObservationTokenUsage(100, 20, 120),
    )


def _failure(status: AIObservationTechnicalStatus) -> AIObservationFailure:
    return AIObservationFailure(
        status=status,
        model="gpt-5.4-nano-2026-03-17",
        prompt_hash="b" * 64,
        api_latency_seconds=0.654,
        attempts=2,
    )


class FakeAIClient:
    def __init__(self, outcome: object, *, close_error: Exception | None = None) -> None:
        self.outcome = outcome
        self.close_error = close_error
        self.classify_calls: list[tuple[AIObservationRequest, float]] = []
        self.close_calls = 0

    async def classify(
        self,
        request: AIObservationRequest,
        *,
        timeout_seconds: float,
    ) -> object:
        self.classify_calls.append((request, timeout_seconds))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = json.loads(captured.out)
    assert isinstance(parsed, dict)
    assert captured.out.count("\n{") == 0
    return parsed


def test_live_guard_stops_before_loading_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: pytest.fail("configuration must not load without --live"),
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(["ai-check", "--matched-keyword", "road", "road is blocked"])

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert "--live" in captured.err


@pytest.mark.parametrize(
    "argv",
    (
        ["ai-check", "--live", "--matched-keyword", "road"],
        [
            "ai-check",
            "--live",
            "--stdin",
            "--matched-keyword",
            "road",
            "positional text",
        ],
    ),
)
def test_ai_check_requires_exactly_one_text_input(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("stdin text"))
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: pytest.fail("invalid input must stop before configuration loading"),
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2
    assert "exactly one" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    (
        ["ai-check", "--live", "message text"],
        ["ai-check", "--live", "--matched-keyword", "  ", "message text"],
    ),
)
def test_ai_check_requires_keyword_or_notify_all_before_configuration(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: pytest.fail("invalid prefilter must stop before configuration loading"),
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(argv)

    assert raised.value.code == 2
    assert "--matched-keyword or --notify-all" in capsys.readouterr().err


def test_main_accepts_stdin_and_notify_all_without_constructing_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    observed: dict[str, object] = {}

    async def fake_run_ai_check(text: str, built_config: MonitorConfig, **kwargs: object) -> int:
        observed.update(text=text, config=built_config, **kwargs)
        return 0

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("stdin route report\n"))
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_run_ai_check", fake_run_ai_check)

    result = cli.main(
        [
            "ai-check",
            "--live",
            "--stdin",
            "--notify-all",
            "--message-age-seconds",
            "8",
        ]
    )

    assert result == 0
    assert observed == {
        "text": "stdin route report",
        "config": config,
        "matched_keywords": (),
        "notify_all": True,
        "trusted_area_context": None,
        "message_age_seconds": 8,
    }


def test_main_accepts_positional_text_and_multiple_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    observed: dict[str, object] = {}

    async def fake_run_ai_check(text: str, built_config: MonitorConfig, **kwargs: object) -> int:
        observed.update(text=text, config=built_config, **kwargs)
        return 0

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_run_ai_check", fake_run_ai_check)

    exit_code = cli.main(
        [
            "ai-check",
            "--live",
            "--matched-keyword",
            "  хмар  ",
            "--matched-keyword",
            "зелен",
            "--trusted-area-context",
            "Львів",
            "route report",
        ]
    )

    assert exit_code == 0
    assert observed["text"] == "route report"
    assert observed["matched_keywords"] == ("хмар", "зелен")
    assert observed["notify_all"] is False
    assert observed["trusted_area_context"] == "Львів"


def test_ai_check_logging_never_writes_logs_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configured: dict[str, object] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: configured.update(kwargs))

    cli._configure_logging(stdout_is_data=True)

    handlers = configured["handlers"]
    assert configured["force"] is True
    assert isinstance(handlers, tuple)
    assert len(handlers) == 1
    handler = handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    logger = logging.Logger("test.ai-check.logging", level=logging.INFO)
    logger.addHandler(handler)
    logger.info("diagnostic log")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "diagnostic log" in captured.err


def test_configure_logging_caps_openai_and_http_loggers_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_loggers = tuple(
        logging.getLogger(logger_name) for logger_name in ("openai", "httpx", "httpcore")
    )
    for logger in protected_loggers:
        monkeypatch.setattr(logger, "level", logging.DEBUG)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)

    cli._configure_logging(stdout_is_data=True)

    for logger in protected_loggers:
        assert logger.level == logging.WARNING
        assert logger.getEffectiveLevel() == logging.WARNING


@pytest.mark.asyncio
async def test_run_ai_check_maps_request_and_uses_disabled_config_for_one_live_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(enabled=False)
    client = FakeAIClient(_success(AIDecision.ACCEPT))
    factory_configs: list[AIObservationConfig] = []

    def factory(ai_config: AIObservationConfig) -> Any:
        factory_configs.append(ai_config)
        return client

    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone(timedelta(hours=3)))
    input_marker = "На Городоцькій зараз перекрито рух marker-input"
    exit_code = await cli._run_ai_check(
        input_marker,
        config,
        matched_keywords=("перекри", "дорог"),
        notify_all=False,
        trusted_area_context=None,
        message_age_seconds=8,
        client_factory=factory,
        now=lambda: now,
    )

    assert exit_code == 0
    assert len(factory_configs) == 1
    assert factory_configs[0] == replace(config.ai_observation, enabled=True)
    assert config.ai_observation.enabled is False
    assert len(client.classify_calls) == 1
    request, timeout_seconds = client.classify_calls[0]
    assert request.message_text == input_marker
    assert request.sent_at == datetime(2026, 8, 15, 8, 59, 52, tzinfo=UTC)
    assert request.sent_at.utcoffset() == timedelta(0)
    assert request.message_age_seconds == 8
    assert request.trusted_area_context == "Львів"
    assert request.matched_keywords == ("перекри", "дорог")
    assert request.notify_all is False
    assert timeout_seconds == 17
    assert client.close_calls == 1
    payload = _payload(capsys)
    assert payload["kind"] == "semantic"
    assert input_marker not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_explicit_area_context_overrides_configured_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeAIClient(_success(AIDecision.ACCEPT))

    await cli._run_ai_check(
        "На дорозі зараз перекрито рух",
        _config(),
        matched_keywords=("перекри",),
        notify_all=False,
        trusted_area_context="Київ",
        message_age_seconds=0,
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
        now=lambda: datetime(2026, 8, 15, tzinfo=UTC),
    )

    request, _ = client.classify_calls[0]
    assert request.trusted_area_context == "Київ"
    _payload(capsys)


@pytest.mark.asyncio
async def test_notify_all_live_check_preserves_request_context_and_nullable_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeAIClient(_notify_all_success())

    exit_code = await cli._run_ai_check(
        "Повідомлення з джерела, де дозволені всі дописи",
        _config(),
        matched_keywords=(),
        notify_all=True,
        trusted_area_context=None,
        message_age_seconds=0,
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
    )

    assert exit_code == 0
    request, _ = client.classify_calls[0]
    assert request.matched_keywords == ()
    assert request.notify_all is True
    payload = _payload(capsys)
    assert payload["kind"] == "semantic"
    assert payload["decision"] == "accept"
    assert set(payload) == {"kind", "decision", "metadata"}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", list(AIDecision))
async def test_semantic_decisions_are_successful_json_results(
    decision: AIDecision,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeAIClient(_success(decision))

    exit_code = await cli._run_ai_check(
        "На маршруті є достатньо довге повідомлення",
        _config(),
        matched_keywords=("маршрут",),
        notify_all=False,
        trusted_area_context=None,
        message_age_seconds=0,
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
    )

    assert exit_code == 0
    assert client.close_calls == 1
    payload = _payload(capsys)
    assert payload["kind"] == "semantic"
    assert payload["decision"] == decision.value
    semantic_fields = set(payload) - {"kind", "decision", "metadata"}
    if decision is AIDecision.ACCEPT:
        assert semantic_fields == {"location", "event"}
    else:
        assert semantic_fields == {"reason_code", "reason"}
    assert payload["metadata"] == {
        "model": "gpt-5.4-nano-2026-03-17",
        "prompt_hash": "a" * 64,
        "api_latency_seconds": 0.321,
        "attempts": 1,
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(AIObservationTechnicalStatus))
async def test_technical_statuses_are_exit_three_without_semantic_fields(
    status: AIObservationTechnicalStatus,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeAIClient(_failure(status))

    exit_code = await cli._run_ai_check(
        "На маршруті є достатньо довге повідомлення",
        _config(),
        matched_keywords=("маршрут",),
        notify_all=False,
        trusted_area_context=None,
        message_age_seconds=0,
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
    )

    assert exit_code == 3
    assert client.close_calls == 1
    payload = _payload(capsys)
    assert payload["kind"] == "technical_failure"
    assert payload["status"] == status.value
    for semantic_field in (
        "decision",
        "location",
        "event",
        "reason_code",
        "reason",
    ):
        assert semantic_field not in payload


@pytest.mark.asyncio
async def test_unexpected_classify_error_is_safe_and_client_is_closed_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_marker = "raw-api-response-and-key-marker"
    client = FakeAIClient(RuntimeError(secret_marker))

    exit_code = await cli._run_ai_check(
        "На маршруті є sensitive-message-marker",
        _config(),
        matched_keywords=("маршрут",),
        notify_all=False,
        trusted_area_context=None,
        message_age_seconds=0,
        client_factory=lambda config: client,  # type: ignore[arg-type,return-value]
        monotonic=iter((10.0, 10.125, 10.250)).__next__,
    )

    assert exit_code == 3
    assert client.close_calls == 1
    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
    assert "sensitive-message-marker" not in captured.out
    payload = json.loads(captured.out)
    assert payload["kind"] == "technical_failure"
    assert payload["status"] == "api_error"
    assert payload["metadata"]["attempts"] == 0


@pytest.mark.asyncio
async def test_close_error_preserves_semantic_result_and_logs_safely(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_marker = "raw-close-exception-marker"
    client = FakeAIClient(
        _success(AIDecision.ACCEPT),
        close_error=RuntimeError(secret_marker),
    )
    caplog.set_level(logging.ERROR, logger="telegram_monitor.cli")

    exit_code = await cli._run_ai_check(
        "На маршруті є sensitive-message-marker",
        _config(),
        matched_keywords=("маршрут",),
        notify_all=False,
        trusted_area_context=None,
        message_age_seconds=0,
        client_factory=lambda config: client,
    )

    assert exit_code == 0
    assert client.close_calls == 1
    captured = capsys.readouterr()
    assert secret_marker not in captured.out
    assert secret_marker not in captured.err
    assert secret_marker not in caplog.text
    payload = json.loads(captured.out)
    assert payload["kind"] == "semantic"
    assert payload["metadata"]["attempts"] == 1
    assert payload["metadata"]["prompt_hash"] == "a" * 64
    assert "AI check client close failed" in caplog.text


@pytest.mark.asyncio
async def test_factory_configuration_error_is_propagated_without_reading_dotenv() -> None:
    def failed_factory(config: AIObservationConfig) -> None:
        del config
        raise ConfigurationError("safe configuration guidance")

    with pytest.raises(ConfigurationError, match="safe configuration guidance"):
        await cli._run_ai_check(
            "На маршруті є достатньо довге повідомлення",
            _config(),
            matched_keywords=("маршрут",),
            notify_all=False,
            trusted_area_context=None,
            message_age_seconds=0,
            client_factory=failed_factory,
        )


def test_main_returns_two_for_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: (_ for _ in ()).throw(ConfigurationError("OPENAI_API_KEY is required")),
    )
    caplog.set_level("ERROR", logger="telegram_monitor.cli")

    exit_code = cli.main(["ai-check", "--live", "--matched-keyword", "road", "road is blocked"])

    assert exit_code == 2
    assert "OPENAI_API_KEY is required" in caplog.text
    assert "sk-" not in caplog.text


def test_legacy_check_remains_offline_and_does_not_build_ai_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(cli, "load_config", lambda: _config(enabled=True))
    monkeypatch.setattr(
        cli,
        "build_openai_observation_client",
        lambda config: pytest.fail("legacy check must not construct an OpenAI client"),
    )

    exit_code = cli.main(["check", "road is blocked"])

    assert exit_code == 0
    assert capsys.readouterr().out == "MATCH @source: road\n"
