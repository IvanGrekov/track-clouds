from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession

from telegram_monitor.event_search import (
    EventSearchAccessDenied,
    EventSearchError,
    TelegramEventSearch,
)
from telegram_monitor.formatting import EVENTS_USAGE_REPLY
from telegram_monitor.subscriber_store import event_reply_fingerprint


def _message(date: datetime, sender_id: int | None, text: str = "галицьке") -> SimpleNamespace:
    return SimpleNamespace(
        date=date,
        sender_id=sender_id,
        message=text,
        to_json=lambda **kwargs: json.dumps(
            {"date": date.isoformat(), "sender_id": sender_id, "message": text}, **kwargs
        ),
    )


class FakeClient:
    def __init__(self, account_id: object, messages: list[SimpleNamespace]) -> None:
        self.account_id = account_id
        self.messages = messages
        self.searches: list[tuple[str, int | None, str]] = []
        self.yielded = 0

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.account_id)

    async def iter_messages(
        self, bot_username: str, *, limit: int | None, search: str
    ) -> AsyncIterator[SimpleNamespace]:
        self.searches.append((bot_username, limit, search))
        for message in self.messages:
            self.yielded += 1
            yield message


@pytest.mark.asyncio
async def test_search_counts_all_server_matches_in_last_ten_days() -> None:
    now = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    start = now - timedelta(days=10)
    messages = [
        _message(now + timedelta(seconds=1), 999),
        _message(now, 999, "newest галицьке"),
        _message(now, 101),
        _message(now, None),
        *[_message(now - timedelta(days=1), 999) for _ in range(120)],
        _message(start, 999),
        _message(start - timedelta(seconds=1), 999),
        _message(start - timedelta(days=1), 999),
    ]
    client = FakeClient(101, messages)
    search = TelegramEventSearch(client, now=lambda: now)

    result = await search.find_matches(101, "ExampleBot", "галицьке", frozenset())

    assert result is not None
    assert result.count == 122
    assert result.first_date == start
    assert result.last_date == now
    assert json.loads(result.last_message_json) == {
        "date": now.isoformat(),
        "sender_id": 999,
        "message": "newest галицьке",
    }
    assert client.searches == [("ExampleBot", None, "галицьке")]
    assert client.yielded == len(messages) - 1


@pytest.mark.asyncio
async def test_search_returns_zero_for_no_matches() -> None:
    client = FakeClient(101, [])
    search = TelegramEventSearch(client)

    assert await search.find_matches(101, "ExampleBot", "галицьке", frozenset()) is None


@pytest.mark.asyncio
async def test_search_excludes_recorded_replies_before_choosing_dates_and_last_message() -> None:
    now = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    reply = "результат для галицьке"
    client = FakeClient(
        101,
        [_message(now, 999, reply), _message(now - timedelta(hours=1), 999, "галицьке alert")],
    )
    result = await TelegramEventSearch(client, now=lambda: now).find_matches(
        101, "ExampleBot", "галицьке", frozenset({event_reply_fingerprint(reply)})
    )
    assert result is not None
    assert result.count == 1
    assert result.first_date == result.last_date == now - timedelta(hours=1)
    assert json.loads(result.last_message_json)["message"] == "галицьке alert"


@pytest.mark.asyncio
async def test_search_excludes_legacy_event_replies_without_a_recorded_fingerprint() -> None:
    now = datetime.now(UTC)
    client = FakeClient(101, [_message(now, 999, EVENTS_USAGE_REPLY)])
    result = await TelegramEventSearch(client, now=lambda: now).find_matches(
        101, "ExampleBot", "галицьке", frozenset()
    )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("date", [None, datetime(2026, 11, 3)])
async def test_search_rejects_results_without_a_valid_timestamp(date: object) -> None:
    client = FakeClient(101, [SimpleNamespace(date=date, sender_id=999)])

    with pytest.raises(EventSearchError, match="timestamp"):
        await TelegramEventSearch(client).find_matches(101, "ExampleBot", "галицьке", frozenset())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_id", "error"),
    [(102, EventSearchAccessDenied), (None, EventSearchError), (True, EventSearchError)],
)
async def test_search_requires_the_connected_account(
    account_id: object, error: type[Exception]
) -> None:
    client = FakeClient(account_id, [])
    search = TelegramEventSearch(client)

    with pytest.raises(error):
        await search.find_matches(101, "ExampleBot", "галицьке", frozenset())
    assert client.searches == []


@pytest.mark.asyncio
async def test_search_propagates_error_instead_of_returning_partial_count() -> None:
    class FailingClient(FakeClient):
        async def iter_messages(
            self, bot_username: str, *, limit: int | None, search: str
        ) -> AsyncIterator[SimpleNamespace]:
            yield _message(datetime.now(UTC), 999)
            raise OSError("history unavailable")

    search = TelegramEventSearch(FailingClient(101, []))

    with pytest.raises(OSError, match="history unavailable"):
        await search.find_matches(101, "ExampleBot", "галицьке", frozenset())


@pytest.mark.asyncio
async def test_real_telethon_iterator_uses_server_search_and_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    client = TelegramClient(StringSession(), 1, "test")
    requests: list[tuple[str, int, int]] = []
    messages = [
        types.Message(
            id=400 - index,
            peer_id=types.PeerUser(999),
            from_id=types.PeerUser(999 if index % 2 == 0 else 101),
            date=now - timedelta(days=1),
            message="галицьке",
        )
        for index in range(150)
    ]

    async def get_me() -> SimpleNamespace:
        return SimpleNamespace(id=101)

    async def get_input_entity(entity: str) -> types.InputPeerUser:
        assert entity == "ExampleBot"
        return types.InputPeerUser(user_id=999, access_hash=1)

    async def call(sender: object, request: object, **kwargs: object) -> SimpleNamespace:
        assert isinstance(request, functions.messages.SearchRequest)
        requests.append((request.q, request.offset_id, request.limit))
        page = [
            message
            for message in messages
            if not request.offset_id or message.id < request.offset_id
        ]
        return SimpleNamespace(
            count=len(messages), messages=page[: request.limit], users=[], chats=[]
        )

    monkeypatch.setattr(client, "get_me", get_me)
    monkeypatch.setattr(client, "get_input_entity", get_input_entity)
    monkeypatch.setattr(client, "_call", call)

    result = await TelegramEventSearch(client, now=lambda: now).find_matches(
        101, "ExampleBot", "галицьке", frozenset()
    )

    assert result is not None
    assert result.count == 75
    assert result.last_message_json == messages[0].to_json(indent=2, ensure_ascii=False)
    assert json.loads(result.last_message_json)["id"] == 400
    assert requests == [("галицьке", 0, 100), ("галицьке", 301, 100), ("галицьке", 251, 100)]
