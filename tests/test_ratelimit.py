"""The token bucket. A fake clock, so no test ever actually sleeps."""

from __future__ import annotations

import threading

import pytest

from timeslides.ratelimit import TokenBucket


@pytest.fixture
def clockwork():
    now = [0.0]
    slept = []

    def sleep(delay):
        slept.append(delay)
        now[0] += delay

    def advance(seconds):
        now[0] += seconds

    return dict(now=now, slept=slept, sleep=sleep, advance=advance,
                clock=lambda: now[0])


def _bucket(clockwork, per_minute=60, capacity=None):
    return TokenBucket(per_minute, capacity=capacity,
                       clock=clockwork["clock"], sleep=clockwork["sleep"])


def test_a_full_bucket_admits_its_capacity_without_waiting(clockwork):
    b = _bucket(clockwork, 60)
    for _ in range(60):
        assert b.take() == 0.0
    assert clockwork["slept"] == []


def test_the_next_request_waits_exactly_one_interval(clockwork):
    b = _bucket(clockwork, 60)
    for _ in range(60):
        b.take()
    assert b.take() == pytest.approx(1.0)


def test_tokens_refill_over_time(clockwork):
    b = _bucket(clockwork, 60)
    for _ in range(60):
        b.take()
    assert b.tokens == pytest.approx(0.0)
    clockwork["advance"](30)
    assert b.tokens == pytest.approx(30.0)


def test_refill_never_exceeds_capacity(clockwork):
    b = _bucket(clockwork, 60)
    b.take(10)
    clockwork["advance"](3600)
    assert b.tokens == pytest.approx(60.0)


def test_capacity_can_be_set_below_the_rate(clockwork):
    """A small bucket with a high rate smooths bursts."""
    b = _bucket(clockwork, 600, capacity=5)
    for _ in range(5):
        assert b.take() == 0.0
    assert b.take() > 0.0


def test_taking_more_than_capacity_is_a_programming_error(clockwork):
    b = _bucket(clockwork, 60, capacity=10)
    with pytest.raises(ValueError, match="cannot take"):
        b.take(11)


def test_a_non_positive_rate_is_rejected():
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            TokenBucket(bad)


def test_concurrent_takers_never_exceed_the_budget():
    """The bucket is shared across render worker threads, so the lock matters."""
    b = TokenBucket(200, capacity=200, sleep=lambda d: None)
    taken = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(25):
            b.take()
            taken.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(taken) == 200
    assert b.tokens < 1.0
