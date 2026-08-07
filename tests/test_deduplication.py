from telegram_monitor.deduplication import RecentMessageCache


def test_claim_commit_release_and_capacity() -> None:
    cache = RecentMessageCache(capacity=2)

    assert cache.claim((1, 1)) is True
    assert cache.claim((1, 1)) is False
    cache.release((1, 1))
    assert cache.claim((1, 1)) is True
    cache.commit((1, 1))
    assert cache.claim((1, 1)) is False

    assert cache.claim((1, 2)) is True
    cache.commit((1, 2))
    assert cache.claim((1, 3)) is True
    cache.commit((1, 3))
    assert cache.claim((1, 1)) is True
