from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

import httpx
import pytest

from telegram_monitor.models import ConfigurationError, QuietHoursConfig
from telegram_monitor.notifier import TelegramBotNotifier
from telegram_monitor.subscriber_store import SubscriberStore


def _update(
    update_id: int,
    chat_id: int,
    text: str,
    *,
    chat_type: str = "private",
    sent_at: datetime | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id,
        "from": {
            "id": chat_id,
            "username": f"user{chat_id}",
            "first_name": f"User {chat_id}",
        },
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }
    if sent_at is not None:
        message["date"] = int(sent_at.timestamp())
    return {
        "update_id": update_id,
        "message": message,
    }


@pytest.mark.asyncio
async def test_start_stop_and_limit_replies(caplog: pytest.LogCaptureFixture) -> None:
    replies: list[tuple[int, str]] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        replies.append((payload["chat_id"], payload["text"]))
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:", subscriber_limit=2)
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start"))
    await notifier.process_update(_update(2, 102, "/start payload"))
    await notifier.process_update(_update(3, 101, "/start@ExampleBot"))
    await notifier.process_update(_update(4, 103, "/start"))

    assert set(store.list_chat_ids()) == {101, 102}
    assert replies[0] == (101, "✅ Ви підписалися на сповіщення.")
    assert replies[2] == (101, "✅ Ви вже підписані на сповіщення.")
    assert replies[3] == (
        103,
        "❌ Максимальна кількість користувачів перевищена (ліміт: 2).",
    )

    await notifier.process_update(_update(5, 101, "/stop"))
    await notifier.process_update(_update(6, 103, "/start"))
    assert set(store.list_chat_ids()) == {102, 103}
    assert replies[-2:] == [
        (101, "🔕 Ви відписалися від сповіщень."),
        (103, "✅ Ви підписалися на сповіщення."),
    ]

    new_user_logs = [record.getMessage() for record in caplog.records if "New user" in record.msg]
    removed_user_logs = [
        record.getMessage() for record in caplog.records if "Removed user" in record.msg
    ]
    assert len(new_user_logs) == 3
    assert "chat_id=101" in new_user_logs[0]
    assert "username=@user101" in new_user_logs[0]
    assert removed_user_logs == [
        "Removed user (chat_id=101, user_id=101, username=@user101, "
        "first_name=User 101, reason=/stop)"
    ]
    await notifier.close()


@pytest.mark.asyncio
async def test_non_commands_and_group_commands_are_ignored() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start", chat_type="group"))
    await notifier.process_update(_update(2, 101, "hello"))

    assert calls == 0
    assert store.list_chat_ids() == ()
    await notifier.close()


@pytest.mark.asyncio
async def test_commands_received_during_quiet_hours_are_discarded() -> None:
    replies: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        replies.append(payload["chat_id"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
        quiet_hours=QuietHoursConfig(enabled=True),
    )

    await notifier.process_update(
        _update(1, 101, "/start", sent_at=datetime(2026, 8, 22, 23, 0, tzinfo=UTC))
    )
    await notifier.process_update(
        _update(2, 102, "/start", sent_at=datetime(2026, 8, 23, 4, 0, tzinfo=UTC))
    )

    assert store.list_chat_ids() == (102,)
    assert replies == [102]
    await notifier.close()


@pytest.mark.asyncio
async def test_failed_command_reply_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"ok": False, "error_code": 500, "description": "temporary outage"},
        )

    store = SubscriberStore(":memory:")
    store.open()
    store.select_bot(999)
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        delivery_attempts=1,
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.process_update(_update(1, 101, "/start"))
    assert store.list_chat_ids() == (101,)
    await notifier.close()

    assert (
        "Could not send Bot API /start command reply to chat_id=101 after 1 attempt(s) "
        "(error_code=500, retryable=True, retry_after=None)" in caplog.text
    )
    assert (
        "Bot command reply was not delivered (command=/start, chat_id=101); "
        "no further automatic retry is scheduled" in caplog.text
    )
    assert "✅ Ви підписалися на сповіщення." not in caplog.text
    assert "123:secret" not in caplog.text


@pytest.mark.asyncio
async def test_poller_processes_command_and_persists_next_offset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    second_poll_started = asyncio.Event()
    keep_polling = asyncio.Event()
    get_updates_payloads: list[Mapping[str, object]] = []
    caplog.set_level(logging.INFO, logger="telegram_monitor.notifier")

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {}})
        assert method == "getUpdates"
        get_updates_payloads.append(payload)
        if len(get_updates_payloads) == 1:
            return httpx.Response(200, json={"ok": True, "result": [_update(42, 101, "/start")]})
        second_poll_started.set()
        await keep_polling.wait()
        return httpx.Response(200, json={"ok": True, "result": []})

    store = SubscriberStore(":memory:")
    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
        store=store,
    )

    await notifier.start()
    await asyncio.wait_for(second_poll_started.wait(), timeout=1)

    assert store.list_chat_ids() == (101,)
    assert store.get_next_update_offset() == 43
    assert "offset" not in get_updates_payloads[0]
    assert get_updates_payloads[1]["offset"] == 43
    assert (
        "Bot command polling started; Accepting /start and /stop "
        "(subscriber limit: 10); Current subscribers: 1" in caplog.text
    )
    assert "Bot subscribers:" not in caplog.text
    await notifier.close()


@pytest.mark.asyncio
async def test_active_webhook_prevents_long_polling() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999}})
        assert method == "getWebhookInfo"
        return httpx.Response(
            200,
            json={"ok": True, "result": {"url": "https://example.test/bot-hook"}},
        )

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ConfigurationError, match="active webhook"):
        await notifier.start()
    await notifier.close()

    assert methods == ["getMe", "getWebhookInfo"]


@pytest.mark.asyncio
async def test_competing_get_updates_consumer_fails_startup() -> None:
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", maxsplit=1)[-1]
        methods.append(method)
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        assert method == "getUpdates"
        return httpx.Response(
            409,
            json={
                "ok": False,
                "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request",
            },
        )

    notifier = TelegramBotNotifier(
        "123:secret",
        ":memory:",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ConfigurationError, match="polling stopped permanently"):
        await notifier.start()
    await notifier.close()

    assert methods == ["getMe", "getWebhookInfo", "getUpdates"]
