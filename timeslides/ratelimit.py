"""A token bucket, because the original script had no rate limiting at all.

One report run fans out to (objects x state providers x data modes) state-vector
calls plus one element-set call per object. Seven objects across three providers
in one mode is 28 requests issued as fast as the network allows. That was
tolerable as a hand-run script and is not tolerable as a service where several
people can press the button at once.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Blocking token bucket. `take()` returns once capacity is available.

    Deliberately blocking rather than raising: the caller is a background render
    job, and the right response to being over budget is to go slower, not to
    fail a job that is already half done.
    """

    def __init__(self, per_minute: int, capacity: int | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.rate = per_minute / 60.0
        self.capacity = float(capacity if capacity is not None else per_minute)
        self._tokens = self.capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated = now

    def take(self, tokens: float = 1.0) -> float:
        """Consume `tokens`, waiting if necessary. Returns seconds waited."""
        if tokens > self.capacity:
            raise ValueError(f"cannot take {tokens} from a bucket of {self.capacity}")
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                shortfall = tokens - self._tokens
                delay = shortfall / self.rate
            self._sleep(delay)
            waited += delay

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
