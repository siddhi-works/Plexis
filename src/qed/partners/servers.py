"""Simulated partner ecosystem: seven independent MCP servers.

In production these would be the airline, hotel, restaurant, ticketing,
weather, payments and telephony partners that Alexa+ add-ons integrate with.
Here each is a real Streamable HTTP MCP server answering from the seeded world,
mounted under one process for convenience (`/transport/mcp`, `/stays/mcp`, ...).

A chaos layer can inject latency, errors and flight cancellations per partner,
which is how the resilience, degradation and disruption features are tested
and demonstrated.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from qed.domain import world
from qed.domain.models import (
    Attraction,
    PropertyFacts,
    Restaurant,
    StayQuote,
    TransportOption,
    TransportStatus,
    WeatherDay,
)
from qed.domain.research import Research

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class ChaosState:
    """Per-partner fault injection, controlled through /admin/chaos."""

    def __init__(self) -> None:
        self.cfg: dict[str, dict[str, float]] = {}
        self.status: dict[str, TransportStatus] = {}
        self.fail_confirm: set[str] = set()

    async def gate(self, partner: str) -> None:
        cfg = self.cfg.get(partner, {})
        latency = cfg.get("latency_ms", 0.0)
        if latency:
            await asyncio.sleep(latency / 1000)
        if random.random() < cfg.get("error_rate", 0.0):  # noqa: S311
            raise ToolError(f"{partner} partner is temporarily unavailable (injected fault)")


CHAOS = ChaosState()


class Holds:
    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}

    def hold(self, partner: str, item_id: str, detail: dict[str, Any], idem: str) -> dict[str, Any]:
        if idem in self.by_key:
            return self.by_id[self.by_key[idem]]
        hid = f"H-{partner[:2].upper()}-{uuid.uuid4().hex[:10]}"
        rec = {"holdId": hid, "itemId": item_id, "status": "held", "partner": partner, **detail}
        self.by_id[hid] = rec
        self.by_key[idem] = hid
        return rec

    def set(self, hid: str, status: str) -> dict[str, Any]:
        rec = self.by_id.get(hid)
        if rec is None:
            raise ToolError(f"unknown hold {hid}")
        if rec["status"] == "released" and status == "confirmed":
            raise ToolError(f"hold {hid} was released and cannot be confirmed")
        rec["status"] = status
        return rec


HOLDS = Holds()


class TransportResults(BaseModel):
    options: list[TransportOption]


class StayResults(BaseModel):
    quotes: list[StayQuote]


class RestaurantResults(BaseModel):
    restaurants: list[Restaurant]


class AttractionResults(BaseModel):
    attractions: list[Attraction]


class WeatherResults(BaseModel):
    days: list[WeatherDay]


class StatusResults(BaseModel):
    statuses: list[TransportStatus]


class HoldResult(BaseModel):
    holdId: str
    itemId: str
    status: str
    partner: str


class WalletResult(BaseModel):
    authId: str
    amount: int
    status: str
    balance: int


class CallTranscript(BaseModel):
    stayId: str
    property: str
    disclosed_ai: bool
    transcript: list[dict[str, str]]
    answers: list[dict[str, str]]
    simulated: bool = True


def _confirmable(partner: str) -> None:
    if partner in CHAOS.fail_confirm:
        raise ToolError(f"{partner} partner rejected the confirmation (injected fault)")


def _hold_tools(server: MCPServer, partner: str) -> None:
    @server.tool(name="confirm_hold", annotations=WRITE, description="Confirm a held item. Idempotent.")
    async def confirm_hold(hold_id: str) -> HoldResult:
        await CHAOS.gate(partner)
        _confirmable(partner)
        return HoldResult.model_validate(HOLDS.set(hold_id, "confirmed"))

    @server.tool(
        name="release_hold", annotations=WRITE, description="Release a hold (compensation). Idempotent."
    )
    async def release_hold(hold_id: str) -> HoldResult:
        return HoldResult.model_validate(HOLDS.set(hold_id, "released"))


def build_partners() -> dict[str, MCPServer]:
    transport = MCPServer(
        "cascade-transport",
        title="Transport partner",
        version="1.0.0",
        instructions="Flights, trains and rental cars between supported cities.",
    )

    @transport.tool(
        annotations=READ, description="Search flights, trains and drives for one leg on one date."
    )
    async def search_transport(origin: str, destination: str, date_iso: str) -> TransportResults:
        await CHAOS.gate("transport")
        opts = world.search_transport(origin, destination, date.fromisoformat(date_iso))
        live = [
            o
            for o in opts
            if CHAOS.status.get(o.id, None) is None or CHAOS.status[o.id].status != "cancelled"
        ]
        return TransportResults(options=live)

    @transport.tool(annotations=READ, description="Live status for booked transport options.")
    async def get_status(option_ids: list[str]) -> StatusResults:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        return StatusResults(
            statuses=[
                CHAOS.status.get(i) or TransportStatus(option_id=i, status="scheduled", updated_at=now)
                for i in option_ids
            ]
        )

    @transport.tool(annotations=WRITE, description="Hold seats for an option.")
    async def hold_transport(option_id: str, travelers: int, idempotency_key: str) -> HoldResult:
        await CHAOS.gate("transport")
        st = CHAOS.status.get(option_id)
        if st and st.status == "cancelled":
            raise ToolError(f"{option_id} is cancelled")
        return HoldResult.model_validate(
            HOLDS.hold("transport", option_id, {"travelers": travelers}, idempotency_key)
        )

    _hold_tools(transport, "transport")

    stays = MCPServer("harbor-stays", title="Lodging partner", version="1.0.0")

    @stays.tool(
        annotations=READ, description="Quote every property in a city for the given nights and guests."
    )
    async def search_stays(city: str, check_in: str, nights: int, guests: int) -> StayResults:
        await CHAOS.gate("stays")
        return StayResults(quotes=world.quote_stays(city, date.fromisoformat(check_in), nights, guests))

    @stays.tool(annotations=WRITE, description="Hold a property.")
    async def hold_stay(stay_id: str, check_in: str, nights: int, idempotency_key: str) -> HoldResult:
        await CHAOS.gate("stays")
        return HoldResult.model_validate(
            HOLDS.hold("stays", stay_id, {"checkIn": check_in, "nights": nights}, idempotency_key)
        )

    _hold_tools(stays, "stays")

    dining = MCPServer("table-dining", title="Dining partner", version="1.0.0")

    @dining.tool(
        annotations=READ, description="All restaurants in a city with cuisines, diets, hours and prices."
    )
    async def search_restaurants(city: str) -> RestaurantResults:
        await CHAOS.gate("dining")
        return RestaurantResults(restaurants=list(world.restaurants_for(city)))

    @dining.tool(annotations=WRITE, description="Hold a table.")
    async def hold_table(
        restaurant_id: str, date_iso: str, meal: str, party: int, idempotency_key: str
    ) -> HoldResult:
        await CHAOS.gate("dining")
        return HoldResult.model_validate(
            HOLDS.hold(
                "dining", restaurant_id, {"date": date_iso, "meal": meal, "party": party}, idempotency_key
            )
        )

    _hold_tools(dining, "dining")

    experiences = MCPServer("lantern-experiences", title="Experiences partner", version="1.0.0")

    @experiences.tool(
        annotations=READ, description="Attractions in a city with hours, prices and accessibility."
    )
    async def search_attractions(city: str) -> AttractionResults:
        await CHAOS.gate("experiences")
        return AttractionResults(attractions=list(world.attractions_for(city)))

    @experiences.tool(annotations=WRITE, description="Hold tickets.")
    async def hold_tickets(attraction_id: str, date_iso: str, party: int, idempotency_key: str) -> HoldResult:
        await CHAOS.gate("experiences")
        return HoldResult.model_validate(
            HOLDS.hold("experiences", attraction_id, {"date": date_iso, "party": party}, idempotency_key)
        )

    _hold_tools(experiences, "experiences")

    weather = MCPServer("skyline-weather", title="Weather partner", version="1.0.0")

    @weather.tool(
        annotations=READ,
        description="Daily forecast: live (Open-Meteo) within 15 days, seasonal outlook beyond that.",
    )
    async def get_forecast(city: str, start: str, days: int) -> WeatherResults:
        await CHAOS.gate("weather")
        first = date.fromisoformat(start)
        live = await asyncio.to_thread(live_forecast, city, first, days)
        return WeatherResults(days=live or world.forecast(city, first, days))

    wallet = MCPServer("vault-wallet", title="Payments partner", version="1.0.0")
    ledger: dict[str, Any] = {"balance": 20000, "auths": {}, "keys": {}}

    @wallet.tool(annotations=WRITE, description="Authorize (reserve) an amount. Idempotent per key.")
    async def authorize(amount: int, idempotency_key: str) -> WalletResult:
        await CHAOS.gate("wallet")
        if idempotency_key in ledger["keys"]:
            a = ledger["auths"][ledger["keys"][idempotency_key]]
            return WalletResult(
                authId=a["id"], amount=a["amount"], status=a["status"], balance=ledger["balance"]
            )
        if amount <= 0 or amount > ledger["balance"]:
            raise ToolError("insufficient funds or invalid amount")
        aid = f"AUTH-{uuid.uuid4().hex[:10]}"
        ledger["auths"][aid] = {"id": aid, "amount": amount, "status": "authorized"}
        ledger["keys"][idempotency_key] = aid
        ledger["balance"] -= amount
        return WalletResult(authId=aid, amount=amount, status="authorized", balance=ledger["balance"])

    @wallet.tool(annotations=WRITE, description="Capture an authorization.")
    async def capture(auth_id: str) -> WalletResult:
        a = ledger["auths"].get(auth_id)
        if not a:
            raise ToolError("unknown authorization")
        _confirmable("wallet")
        a["status"] = "captured"
        return WalletResult(authId=auth_id, amount=a["amount"], status="captured", balance=ledger["balance"])

    @wallet.tool(annotations=WRITE, description="Void an authorization and return funds (compensation).")
    async def void(auth_id: str) -> WalletResult:
        a = ledger["auths"].get(auth_id)
        if not a:
            raise ToolError("unknown authorization")
        if a["status"] == "authorized":
            a["status"] = "voided"
            ledger["balance"] += a["amount"]
        return WalletResult(authId=auth_id, amount=a["amount"], status=a["status"], balance=ledger["balance"])

    frontdesk = MCPServer(
        "concierge-line",
        title="Front-desk calling partner (simulated)",
        version="1.0.0",
        instructions="Simulated outbound calls to property front desks. Production path: Amazon "
        "Connect outbound + Amazon Nova Sonic speech-to-speech.",
    )

    @frontdesk.tool(
        annotations=READ,
        description="Place a (simulated) call to a property's front desk and ask "
        "pre-approved questions. The caller always discloses it is an AI.",
    )
    async def call_property(
        stay_id: str, questions: list[str], on_behalf_of: str = "a guest"
    ) -> CallTranscript:
        await CHAOS.gate("frontdesk")
        facts = world.property_facts(stay_id)
        if facts is None:
            raise ToolError("unknown property")
        name = next((s.name for s in world.stays_for(stay_id.split("-")[1]) if s.id == stay_id), stay_id)
        await asyncio.sleep(0.6 + random.random() * 0.6)  # noqa: S311 - a phone call takes a moment
        return _simulate_call(facts, name, questions, on_behalf_of)

    guide = MCPServer(
        "trail-guide",
        title="Destination research partner",
        version="1.0.0",
        instructions="Real places worth seeing: curated landmarks plus Wikivoyage listings (CC BY-SA 4.0).",
    )
    research = Research(Path(os.environ.get("QED_STATE_DIR", "var")) / "research")

    @guide.tool(
        annotations=READ, description="Places worth seeing in a city, each with a one-line reason and source."
    )
    async def get_highlights(city: str) -> Highlights:
        await CHAOS.gate("guide")
        return Highlights.model_validate(await asyncio.to_thread(research.highlights, city))

    return {
        "transport": transport,
        "stays": stays,
        "dining": dining,
        "experiences": experiences,
        "weather": weather,
        "wallet": wallet,
        "frontdesk": frontdesk,
        "guide": guide,
    }


class Place(BaseModel):
    name: str
    why: str
    source: str


class Highlights(BaseModel):
    city: str
    places: list[Place]
    tagline: str
    source: str
    url: str | None = None


_WMO = {0: "sunny", 1: "sunny", 2: "cloudy", 3: "cloudy", 45: "cloudy", 48: "cloudy"}
_live_cache: dict[tuple[str, str, int], list[WeatherDay]] = {}


def live_forecast(city: str, start: date, days: int) -> list[WeatherDay] | None:
    """Real forecast from Open-Meteo (free, no key) when every day is within its 16-day horizon."""
    today = world.today()
    if os.environ.get("QED_LIVE_WEATHER", "true").lower() in ("0", "false", "no") or city not in world.CITIES:
        return None
    if (start - today).days < 0 or (start + timedelta(days=days - 1) - today).days > 15:
        return None
    key = (city, start.isoformat(), days)
    if key in _live_cache:
        return _live_cache[key]
    c = world.CITIES[city]
    try:
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            timeout=1.0,
            params={
                "latitude": c.lat,
                "longitude": c.lon,
                "timezone": "auto",
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=days - 1)).isoformat(),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
            },
        )
        d = r.json()["daily"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    out = []
    for i, day in enumerate(d["time"]):
        code = int(d["weather_code"][i] or 0)
        cond = _WMO.get(code) or ("storm" if code >= 95 else "snow" if 71 <= code <= 86 else "rain")
        out.append(
            WeatherDay(
                city=city,
                date=date.fromisoformat(day),
                condition=cond,  # type: ignore[arg-type]
                high_c=round(d["temperature_2m_max"][i]),
                low_c=round(d["temperature_2m_min"][i]),
                precip_pct=int(d["precipitation_probability_max"][i] or 0),
                wind_kph=round(d["wind_speed_10m_max"][i] or 0),
                source="forecast",
            )
        )
    _live_cache[key] = out
    return out


TOPICS = {
    "early_checkin": ("early", "check in early", "check-in early", "early check"),
    "late_checkout": ("late checkout", "late check-out", "check out late", "stay later"),
    "parking": ("park", "parking", "garage", "car"),
    "breakfast": ("breakfast", "morning meal"),
    "accessibility_detail": ("wheelchair", "accessible", "step-free", "roll-in", "grab bar", "mobility"),
    "cribs": ("crib", "cot", "baby", "infant"),
    "shuttle": ("shuttle", "airport", "pickup", "transfer"),
    "quiet_hours": ("quiet", "noise", "loud"),
}


def classify(question: str) -> str | None:
    q = question.lower()
    for topic, keys in TOPICS.items():
        if any(k in q for k in keys):
            return topic
    return None


def _simulate_call(facts: PropertyFacts, name: str, questions: list[str], who: str) -> CallTranscript:
    t = [
        {"speaker": "front desk", "text": f"Thank you for calling {name}, this is the front desk."},
        {
            "speaker": "Plexis",
            "text": f"Hi! I'm an automated AI assistant calling on behalf of {who}. "
            "I have a couple of quick questions about a possible stay. Is now okay?",
        },
        {"speaker": "front desk", "text": "Sure, go ahead."},
    ]
    answers = []
    for q in questions[:5]:
        topic = classify(q)
        t.append({"speaker": "Plexis", "text": q})
        if topic is None:
            a = "I'm not sure about that one. The reservations team can confirm by email."
        else:
            a = getattr(facts, topic)
        t.append({"speaker": "front desk", "text": a})
        answers.append({"question": q, "topic": topic or "unknown", "answer": a})
    t.append({"speaker": "Plexis", "text": "That's everything, thank you for your help!"})
    return CallTranscript(
        stayId=facts.stay_id, property=name, disclosed_ai=True, transcript=t, answers=answers
    )


def build_app(host: str = "127.0.0.1") -> Starlette:
    servers = build_partners()
    mounts = [
        Mount(
            f"/{name}",
            app=s.streamable_http_app(
                streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host
            ),
        )
        for name, s in servers.items()
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for s in servers.values():
                await stack.enter_async_context(s.session_manager.run())
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "partners": list(servers)})

    async def chaos(request: Request) -> JSONResponse:
        body = await request.json() if request.method == "POST" else {}
        if request.method == "POST":
            partner = body.get("partner")
            if body.get("reset"):
                CHAOS.cfg.clear()
                CHAOS.status.clear()
                CHAOS.fail_confirm.clear()
            elif partner:
                CHAOS.cfg[partner] = {
                    "latency_ms": float(body.get("latency_ms", 0)),
                    "error_rate": float(body.get("error_rate", 0)),
                }
                if "fail_confirm" in body:
                    (CHAOS.fail_confirm.add if body["fail_confirm"] else CHAOS.fail_confirm.discard)(partner)
            if body.get("cancel"):
                oid = body["cancel"]
                CHAOS.status[oid] = TransportStatus(
                    option_id=oid,
                    status="cancelled",
                    updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
                )
        return JSONResponse(
            {
                "config": CHAOS.cfg,
                "cancelled": [k for k, v in CHAOS.status.items() if v.status == "cancelled"],
                "failConfirm": sorted(CHAOS.fail_confirm),
            }
        )

    return Starlette(
        routes=[Route("/healthz", health), Route("/admin/chaos", chaos, methods=["GET", "POST"]), *mounts],
        lifespan=lifespan,
    )
