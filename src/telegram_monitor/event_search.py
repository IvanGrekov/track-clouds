from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from telethon import TelegramClient


class EventSearchAccessDenied(Exception):
    """The requester does not own the connected Telegram account."""


class EventSearchError(Exception):
    """The connected Telegram account cannot be identified."""


class EventSearch(Protocol):
    async def count_matches(self, requester_id: int, bot_username: str, location: str) -> int: ...


class TelegramEventSearch:
    def __init__(
        self,
        client: TelegramClient,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))

    async def count_matches(self, requester_id: int, bot_username: str, location: str) -> int:
        account = await self._client.get_me()
        if account is None or not isinstance(account.id, int) or isinstance(account.id, bool):
            raise EventSearchError("Could not identify the connected Telegram account")
        if account.id != requester_id:
            raise EventSearchAccessDenied

        end = self._now().astimezone(UTC)
        start = end - timedelta(days=10)
        count = 0
        async for message in self._client.iter_messages(
            bot_username,
            limit=None,
            search=location,
        ):
            message_date = message.date
            if not isinstance(message_date, datetime) or message_date.utcoffset() is None:
                raise EventSearchError("A search result has no valid timestamp")
            if message_date < start:
                break
            if message_date <= end and message.sender_id not in (None, requester_id):
                count += 1
        return count
