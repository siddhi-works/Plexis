"""Fault-tolerance primitives for calls to partner systems.

- CircuitBreaker: stop hammering a failing partner, probe again after a cool-down.
- retry(): exponential backoff with full jitter, only for idempotent reads.
- TTLCache: fresh hits within the TTL; *stale* hits are still returned (and
  flagged) when the partner is down, so a planning request degrades instead of
  failing. Every degraded answer is labelled in the certificate.
- Bulkhead: cap concurrent calls per partner so one slow system can't starve others.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class BreakerOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 4
    reset_after_s: float = 8.0
    state: str = "closed"  # closed | open | half_open
    failures: int = 0
    opened_at: float = 0.0
    transitions: list[tuple[float, str]] = field(default_factory=list)

    def _to(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.transitions.append((time.time(), state))
            self.transitions[:] = self.transitions[-20:]

    def allow(self) -> bool:
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.reset_after_s:
                self._to("half_open")
                return True
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self._to("closed")

    def failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.opened_at = time.monotonic()
            self._to("open")


async def retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_s: float = 0.08,
    cap_s: float = 0.6,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except retry_on as exc:
            last = exc
            if i == attempts - 1:
                break
            await asyncio.sleep(random.uniform(0, min(cap_s, base_s * 2**i)))  # noqa: S311 - jitter, not crypto
    assert last is not None
    raise last


class TTLCache(Generic[T]):
    def __init__(self, ttl_s: float, max_items: int = 2048) -> None:
        self.ttl = ttl_s
        self.max = max_items
        self._d: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self.hits = self.misses = self.stale_hits = 0

    def get(self, key: str, *, allow_stale: bool = False) -> tuple[T | None, bool]:
        """Returns (value, is_stale)."""
        item = self._d.get(key)
        if item is None:
            self.misses += 1
            return None, False
        stored, value = item
        self._d.move_to_end(key)
        if time.monotonic() - stored <= self.ttl:
            self.hits += 1
            return value, False
        if allow_stale:
            self.stale_hits += 1
            return value, True
        self.misses += 1
        return None, False

    def put(self, key: str, value: T) -> None:
        self._d[key] = (time.monotonic(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)

    def stats(self) -> dict[str, Any]:
        return {"size": len(self._d), "hits": self.hits, "misses": self.misses, "staleHits": self.stale_hits}


class Bulkhead:
    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit)

    async def run(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._sem:
            return await fn()
