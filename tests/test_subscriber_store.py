from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

import telegram_monitor.subscriber_store as subscriber_store
from telegram_monitor.subscriber_store import (
    Subscriber,
    SubscriberStore,
    SubscriptionResult,
    event_reply_fingerprint,
)


def _subscriber(chat_id: int) -> Subscriber:
    return Subscriber(
        chat_id=chat_id,
        user_id=chat_id,
        username=f"user{chat_id}",
        first_name=f"User {chat_id}",
    )


def test_store_keeps_only_ten_and_stop_frees_a_slot(tmp_path: Path) -> None:
    database = str(tmp_path / "subscribers.sqlite3")
    store = SubscriberStore(database, subscriber_limit=10)
    store.open()
    store.select_bot(100)

    for chat_id in range(1, 11):
        assert store.subscribe(_subscriber(chat_id)) is SubscriptionResult.ADDED

    assert store.subscribe(_subscriber(11)) is SubscriptionResult.LIMIT_REACHED
    assert store.subscribe(_subscriber(1)) is SubscriptionResult.ALREADY_SUBSCRIBED
    assert store.list_chat_ids() == tuple(range(1, 11))

    assert store.unsubscribe(5) is True
    assert store.unsubscribe(5) is False
    assert store.subscribe(_subscriber(11)) is SubscriptionResult.ADDED
    assert set(store.list_chat_ids()) == {*range(1, 5), *range(6, 12)}
    store.close()


def test_store_persists_and_isolates_each_bot(tmp_path: Path) -> None:
    database = str(tmp_path / "subscribers.sqlite3")
    first = SubscriberStore(database)
    first.open()
    first.select_bot(100)
    first.subscribe(_subscriber(1))
    first.set_next_update_offset(50)
    first.set_next_update_offset(40)
    first.select_bot(200)
    first.subscribe(_subscriber(2))
    first.set_next_update_offset(75)
    first.close()

    reopened = SubscriberStore(database)
    reopened.open()
    reopened.select_bot(100)
    assert reopened.list_chat_ids() == (1,)
    assert reopened.get_next_update_offset() == 50
    reopened.select_bot(200)
    assert reopened.list_chat_ids() == (2,)
    assert reopened.get_next_update_offset() == 75
    reopened.close()


def test_event_reply_fingerprints_are_scoped_persistent_and_expire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return now

    monkeypatch.setattr(subscriber_store, "datetime", FrozenDatetime)
    database = str(tmp_path / "subscribers.sqlite3")
    store = SubscriberStore(database)
    store.open()
    store.select_bot(100)
    store.record_event_reply(1, "first")
    store.record_event_reply(2, "other chat")
    store.select_bot(200)
    store.record_event_reply(1, "other bot")
    store.close()

    store = SubscriberStore(database)
    store.open()
    store.select_bot(100)
    assert store.event_reply_fingerprints(1) == frozenset({event_reply_fingerprint("first")})
    assert store.event_reply_fingerprints(2) == frozenset({event_reply_fingerprint("other chat")})
    store.select_bot(200)
    assert store.event_reply_fingerprints(1) == frozenset({event_reply_fingerprint("other bot")})
    now += timedelta(days=11, seconds=1)
    assert store.event_reply_fingerprints(1) == frozenset()
    store.close()


def test_two_connections_cannot_claim_the_last_slot(tmp_path: Path) -> None:
    database = str(tmp_path / "subscribers.sqlite3")
    initializer = SubscriberStore(database, subscriber_limit=1)
    initializer.open()
    initializer.close()
    barrier = Barrier(2, timeout=5)

    def subscribe(chat_id: int) -> SubscriptionResult:
        store = SubscriberStore(database, subscriber_limit=1)
        try:
            store.open()
            store.select_bot(100)
            barrier.wait()
            return store.subscribe(_subscriber(chat_id))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(subscribe, (1, 2)))

    assert sorted(result.value for result in results) == ["added", "limit_reached"]
