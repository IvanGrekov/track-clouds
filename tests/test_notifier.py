from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from contextlib import suppress
from threading import Event

import httpx
import pytest

from telegram_monitor.models import ConfigurationError
from telegram_monitor.notifier import (
    NotificationError,
    TelegramBotApi,
    TelegramBotNotifier,
    TelegramDialogNotifier,
)
from telegram_monitor.subscriber_store import Subscriber, SubscriberStore


class FakeTelegramClient:
    def __init__(self) -> None:
        self.call: tuple[object, str, dict[str, object]] | None = None

    async def send_message(self, target: object, text: str, **kwargs: object) -> None:
        self.call = (target, text, kwargs)


def _subscriber(chat_id: int) -> Subscriber:
    return Subscriber(chat_id=chat_id, user_id=chat_id, username=None, first_name=None)


def _open_store(*chat_ids: int, limit: int = 10) -> SubscriberStore:
    store = SubscriberStore(":memory:", limit)
    store.open()
    store.select_bot(999)
    for chat_id in chat_ids:
        store.subscribe(_subscriber(chat_id))
    return store


@pytest.mark.asyncio
async def test_dialog_notifier_sends_plain_text_without_preview() -> None:
    client = FakeTelegramClient()
    notifier = TelegramDialogNotifier(client, "me")

    await notifier.start()
    await notifier.send("alert")
    await notifier.close()

    assert client.call == (
        "me",
        "alert",
        {"parse_mode": None, "link_preview": False},
    )


@pytest.mark.asyncio
async def test_bot_notifier_broadcasts_plain_text_without_formatting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: list[dict[str, object]] = []
    caplog.set_level(logging.DEBUG)

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=_open_store(456, 789),
    )
    await notifier.send("alert")
    await notifier.close()

    assert captured == [
        {
            "chat_id": 456,
            "text": "alert",
            "link_preview_options": {"is_disabled": True},
        },
        {
            "chat_id": 789,
            "text": "alert",
            "link_preview_options": {"is_disabled": True},
        },
    ]
    assert "123:secret" not in caplog.text
    assert "Bot alert broadcast started (total=2)" in caplog.text
    assert "Bot alert delivered to 2/2 subscriber(s)" in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_bot_notifier_keeps_event_loop_responsive_during_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _open_store()
    original_list_chat_ids = store.list_chat_ids
    entered_store_call = Event()
    release_store_call = Event()

    def blocked_list_chat_ids() -> tuple[int, ...]:
        entered_store_call.set()
        if not release_store_call.wait(timeout=1):
            raise AssertionError("SQLite access blocked the asyncio event loop")
        return original_list_chat_ids()

    monkeypatch.setattr(store, "list_chat_ids", blocked_list_chat_ids)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        store=store,
    )

    async def release_after_store_call_starts() -> None:
        while not entered_store_call.is_set():
            await asyncio.sleep(0)
        release_store_call.set()

    releaser = asyncio.create_task(release_after_store_call_starts())
    try:
        await notifier.send("alert")
        await asyncio.wait_for(releaser, timeout=1)
    finally:
        release_store_call.set()
        if not releaser.done():
            releaser.cancel()
        with suppress(asyncio.CancelledError):
            await releaser
        await notifier.close()


@pytest.mark.asyncio
async def test_bot_api_does_not_expose_token_in_transport_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed for " + str(request.url), request=request)

    api = TelegramBotApi("123:very-secret", transport=httpx.MockTransport(handler))

    with pytest.raises(NotificationError) as caught:
        await api.send_message(456, "alert")
    await api.close()

    assert "very-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_bot_api_exposes_retry_after_without_exposing_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )

    api = TelegramBotApi("123:very-secret", transport=httpx.MockTransport(handler))

    with pytest.raises(NotificationError) as caught:
        await api.send_message(456, "alert")
    await api.close()

    assert caught.value.retryable is True
    assert caught.value.retry_after == 7
    assert caught.value.error_code == 429
    assert "very-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_broadcast_retries_only_failed_recipient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: Counter[int] = Counter()
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        chat_id = int(payload["chat_id"])
        calls[chat_id] += 1
        if chat_id == 2 and calls[chat_id] == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        delivery_attempts=2,
        delivery_retry_base_seconds=0,
        delivery_retry_max_seconds=0,
        transport=httpx.MockTransport(handler),
        store=_open_store(1, 2, 3),
    )

    await notifier.send("alert")
    await notifier.close()

    assert calls == Counter({2: 2, 1: 1, 3: 1})
    assert (
        "Bot API alert delivery to chat_id=2 failed "
        "(error_code=429, retry_after=0.0); retrying in 0.0s (1/2)" in caplog.text
    )
    assert "Bot alert delivered to 3/3 subscriber(s)" in caplog.text


@pytest.mark.parametrize(
    ("failing_chat_ids", "expected_status", "expected_delivered"),
    (
        ({2}, "partial", 2),
        ({1, 2, 3}, "failed", 0),
    ),
)
@pytest.mark.asyncio
async def test_broadcast_logs_permanent_delivery_failures(
    caplog: pytest.LogCaptureFixture,
    failing_chat_ids: set[int],
    expected_status: str,
    expected_delivered: int,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["chat_id"] in failing_chat_ids:
            return httpx.Response(
                500,
                json={"ok": False, "error_code": 500, "description": "temporary outage"},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        delivery_attempts=1,
        transport=httpx.MockTransport(handler),
        store=_open_store(1, 2, 3),
    )

    await notifier.send("private alert text")
    await notifier.close()

    failed_ids = ",".join(str(chat_id) for chat_id in sorted(failing_chat_ids))
    assert (
        f"status={expected_status}, delivered={expected_delivered}, "
        f"failed={len(failing_chat_ids)}, total=3, failed_chat_ids={failed_ids}" in caplog.text
    )
    for chat_id in failing_chat_ids:
        assert (
            f"Could not send Bot API alert to chat_id={chat_id} after 1 attempt(s) "
            "(error_code=500, retryable=True, retry_after=None)" in caplog.text
        )
    assert "no further automatic retry is scheduled" in caplog.text
    assert "Bot alert delivered to" not in caplog.text
    assert "private alert text" not in caplog.text
    assert "123:secret" not in caplog.text


@pytest.mark.asyncio
async def test_broadcast_removes_blocked_subscriber_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")
    store = _open_store(1, 2, 3)

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["chat_id"] == 2:
            return httpx.Response(
                403,
                json={"ok": False, "error_code": 403, "description": "bot was blocked"},
            )
        return httpx.Response(200, json={"ok": True, "result": {}})

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.send("alert")
    assert store.list_chat_ids() == (1, 3)
    assert any(
        "Removed user (chat_id=2" in record.getMessage()
        and "reason=unreachable" in record.getMessage()
        for record in caplog.records
    )
    assert (
        "Bot API alert was not delivered to an unreachable subscriber "
        "(chat_id=2, error_code=403); removing the subscriber" in caplog.text
    )
    assert "status=partial, delivered=2, failed=1, total=3, failed_chat_ids=2" in caplog.text
    await notifier.close()


def test_bot_notifier_requires_token(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        TelegramBotNotifier.from_environment(":memory:")
