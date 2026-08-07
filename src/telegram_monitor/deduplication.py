from __future__ import annotations

from collections import OrderedDict

MessageKey = tuple[int, int]


class RecentMessageCache:
    """Bounded in-memory duplicate guard for concurrently delivered updates."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[MessageKey, None] = OrderedDict()
        self._in_flight: set[MessageKey] = set()

    def claim(self, key: MessageKey) -> bool:
        if key in self._seen or key in self._in_flight:
            return False
        self._in_flight.add(key)
        return True

    def commit(self, key: MessageKey) -> None:
        self._in_flight.discard(key)
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def release(self, key: MessageKey) -> None:
        self._in_flight.discard(key)
