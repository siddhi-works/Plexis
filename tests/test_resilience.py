"""Chaos tests against real partner MCP servers: retries, circuit breaking, stale-cache degradation."""

from __future__ import annotations

import httpx
import pytest

from qed.partners.client import McpPartners, PartnerError
from qed.resilience import CircuitBreaker, TTLCache
from tests.conftest import TRIP_DAY

pytestmark = pytest.mark.chaos


def chaos(partners_url: str, **body: object) -> None:
    httpx.post(f"{partners_url}/admin/chaos", json=body).raise_for_status()


@pytest.mark.asyncio
async def test_stale_cache_keeps_planning_alive_when_a_partner_dies(live_stack) -> None:  # type: ignore[no-untyped-def]
    url = live_stack["partners"]
    hub = McpPartners(url, timeout_s=1.0, cache_ttl_s=0.0)
    try:
        fresh, f1 = await hub.restaurants("SFO")
        assert f1.freshness == "fresh"
        chaos(url, partner="dining", error_rate=1.0)
        stale, f2 = await hub.restaurants("SFO")
        assert f2.freshness == "stale" and [r.id for r in stale] == [r.id for r in fresh]
        with pytest.raises(PartnerError):
            await hub.restaurants("MIA")  # nothing cached for Miami, so there is nothing to degrade to
    finally:
        chaos(url, reset=True)
        await hub.close()


@pytest.mark.asyncio
async def test_breaker_opens_then_recovers(live_stack) -> None:  # type: ignore[no-untyped-def]
    url = live_stack["partners"]
    hub = McpPartners(url, timeout_s=1.0, cache_ttl_s=0.0)
    hub.breakers["weather"] = CircuitBreaker("weather", failure_threshold=2, reset_after_s=0.2)
    try:
        chaos(url, partner="weather", error_rate=1.0)
        for _ in range(2):
            with pytest.raises(PartnerError):
                await hub.forecast("SEA", TRIP_DAY, 2)
        assert hub.breakers["weather"].state == "open"
        chaos(url, reset=True)
        import asyncio

        await asyncio.sleep(0.25)
        days, _f = await hub.forecast("SEA", TRIP_DAY, 2)
        assert len(days) == 2 and hub.breakers["weather"].state == "closed"
    finally:
        chaos(url, reset=True)
        await hub.close()


@pytest.mark.asyncio
async def test_slow_partner_times_out_instead_of_hanging(live_stack) -> None:  # type: ignore[no-untyped-def]
    url = live_stack["partners"]
    hub = McpPartners(url, timeout_s=0.3, cache_ttl_s=0.0)
    try:
        chaos(url, partner="experiences", latency_ms=900)
        with pytest.raises(PartnerError):
            await hub.attractions("BOS")
    finally:
        chaos(url, reset=True)
        await hub.close()


def test_ttl_cache_serves_stale_only_when_asked() -> None:
    c: TTLCache[int] = TTLCache(ttl_s=0.0)
    c.put("k", 1)
    assert c.get("k") == (None, False)
    assert c.get("k", allow_stale=True) == (1, True)
