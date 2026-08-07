from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import RLock


class SubscriptionResult(Enum):
    ADDED = "added"
    ALREADY_SUBSCRIBED = "already_subscribed"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class Subscriber:
    chat_id: int
    user_id: int | None
    username: str | None
    first_name: str | None


class SubscriberStore:
    """Small persistent registry for Bot API subscribers and polling state."""

    def __init__(self, database_path: str, subscriber_limit: int = 10) -> None:
        self._database_path = (
            database_path if database_path == ":memory:" else str(Path(database_path).expanduser())
        )
        self._subscriber_limit = subscriber_limit
        self._connection: sqlite3.Connection | None = None
        self._bot_id: int | None = None
        self._lock = RLock()

    @property
    def subscriber_limit(self) -> int:
        return self._subscriber_limit

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return

            if self._database_path != ":memory:":
                Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
            # Runtime calls are offloaded with asyncio.to_thread(). The connection may
            # therefore be used by different worker threads, while _lock guarantees that
            # SQLite still sees only one operation at a time for this store instance.
            connection = sqlite3.connect(
                self._database_path,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 5000")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_subscribers (
                        bot_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER,
                        username TEXT,
                        first_name TEXT,
                        subscribed_at TEXT NOT NULL,
                        PRIMARY KEY (bot_id, chat_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_state (
                        bot_id INTEGER NOT NULL,
                        key TEXT NOT NULL,
                        value INTEGER NOT NULL,
                        PRIMARY KEY (bot_id, key)
                    )
                    """
                )
            except BaseException:
                connection.close()
                raise
            self._connection = connection

    def select_bot(self, bot_id: int) -> None:
        with self._lock:
            self._bot_id = bot_id

    def subscribe(self, subscriber: Subscriber) -> SubscriptionResult:
        with self._lock:
            connection = self._require_connection()
            bot_id = self._require_bot_id()
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT 1 FROM bot_subscribers WHERE bot_id = ? AND chat_id = ?",
                    (bot_id, subscriber.chat_id),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE bot_subscribers
                        SET user_id = ?, username = ?, first_name = ?
                        WHERE bot_id = ? AND chat_id = ?
                        """,
                        (
                            subscriber.user_id,
                            subscriber.username,
                            subscriber.first_name,
                            bot_id,
                            subscriber.chat_id,
                        ),
                    )
                    connection.execute("COMMIT")
                    return SubscriptionResult.ALREADY_SUBSCRIBED

                count = connection.execute(
                    "SELECT COUNT(*) FROM bot_subscribers WHERE bot_id = ?", (bot_id,)
                ).fetchone()[0]
                if count >= self._subscriber_limit:
                    connection.execute("COMMIT")
                    return SubscriptionResult.LIMIT_REACHED

                connection.execute(
                    """
                    INSERT INTO bot_subscribers (
                        bot_id, chat_id, user_id, username, first_name, subscribed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bot_id,
                        subscriber.chat_id,
                        subscriber.user_id,
                        subscriber.username,
                        subscriber.first_name,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.execute("COMMIT")
                return SubscriptionResult.ADDED
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def unsubscribe(self, chat_id: int) -> bool:
        return self.remove_subscriber(chat_id) is not None

    def remove_subscriber(self, chat_id: int) -> Subscriber | None:
        with self._lock:
            bot_id = self._require_bot_id()
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT chat_id, user_id, username, first_name
                    FROM bot_subscribers
                    WHERE bot_id = ? AND chat_id = ?
                    """,
                    (bot_id, chat_id),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                connection.execute(
                    "DELETE FROM bot_subscribers WHERE bot_id = ? AND chat_id = ?",
                    (bot_id, chat_id),
                )
                connection.execute("COMMIT")
                return _subscriber_from_row(row)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def list_chat_ids(self) -> tuple[int, ...]:
        with self._lock:
            bot_id = self._require_bot_id()
            rows = self._require_connection().execute(
                """
                SELECT chat_id FROM bot_subscribers
                WHERE bot_id = ?
                ORDER BY subscribed_at, chat_id
                """,
                (bot_id,),
            )
            return tuple(int(row["chat_id"]) for row in rows)

    def get_next_update_offset(self) -> int | None:
        with self._lock:
            bot_id = self._require_bot_id()
            row = (
                self._require_connection()
                .execute(
                    """
                SELECT value FROM bot_state
                WHERE bot_id = ? AND key = 'next_update_offset'
                """,
                    (bot_id,),
                )
                .fetchone()
            )
            return int(row["value"]) if row is not None else None

    def set_next_update_offset(self, offset: int) -> None:
        with self._lock:
            bot_id = self._require_bot_id()
            self._require_connection().execute(
                """
                INSERT INTO bot_state (bot_id, key, value)
                VALUES (?, 'next_update_offset', ?)
                ON CONFLICT(bot_id, key) DO UPDATE
                SET value = MAX(bot_state.value, excluded.value)
                """,
                (bot_id, offset),
            )

    def close(self) -> None:
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Subscriber store is not open")
        return self._connection

    def _require_bot_id(self) -> int:
        if self._bot_id is None:
            raise RuntimeError("Subscriber store has no selected Telegram bot")
        return self._bot_id


def _subscriber_from_row(row: sqlite3.Row) -> Subscriber:
    return Subscriber(
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]) if row["user_id"] is not None else None,
        username=str(row["username"]) if row["username"] is not None else None,
        first_name=str(row["first_name"]) if row["first_name"] is not None else None,
    )
