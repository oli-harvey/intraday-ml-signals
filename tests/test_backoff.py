"""ExponentialBackoff schedule."""

from signals.data.backoff import ExponentialBackoff


def test_deterministic_schedule_with_cap() -> None:
    b = ExponentialBackoff(initial=1.0, factor=2.0, cap=8.0, jitter=0.0)
    assert [b.next() for _ in range(6)] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


def test_reset_restarts_schedule() -> None:
    b = ExponentialBackoff(initial=1.0, factor=2.0, cap=8.0, jitter=0.0)
    b.next()
    b.next()
    b.reset()
    assert b.next() == 1.0


def test_jitter_stays_within_cap() -> None:
    b = ExponentialBackoff(initial=4.0, factor=2.0, cap=8.0, jitter=0.5)
    for _ in range(50):
        assert 0 < b.next() <= 8.0
