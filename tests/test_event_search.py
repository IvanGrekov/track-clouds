from __future__ import annotations

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
        SimpleNamespace(date=now + timedelta(seconds=1), sender_id=999),
        SimpleNamespace(date=now, sender_id=999),
        SimpleNamespace(date=now, sender_id=101),
        SimpleNamespace(date=now, sender_id=None),
        *[SimpleNamespace(date=now - timedelta(days=1), sender_id=999) for _ in range(120)],
        SimpleNamespace(date=start, sender_id=999),
        SimpleNamespace(date=start - timedelta(seconds=1), sender_id=999),
        SimpleNamespace(date=start - timedelta(days=1), sender_id=999),
    ]
    client = FakeClient(101, messages)
    search = TelegramEventSearch(client, now=lambda: now)

    count = await search.count_matches(101, "ExampleBot", "галицьке")

    assert count == 122
    assert client.searches == [("ExampleBot", None, "галицьке")]
    assert client.yielded == len(messages) - 1


@pytest.mark.asyncio
async def test_search_returns_zero_for_no_matches() -> None:
    client = FakeClient(101, [])
    search = TelegramEventSearch(client)

    assert await search.count_matches(101, "ExampleBot", "галицьке") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("date", [None, datetime(2026, 11, 3)])
async def test_search_rejects_results_without_a_valid_timestamp(date: object) -> None:
    client = FakeClient(101, [SimpleNamespace(date=date, sender_id=999)])

    with pytest.raises(EventSearchError, match="timestamp"):
        await TelegramEventSearch(client).count_matches(101, "ExampleBot", "галицьке")


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
        await search.count_matches(101, "ExampleBot", "галицьке")
    assert client.searches == []


@pytest.mark.asyncio
async def test_search_propagates_error_instead_of_returning_partial_count() -> None:
    class FailingClient(FakeClient):
        async def iter_messages(
            self, bot_username: str, *, limit: int | None, search: str
        ) -> AsyncIterator[SimpleNamespace]:
            yield SimpleNamespace(date=datetime.now(UTC), sender_id=999)
            raise OSError("history unavailable")

    search = TelegramEventSearch(FailingClient(101, []))

    with pytest.raises(OSError, match="history unavailable"):
        await search.count_matches(101, "ExampleBot", "галицьке")


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

    count = await TelegramEventSearch(client, now=lambda: now).count_matches(
        101, "ExampleBot", "галицьке"
    )

    assert count == 75
    assert requests == [("галицьке", 0, 100), ("галицьке", 301, 100), ("галицьке", 251, 100)]
