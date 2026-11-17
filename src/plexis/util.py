"""Small, dependency-free helpers shared by every layer.

Canonical JSON matters here: certificates are hashed and signed, so two
processes must serialise the same certificate to the same bytes.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any

import orjson

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, dates as ISO strings."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS, default=_default)


def _default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """Stable content hash of any JSON-able value."""
    return sha256_hex(canonical_json(value))


def stable_seed(*parts: object) -> int:
    """A seed that is stable across processes (unlike hash())."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fmt_minutes(minutes: int) -> str:
    """540 -> '9:00 am'; values past midnight are shown with a '(+1)' day marker."""
    day, rem = divmod(int(minutes), 1440)
    h, m = divmod(rem, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    text = f"{h12}:{m:02d} {suffix}"
    return f"{text} (+{day})" if day else text


def fmt_money(amount: float) -> str:
    return f"${amount:,.0f}"


def daterange(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


class Stopwatch:
    """Monotonic stage timer used for the latency waterfall."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.stages: list[dict[str, Any]] = []

    @contextmanager
    def stage(self, name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
        start = time.perf_counter()
        record: dict[str, Any] = {"name": name, "startMs": round((start - self.t0) * 1000, 2), **attrs}
        try:
            yield record
        finally:
            record["durationMs"] = round((time.perf_counter() - start) * 1000, 2)
            self.stages.append(record)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 2)
