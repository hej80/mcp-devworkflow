"""
BEST PRACTICE #2 -- RATE LIMITING (token bucket).

Why an MCP server needs this
----------------------------
An MCP tool is called by a model in a loop. Three realistic failure modes:
  * a retry loop ("that didn't work, try again") calling a write tool 50x;
  * an attacker-authored instruction inside fetched text telling the model to
    call a tool repeatedly (prompt injection -> tool-call amplification);
  * a plain bug in an agent.
Any of those can burn the GitHub API quota (5,000 req/h for a PAT), spam a
repository with issues, or fork-bomb the host with `git` subprocesses.

Algorithm: TOKEN BUCKET.
  * The bucket holds at most `capacity` tokens and refills continuously at
    `capacity / window_seconds` tokens per second.
  * Each call spends one token; a call with no token available is refused and
    told how long to wait.
  * A bucket allows a short burst (good UX: a few quick calls feel responsive)
    while bounding the long-run average rate -- which a naive "N calls per
    fixed minute" counter does not, since it permits 2N calls across a window
    boundary.

Implementation notes:
  * `time.monotonic()` is used, not `time.time()`: a clock adjustment (NTP, DST)
    must not hand out free tokens or lock the bucket forever.
  * A `threading.Lock` guards the state. The MCP stdio server is async and may
    run handlers in a worker thread, so check-then-act must be atomic.
  * Limits are per tool name (separate budgets for reads and writes) and are
    enforced BEFORE any network or subprocess work happens.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .errors import RateLimitError


@dataclass
class BucketState:
    """Snapshot of a bucket, returned for observability/tests."""

    tokens: float
    capacity: int
    refill_per_second: float


class TokenBucket:
    """A thread-safe token bucket.

    The classic token-bucket algorithm (Tanenbaum, *Computer Networks*); this
    continuous-refill formulation follows the widely used pattern described at
    https://en.wikipedia.org/wiki/Token_bucket . Implemented from the algorithm
    description, not copied from a specific codebase.
    """

    def __init__(self, capacity: int, window_seconds: float, name: str = "bucket") -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.name = name
        self.capacity = int(capacity)
        self.window_seconds = float(window_seconds)
        self.refill_per_second = self.capacity / self.window_seconds
        self._tokens = float(self.capacity)   # start full
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    # -- internal ---------------------------------------------------------
    def _refill_locked(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._updated = now

    # -- public -----------------------------------------------------------
    def try_consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        """Attempt to spend `tokens`.

        Returns (allowed, retry_after_seconds). `retry_after_seconds` is 0.0
        when allowed, otherwise the exact wait until enough tokens exist.
        """
        with self._lock:
            now = time.monotonic()
            self._refill_locked(now)
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, 0.0
            deficit = tokens - self._tokens
            return False, deficit / self.refill_per_second

    def consume_or_raise(self, tokens: float = 1.0) -> None:
        """Spend a token or raise `RateLimitError` with a precise retry hint."""
        allowed, retry_after = self.try_consume(tokens)
        if not allowed:
            raise RateLimitError(
                f"Rate limit reached for '{self.name}': at most {self.capacity} "
                f"call(s) per {self.window_seconds:.0f}s. The request was refused "
                f"before doing any work. Retry in {retry_after:.1f}s.",
                retry_after_seconds=retry_after,
            )

    def peek(self) -> BucketState:
        """Read current state without spending a token (for status tooling)."""
        with self._lock:
            self._refill_locked(time.monotonic())
            return BucketState(
                tokens=round(self._tokens, 3),
                capacity=self.capacity,
                refill_per_second=round(self.refill_per_second, 4),
            )

    def reset(self) -> None:
        """Refill to capacity. Used by the test-suite, not by the server."""
        with self._lock:
            self._tokens = float(self.capacity)
            self._updated = time.monotonic()


class RateLimiterRegistry:
    """Named buckets, created once at start-up and shared by all tool calls."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def register(self, name: str, capacity: int, window_seconds: float) -> TokenBucket:
        with self._lock:
            bucket = TokenBucket(capacity, window_seconds, name=name)
            self._buckets[name] = bucket
            return bucket

    def get(self, name: str) -> TokenBucket:
        bucket = self._buckets.get(name)
        if bucket is None:
            raise KeyError(f"no rate-limit bucket registered for '{name}'")
        return bucket

    def snapshot(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for name, bucket in self._buckets.items():
            state = bucket.peek()
            out[name] = {
                "tokens_available": state.tokens,
                "capacity": state.capacity,
                "refill_per_second": state.refill_per_second,
            }
        return out
