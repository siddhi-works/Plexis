"""Plexis's client side of the partner ecosystem.

`McpPartners` talks to partner MCP servers over Streamable HTTP. Each partner
gets one persistent `ClientSession`, owned by a dedicated background task (so
anyio cancel scopes open and close in the same task), wrapped in a circuit
breaker, a bulkhead, timeouts, jittered retries for reads, and a TTL cache
that can serve *stale* data when a partner is down. Every response carries
provenance (fresh / cached / stale) into the certificate.

`LocalPartners` implements the same interface in-process for tests and
benchmarks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from qed.domain import world
from qed.domain.models import (
    Attraction,
    Restaurant,
    StayQuote,
    TransportOption,
    TransportStatus,
    WeatherDay,
)
from qed.resilience import BreakerOpen, Bulkhead, CircuitBreaker, TTLCache, retry
from qed.util import digest

log = logging.getLogger("qed.partners")
PARTNERS = ("transport", "stays", "dining", "experiences", "weather", "wallet", "frontdesk", "guide")


class PartnerError(RuntimeError):
    def __init__(self, partner: str, message: str) -> None:
        super().__init__(f"{partner}: {message}")
        self.partner = partner


@dataclass
class Fetched:
    data: dict[str, Any]
    partner: str
    tool: str
    digest: str
    freshness: str  # fresh | cached | stale
    latency_ms: float


@dataclass
class CallLog:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, f: Fetched) -> None:
        self.calls.append(
            {
                "partner": f.partner,
                "tool": f.tool,
                "freshness": f.freshness,
                "latencyMs": round(f.latency_ms, 1),
                "digest": f.digest[:12],
            }
        )


class Partners(ABC):
    """The operations the compiler needs from the outside world."""

    @abstractmethod
    async def fetch(self, partner: str, tool: str, args: dict[str, Any], *, read: bool = True) -> Fetched: ...

    def health(self) -> dict[str, Any]:
        return {}

    # Typed helpers ------------------------------------------------------

    async def transport(self, origin: str, dest: str, on: date) -> tuple[list[TransportOption], Fetched]:
        f = await self.fetch(
            "transport",
            "search_transport",
            {"origin": origin, "destination": dest, "date_iso": on.isoformat()},
        )
        return [TransportOption.model_validate(x) for x in f.data["options"]], f

    async def stays(
        self, city: str, check_in: date, nights: int, guests: int
    ) -> tuple[list[StayQuote], Fetched]:
        f = await self.fetch(
            "stays",
            "search_stays",
            {"city": city, "check_in": check_in.isoformat(), "nights": nights, "guests": guests},
        )
        return [StayQuote.model_validate(x) for x in f.data["quotes"]], f

    async def restaurants(self, city: str) -> tuple[list[Restaurant], Fetched]:
        f = await self.fetch("dining", "search_restaurants", {"city": city})
        return [Restaurant.model_validate(x) for x in f.data["restaurants"]], f

    async def attractions(self, city: str) -> tuple[list[Attraction], Fetched]:
        f = await self.fetch("experiences", "search_attractions", {"city": city})
        return [Attraction.model_validate(x) for x in f.data["attractions"]], f

    async def forecast(self, city: str, start: date, days: int) -> tuple[list[WeatherDay], Fetched]:
        f = await self.fetch(
            "weather", "get_forecast", {"city": city, "start": start.isoformat(), "days": days}
        )
        return [WeatherDay.model_validate(x) for x in f.data["days"]], f

    async def statuses(self, ids: list[str]) -> list[TransportStatus]:
        f = await self.fetch("transport", "get_status", {"option_ids": ids}, read=False)
        return [TransportStatus.model_validate(x) for x in f.data["statuses"]]

    async def write(self, partner: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return (await self.fetch(partner, tool, args, read=False)).data


class McpPartners(Partners):
    def __init__(self, base_url: str, *, timeout_s: float = 1.5, cache_ttl_s: float = 120.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s
        self.breakers = {p: CircuitBreaker(p) for p in PARTNERS}
        self.bulkheads = {p: Bulkhead(16) for p in PARTNERS}
        self.cache: TTLCache[Fetched] = TTLCache(cache_ttl_s)
        self._sessions: dict[str, Any] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._start_lock = asyncio.Lock()

    async def _own(self, partner: str) -> None:
        """Owns one partner connection for its whole lifetime; reconnects with backoff."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        delay = 0.2
        while not self._stop.is_set():
            try:
                async with (
                    streamable_http_client(f"{self.base}/{partner}/mcp") as streams,
                    ClientSession(streams[0], streams[1]) as session,
                ):
                    await session.initialize()
                    self._sessions[partner] = session
                    self._ready[partner].set()
                    delay = 0.2
                    while not self._stop.is_set() and self._sessions.get(partner) is session:
                        await asyncio.sleep(0.25)
            except Exception as exc:
                log.debug("partner %s connection lost: %s", partner, exc)
            finally:
                self._sessions.pop(partner, None)
                self._ready[partner].clear()
            if not self._stop.is_set():
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def _session(self, partner: str) -> Any:
        if partner not in self._tasks:
            async with self._start_lock:
                if partner not in self._tasks:
                    self._ready[partner] = asyncio.Event()
                    self._tasks[partner] = asyncio.create_task(self._own(partner), name=f"partner-{partner}")
        await asyncio.wait_for(self._ready[partner].wait(), timeout=self.timeout * 2)
        return self._sessions[partner]

    async def start(self) -> None:
        await asyncio.gather(*(self._session(p) for p in PARTNERS), return_exceptions=True)

    async def close(self) -> None:
        self._stop.set()
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def fetch(self, partner: str, tool: str, args: dict[str, Any], *, read: bool = True) -> Fetched:
        key = f"{partner}:{tool}:{digest(args)}"
        if read:
            hit, stale = self.cache.get(key)
            if hit is not None and not stale:
                return Fetched(hit.data, partner, tool, hit.digest, "cached", 0.0)
        breaker = self.breakers[partner]
        t0 = time.perf_counter()

        async def once() -> dict[str, Any]:
            if not breaker.allow():
                raise BreakerOpen(f"{partner} circuit open")
            try:
                session = await self._session(partner)
                result = await asyncio.wait_for(session.call_tool(tool, args), timeout=self.timeout)
            except Exception:
                breaker.failure()
                raise
            if result.is_error:
                breaker.failure()
                text = " ".join(getattr(c, "text", "") for c in result.content)
                raise PartnerError(partner, text or "tool error")
            breaker.success()
            return dict(result.structured_content or {})

        try:
            data = await self.bulkheads[partner].run(
                lambda: (
                    retry(once, attempts=3 if read else 1, retry_on=(PartnerError, TimeoutError, OSError))
                    if read
                    else once()
                )
            )
        except Exception as exc:
            if read:
                stale_hit, _ = self.cache.get(key, allow_stale=True)
                if stale_hit is not None:
                    return Fetched(
                        stale_hit.data,
                        partner,
                        tool,
                        stale_hit.digest,
                        "stale",
                        (time.perf_counter() - t0) * 1000,
                    )
            raise PartnerError(partner, str(exc)) from exc
        fetched = Fetched(data, partner, tool, digest(data), "fresh", (time.perf_counter() - t0) * 1000)
        if read:
            self.cache.put(key, fetched)
        return fetched

    def health(self) -> dict[str, Any]:
        return {
            "breakers": {p: b.state for p, b in self.breakers.items()},
            "connected": sorted(self._sessions),
            "cache": self.cache.stats(),
        }


class LocalPartners(Partners):
    """Same contract, no network. Used by unit tests, property tests and benchmarks."""

    def __init__(self) -> None:
        from qed.partners import servers

        self._servers = servers
        from qed.domain.research import Research

        self.research = Research(online=False)  # deterministic: curated landmarks only
        self.cancelled: set[str] = set()
        self.holds: dict[str, dict[str, Any]] = {}
        self.fail_confirm: set[str] = set()
        self.wallet = {"balance": 20000, "auths": {}}

    async def fetch(self, partner: str, tool: str, args: dict[str, Any], *, read: bool = True) -> Fetched:
        t0 = time.perf_counter()
        data = self._dispatch(partner, tool, args)
        return Fetched(data, partner, tool, digest(data), "fresh", (time.perf_counter() - t0) * 1000)

    def _dispatch(self, partner: str, tool: str, a: dict[str, Any]) -> dict[str, Any]:
        if tool == "search_transport":
            opts = world.search_transport(a["origin"], a["destination"], date.fromisoformat(a["date_iso"]))
            return {"options": [o.model_dump(mode="json") for o in opts if o.id not in self.cancelled]}
        if tool == "search_stays":
            qs = world.quote_stays(a["city"], date.fromisoformat(a["check_in"]), a["nights"], a["guests"])
            return {"quotes": [q.model_dump(mode="json") for q in qs]}
        if tool == "search_restaurants":
            return {"restaurants": [r.model_dump(mode="json") for r in world.restaurants_for(a["city"])]}
        if tool == "search_attractions":
            return {"attractions": [x.model_dump(mode="json") for x in world.attractions_for(a["city"])]}
        if tool == "get_highlights":
            return self.research.highlights(a["city"])
        if tool == "get_forecast":
            return {
                "days": [
                    d.model_dump(mode="json")
                    for d in world.forecast(a["city"], date.fromisoformat(a["start"]), a["days"])
                ]
            }
        if tool == "get_status":
            return {
                "statuses": [
                    {
                        "option_id": i,
                        "status": "cancelled" if i in self.cancelled else "scheduled",
                        "delay_min": 0,
                        "updated_at": "local",
                    }
                    for i in a["option_ids"]
                ]
            }
        if tool.startswith("hold_"):
            key = a["idempotency_key"]
            if key not in self.holds:
                item = next(v for k, v in a.items() if k.endswith("_id"))
                if item in self.cancelled:
                    raise PartnerError(partner, f"{item} is cancelled")
                self.holds[key] = {
                    "holdId": f"H-{partner[:2].upper()}-{len(self.holds):04d}",
                    "itemId": item,
                    "status": "held",
                    "partner": partner,
                }
            return self.holds[key]
        if tool in ("confirm_hold", "release_hold"):
            rec = next(h for h in self.holds.values() if h["holdId"] == a["hold_id"])
            if tool == "confirm_hold" and partner in self.fail_confirm:
                raise PartnerError(partner, "confirmation rejected (injected fault)")
            rec["status"] = "confirmed" if tool == "confirm_hold" else "released"
            return rec
        if tool == "authorize":
            auth = {
                "authId": f"AUTH-{len(self.wallet['auths']):04d}",
                "amount": a["amount"],
                "status": "authorized",
            }
            self.wallet["auths"].setdefault(a["idempotency_key"], auth)
            return {**self.wallet["auths"][a["idempotency_key"]], "balance": self.wallet["balance"]}
        if tool in ("capture", "void"):
            if tool == "capture" and "wallet" in self.fail_confirm:
                raise PartnerError("wallet", "capture rejected (injected fault)")
            auth = next(x for x in self.wallet["auths"].values() if x["authId"] == a["auth_id"])
            auth["status"] = "captured" if tool == "capture" else "voided"
            return {**auth, "balance": self.wallet["balance"]}
        if tool == "call_property":
            facts = world.property_facts(a["stay_id"])
            if facts is None:
                raise PartnerError(partner, "unknown property")
            return self._servers._simulate_call(
                facts, a["stay_id"], a["questions"], a.get("on_behalf_of", "a guest")
            ).model_dump()
        raise PartnerError(partner, f"unknown tool {tool}")
