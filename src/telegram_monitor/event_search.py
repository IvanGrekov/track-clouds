from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from telethon import TelegramClient

from .formatting import LEGACY_EVENT_REPLIES
from .subscriber_store import event_reply_fingerprint


@dataclass(frozen=True, slots=True)
class EventSearchResult:
    count: int
    first_date: datetime
    last_date: datetime
    last_message_json: str = field(repr=False)


class EventSearchAccessDenied(Exception):
    """The requester does not own the connected Telegram account."""


class EventSearchError(Exception):
    """The connected Telegram account cannot be identified."""


class EventSearch(Protocol):
    async def find_matches(
        self,
        requester_id: int,
        bot_username: str,
        location: str,
        excluded_replies: frozenset[str],
    ) -> EventSearchResult | None: ...


class TelegramEventSearch:
    def __init__(
        self,
        client: TelegramClient,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))

    async def find_matches(
        self,
        requester_id: int,
        bot_username: str,
        location: str,
        excluded_replies: frozenset[str],
    ) -> EventSearchResult | None:
        account = await self._client.get_me()
        if account is None or not isinstance(account.id, int) or isinstance(account.id, bool):
            raise EventSearchError("Could not identify the connected Telegram account")
        if account.id != requester_id:
            raise EventSearchAccessDenied

        end = self._now().astimezone(UTC)
        start = end - timedelta(days=10)
        count = 0
        first_date: datetime | None = None
        last_date: datetime | None = None
        last_message_json: str | None = None
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
            if message_date > end or message.sender_id in (None, requester_id):
                continue
            if message.message:
                if message.message in LEGACY_EVENT_REPLIES:
                    continue
                if event_reply_fingerprint(message.message) in excluded_replies:
                    continue
            count += 1
            first_date = message_date
            if last_date is None:
                last_date = message_date
                last_message_json = message.to_json(indent=2, ensure_ascii=False)
        if count == 0:
            return None
        if first_date is None or last_date is None or last_message_json is None:
            raise EventSearchError("Search returned an incomplete result")
        return EventSearchResult(count, first_date, last_date, last_message_json)
