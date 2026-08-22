from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import telegram_monitor.app as app
from telegram_monitor.ai_observer import UnavailableAIObserver, build_ai_observer
from telegram_monitor.models import (
    AIObservationConfig,
    ConfigurationError,
    MonitorConfig,
    QuietHoursConfig,
    SourceRule,
)


def _config(*, ai_enabled: bool = False) -> MonitorConfig:
    return MonitorConfig(
        sources=(SourceRule(peer="@source", keywords=("road",)),),
        ai_observation=AIObservationConfig(enabled=ai_enabled),
    )


class FakeClient:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def connect(self) -> None:
        self._events.append("client.connect")

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=123)

    async def run_until_disconnected(self) -> None:
        self._events.append("client.run")

    async def disconnect(self) -> None:
        self._events.append("client.disconnect")


class FakeNotifier:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.close_calls = 0

    async def start(self) -> None:
        return None

    async def send(self, text: str) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self._events.append("notifier.close")


class FakeObserver:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self._events.append("observer.close")


class FakeMonitor:
    def __init__(
        self,
        *,
        client: object,
        config: MonitorConfig,
        notifier: FakeNotifier,
        ai_observer: FakeObserver | None,
        accept_events_since: datetime | None,
        events: list[str],
        fail_prepare: bool = False,
    ) -> None:
        self.client = client
        self.config = config
        self.notifier = notifier
        self.ai_observer = ai_observer
        self.accept_events_since = accept_events_since
        self._events = events
        self._fail_prepare = fail_prepare

    def start_capture(self) -> None:
        self._events.append("monitor.start_capture")

    async def prepare(self) -> tuple[str, ...]:
        self._events.append("monitor.prepare")
        if self._fail_prepare:
            raise RuntimeError("prepare failed")
        return ()

    async def close(self, *, discard_pending: bool = False) -> None:
        self._events.append("monitor.close.discard" if discard_pending else "monitor.close")
        try:
            await self.notifier.close()
        finally:
            if self.ai_observer is not None:
                await self.ai_observer.close()


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: FakeClient,
    notifier: FakeNotifier,
) -> None:
    monkeypatch.setattr(
        app,
        "TelegramCredentials",
        SimpleNamespace(from_environment=lambda: object()),
    )
    monkeypatch.setattr(app, "create_client", lambda credentials: client)
    monkeypatch.setattr(app, "build_notifier", lambda built_client, config: notifier)


@pytest.mark.asyncio
async def test_run_monitor_builds_optional_observer_once_and_transfers_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)
    observer = FakeObserver(events)
    config = _config(ai_enabled=True)
    observer_configs: list[AIObservationConfig] = []
    monitors: list[FakeMonitor] = []

    _patch_runtime(monkeypatch, client=client, notifier=notifier)

    def build_observer(value: AIObservationConfig) -> FakeObserver:
        observer_configs.append(value)
        return observer

    def build_monitor(**kwargs: object) -> FakeMonitor:
        monitor = FakeMonitor(events=events, **kwargs)  # type: ignore[arg-type]
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(app, "build_ai_observer", build_observer)
    monkeypatch.setattr(app, "TelegramMonitor", build_monitor)

    await app.run_monitor(config)

    assert observer_configs == [config.ai_observation]
    assert len(monitors) == 1
    assert monitors[0].ai_observer is observer
    assert notifier.close_calls == 1
    assert observer.close_calls == 1
    assert events[-5:] == [
        "client.run",
        "monitor.close",
        "notifier.close",
        "observer.close",
        "client.disconnect",
    ]
    assert events[-1] == "client.disconnect"


@pytest.mark.asyncio
async def test_run_monitor_passes_disabled_observer_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)
    config = _config()
    observer_configs: list[AIObservationConfig] = []
    monitors: list[FakeMonitor] = []

    _patch_runtime(monkeypatch, client=client, notifier=notifier)

    def build_observer(value: AIObservationConfig) -> None:
        observer_configs.append(value)
        return None

    def build_monitor(**kwargs: object) -> FakeMonitor:
        monitor = FakeMonitor(events=events, **kwargs)  # type: ignore[arg-type]
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(app, "build_ai_observer", build_observer)
    monkeypatch.setattr(app, "TelegramMonitor", build_monitor)

    await app.run_monitor(config)

    assert observer_configs == [config.ai_observation]
    assert monitors[0].ai_observer is None
    assert notifier.close_calls == 1


@pytest.mark.asyncio
async def test_run_monitor_keeps_running_with_fail_open_unavailable_observer(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)
    config = _config(ai_enabled=True)
    monitors: list[FakeMonitor] = []

    _patch_runtime(monkeypatch, client=client, notifier=notifier)

    def fail_client_setup(value: AIObservationConfig) -> None:
        raise ConfigurationError("private setup detail")

    def build_fail_open_observer(value: AIObservationConfig):
        return build_ai_observer(value, client_factory=fail_client_setup)

    def build_monitor(**kwargs: object) -> FakeMonitor:
        monitor = FakeMonitor(events=events, **kwargs)  # type: ignore[arg-type]
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(app, "build_ai_observer", build_fail_open_observer)
    monkeypatch.setattr(app, "TelegramMonitor", build_monitor)

    await app.run_monitor(config)

    assert isinstance(monitors[0].ai_observer, UnavailableAIObserver)
    assert "client.run" in events
    assert "AI observation setup failed" in caplog.text
    assert "private setup detail" not in caplog.text


@pytest.mark.asyncio
async def test_run_monitor_closes_unowned_resources_when_monitor_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)
    observer = FakeObserver(events)

    _patch_runtime(monkeypatch, client=client, notifier=notifier)
    monkeypatch.setattr(app, "build_ai_observer", lambda config: observer)

    def fail_monitor_construction(**kwargs: object) -> None:
        raise RuntimeError("monitor construction failed")

    monkeypatch.setattr(app, "TelegramMonitor", fail_monitor_construction)

    with pytest.raises(RuntimeError, match="monitor construction failed"):
        await app.run_monitor(_config(ai_enabled=True))

    assert notifier.close_calls == 1
    assert observer.close_calls == 1
    assert events[-3:] == ["notifier.close", "observer.close", "client.disconnect"]


@pytest.mark.asyncio
async def test_run_monitor_closes_notifier_when_observer_factory_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)

    _patch_runtime(monkeypatch, client=client, notifier=notifier)

    def fail_observer_setup(config: AIObservationConfig) -> None:
        raise RuntimeError("unexpected observer factory failure")

    monkeypatch.setattr(app, "build_ai_observer", fail_observer_setup)

    with pytest.raises(RuntimeError, match="unexpected observer factory failure"):
        await app.run_monitor(_config(ai_enabled=True))

    assert notifier.close_calls == 1
    assert events[-2:] == ["notifier.close", "client.disconnect"]


@pytest.mark.asyncio
async def test_run_monitor_closes_owned_resources_after_prepare_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    notifier = FakeNotifier(events)
    observer = FakeObserver(events)

    _patch_runtime(monkeypatch, client=client, notifier=notifier)
    monkeypatch.setattr(app, "build_ai_observer", lambda config: observer)
    monkeypatch.setattr(
        app,
        "TelegramMonitor",
        lambda **kwargs: FakeMonitor(events=events, fail_prepare=True, **kwargs),
    )

    with pytest.raises(RuntimeError, match="prepare failed"):
        await app.run_monitor(_config(ai_enabled=True))

    assert notifier.close_calls == 1
    assert observer.close_calls == 1
    assert events[-4:] == [
        "monitor.close",
        "notifier.close",
        "observer.close",
        "client.disconnect",
    ]


@pytest.mark.asyncio
async def test_run_monitor_stays_offline_when_started_during_quiet_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopTest(RuntimeError):
        pass

    def fail_client_creation(credentials: object) -> None:
        del credentials
        raise AssertionError("Telegram client must not be created during quiet hours")

    async def stop_sleep(seconds: float) -> None:
        assert seconds == 5 * 60 * 60
        raise StopTest

    monkeypatch.setattr(app, "create_client", fail_client_creation)
    config = MonitorConfig(
        sources=(SourceRule(peer="@source", keywords=("road",)),),
        quiet_hours=QuietHoursConfig(enabled=True),
    )

    with pytest.raises(StopTest):
        await app.run_monitor(
            config,
            now=lambda: datetime(2026, 8, 22, 23, 0, tzinfo=UTC),
            sleep=stop_sleep,
        )


@pytest.mark.asyncio
async def test_run_monitor_cancels_active_session_at_quiet_hours_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopTest(RuntimeError):
        pass

    events: list[str] = []

    class BlockingClient(FakeClient):
        def __init__(self, recorded_events: list[str]) -> None:
            super().__init__(recorded_events)
            self._disconnected = asyncio.Event()

        async def run_until_disconnected(self) -> None:
            self._events.append("client.run")
            await self._disconnected.wait()

        async def disconnect(self) -> None:
            self._events.append("client.disconnect")
            self._disconnected.set()

    class AdvancingClock:
        def __init__(self) -> None:
            self.current = datetime(2026, 8, 22, 4, 0, tzinfo=UTC)
            self.sleeps: list[float] = []

        def now(self) -> datetime:
            return self.current

        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            if len(self.sleeps) == 1:
                self.current += timedelta(seconds=seconds)
                return
            raise StopTest

    clock = AdvancingClock()
    client = BlockingClient(events)
    notifier = FakeNotifier(events)
    observer = FakeObserver(events)
    monitors: list[FakeMonitor] = []
    monkeypatch.setattr(
        app,
        "TelegramCredentials",
        SimpleNamespace(from_environment=lambda: object()),
    )
    monkeypatch.setattr(app, "create_client", lambda credentials: client)
    monkeypatch.setattr(app, "build_notifier", lambda built_client, config: notifier)
    monkeypatch.setattr(app, "build_ai_observer", lambda config: observer)

    def build_monitor(**kwargs: object) -> FakeMonitor:
        monitor = FakeMonitor(events=events, **kwargs)  # type: ignore[arg-type]
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(app, "TelegramMonitor", build_monitor)
    config = MonitorConfig(
        sources=(SourceRule(peer="@source", keywords=("road",)),),
        quiet_hours=QuietHoursConfig(enabled=True),
        ai_observation=AIObservationConfig(enabled=True),
    )

    with pytest.raises(StopTest):
        await app.run_monitor(config, now=clock.now, sleep=clock.sleep)

    assert len(monitors) == 1
    assert clock.sleeps == [18.5 * 60 * 60, 5.5 * 60 * 60]
    assert "monitor.close.discard" in events
    assert events[-4:] == [
        "monitor.close.discard",
        "notifier.close",
        "observer.close",
        "client.disconnect",
    ]
