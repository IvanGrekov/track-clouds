from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from telegram_monitor.subscriber_store import Subscriber, SubscriberStore, SubscriptionResult


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
