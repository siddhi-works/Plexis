"""Plexis as an Alexa+ add-on: a Streamable HTTP MCP server (protocol 2025-11-25).

Follows the Alexa+ MCP Toolkit conventions:
- one tool per customer intent, stable ids returned and accepted later;
- structuredContent (validated against outputSchema) + short text, and
  `_meta.ui.resourceUri` pointing at an MCP App card for Echo Show screens;
- explicit confirmation before payment (elicitation, or a confirmation-token
  fallback for hosts without elicitation), a price breakdown, a separate
  purchase confirmation, and duplicate-booking prevention;
- errors as `isError` results that let the host ask a follow-up question;
- two-tier OAuth: `mcp:service` for discovery, `mcp:tools` for personal actions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.apps import Apps, ResourceCsp
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from qed import telemetry as tm
from qed.agent.dialog import Dialog, Turn
from qed.companion.profile import interpret
from qed.config import Settings
from qed.domain import world
from qed.engine import CompileResult, Engine
from qed.partners.client import McpPartners
from qed.server.auth import Authority
from qed.server.present import plan_data, voice_options
from qed.util import fmt_money

CARD_URI = "ui://qed/trip-card.html"
CARD_HTML = (Path(__file__).parent / "card.html").read_text(encoding="utf-8")


class QedOutput(BaseModel):
    """Every Plexis tool returns this envelope."""

    status: str = Field(
        description="planned | infeasible | needs_input | rejected | booked | confirmation_required | ..."
    )
    speech: str = Field(description="Short voice-ready summary derived from the signed certificate.")
    planId: str | None = Field(default=None, description="Stable id to pass to later tools.")
    draftId: str | None = Field(
        default=None, description="Conversation id: pass it back to plan_trip with the customer's next reply."
    )
    tripId: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    voiceOptions: list[str] = Field(
        default_factory=list, description="At most 3 short options for voice-only devices."
    )
    trace: dict[str, Any] = Field(default_factory=dict, description="Stage latencies (ms) and partner calls.")


Out = Annotated[CallToolResult, QedOutput]


def ui_meta(invoking: str, invoked: str) -> dict[str, Any]:
    return {"ui": {"resourceUri": CARD_URI, "invoking": invoking, "invoked": invoked}}


def result(out: QedOutput, *, error: bool = False, card: bool = True) -> CallToolResult:
    meta = {"ui": {"resourceUri": CARD_URI}} if card else None
    return CallToolResult(
        content=[TextContent(type="text", text=out.speech)],
        structured_content=out.model_dump(mode="json"),
        is_error=error,
        meta=meta,
    )


def from_compile(res: CompileResult, engine: Engine) -> CallToolResult:
    out = QedOutput(
        status=res.status,
        speech=res.speech,
        planId=res.stored.plan_id if res.stored else None,
        data=plan_data(res, engine),
        voiceOptions=voice_options(res),
        trace={
            "totalMs": res.total_ms,
            "stages": res.timings,
            "partnerCalls": res.partner_calls,
            "graph": res.graph,
            "budgetMs": engine.s.latency_budget_ms,
        },
    )
    if res.status == "needs_input":
        out.data["missing"] = res.missing
    return result(out, error=res.status == "rejected")


def from_turn(turn: Turn, engine: Engine) -> CallToolResult:
    """One conversation turn: either a question with suggestions, or a compiled plan or conflict."""
    if turn.result is not None and turn.status in ("planned", "infeasible"):
        call = from_compile(turn.result, engine)
        sc = dict(call.structured_content or {})
        sc["speech"] = turn.speech
        call.content = [TextContent(type="text", text=turn.speech)]
    else:
        sc = QedOutput(status=turn.status, speech=turn.speech).model_dump(mode="json")
        if turn.result is not None:  # e.g. rejected after compiling
            sc["trace"] = {
                "totalMs": turn.result.total_ms,
                "stages": turn.result.timings,
                "graph": turn.result.graph,
            }
        call = CallToolResult(
            content=[TextContent(type="text", text=turn.speech)],
            structured_content=sc,
            is_error=turn.status == "rejected",
        )
    sc["draftId"] = turn.draft_id
    sc.setdefault("trace", {})["turnPath"] = turn.path
    sc["voiceOptions"] = [s.label for s in turn.suggestions][:3]
    sc.setdefault("data", {})["question"] = turn.question.model_dump() if turn.question else None
    call.structured_content = sc
    return call


class Confirm(BaseModel):
    confirm: bool = Field(default=False, description="Yes, go ahead")


class FixChoice(BaseModel):
    choice: str = Field(description="Which change should I make?")


def build_server(engine: Engine, settings: Settings, authority: Authority | None) -> MCPServer:
    apps = Apps()
    apps.add_html_resource(
        CARD_URI,
        CARD_HTML,
        name="Plexis trip card",
        title="Verified trip card",
        description="Echo Show card: itinerary, price breakdown, proof and fix-its.",
        csp=ResourceCsp(connect_domains=[], resource_domains=[]),
        prefers_border=False,
    )
    auth = None
    if authority is not None:
        auth = AuthSettings(
            issuer_url=authority.issuer,
            resource_server_url=settings.resource_url,  # type: ignore[arg-type]
            required_scopes=["mcp:service"],
        )
    server = MCPServer(
        "plexis",
        title="Plexis — trips you can trust",
        description="Verified trip planning for Alexa+: every plan is solved, independently checked and signed.",
        instructions=(
            "Use plan_trip for any new trip request, passing the customer's words verbatim. Use refine_trip "
            "for changes, explain_choice for why/why-not questions, book_trip only after the customer asks "
            "to book. Never read ids or tool names aloud."
        ),
        version="0.1.0",
        extensions=[apps],
        auth=auth,
        token_verifier=authority,
    )

    dialog = Dialog(engine)

    def personal() -> bool:
        if authority is None:
            return True
        tok = get_access_token()
        return tok is not None and "mcp:tools" in tok.scopes

    def link_account(action: str) -> CallToolResult:
        return result(
            QedOutput(
                status="needs_account_linking",
                speech=f"To {action}, link your Plexis account in the Alexa app first.",
            ),
            error=True,
            card=False,
        )

    def can_elicit(ctx: Context) -> bool:
        caps = ctx.client_capabilities
        return caps is not None and getattr(caps, "elicitation", None) is not None

    def progress_of(ctx: Context) -> Callable[[int, int, str], Awaitable[None]]:
        async def report(step: int, total: int, msg: str) -> None:
            await ctx.report_progress(step, total, msg)

        return report

    read = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
    act = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    )

    @server.tool(
        title="Plan a trip",
        annotations=read,
        meta=ui_meta("Planning and verifying your trip…", "Trip ready"),
        description="The trip-planning conversation. Pass every customer reply verbatim, plus the draftId "
        "from the previous response. Plexis asks for one missing detail at a time (destination, "
        "dates, travelers, departure city, budget, room needs) with up to three suggestions, then "
        "returns a verified, signed plan or a proven conflict with fixes. Replies like 'the first "
        "one', 'yes' or 'no preference' are understood in context. Synonyms: plan a vacation, "
        "getaway, weekend away, holiday, trip, travel itinerary.",
    )
    async def plan_trip(
        request: Annotated[str, Field(description="The customer's words, verbatim.")],
        ctx: Context,
        draft_id: Annotated[
            str | None,
            Field(description="draftId from the previous plan_trip response, to continue that conversation."),
        ] = None,
    ) -> Out:
        turn = await dialog.turn(request, draft_id, use_profile=personal(), progress=progress_of(ctx))
        return from_turn(turn, engine)

    @server.tool(
        title="Change a trip",
        annotations=read,
        meta=ui_meta("Re-planning…", "Updated plan ready"),
        description="Change an existing plan: cheaper, nicer, different budget or dates, add or remove a "
        "requirement, or apply a suggested fix (fix_id).",
    )
    async def refine_trip(
        plan_id: str,
        ctx: Context,
        change: Annotated[str, Field(description="What to change, in the customer's words.")] = "",
        fix_id: Annotated[str | None, Field(description="Id of a suggested fix to apply.")] = None,
    ) -> Out:
        try:
            if fix_id:
                res = await engine.apply_fix(plan_id, fix_id, progress=progress_of(ctx))
            else:
                res = await engine.refine(plan_id, change, use_profile=personal(), progress=progress_of(ctx))
        except KeyError as exc:
            return result(
                QedOutput(status="rejected", speech=f"I couldn't find that plan or fix. {exc.args[0]}"),
                error=True,
                card=False,
            )
        return from_compile(res, engine)

    @server.tool(
        title="Explain a choice",
        annotations=read,
        description="Answer why a hotel, restaurant, activity or flight was or wasn't chosen "
        "(e.g. 'why not the Harbor Inn?', 'why this flight?').",
    )
    async def explain_choice(plan_id: str, question: str) -> Out:
        try:
            ans = await engine.why(plan_id, question)
        except KeyError as exc:
            return result(QedOutput(status="rejected", speech=str(exc.args[0])), error=True, card=False)
        return result(
            QedOutput(status="explained", speech=ans["answer"], planId=plan_id, data=ans), card=False
        )

    @server.tool(
        title="Find better dates",
        annotations=read,
        description="Check whether nearby dates have better weather and re-plan them to compare prices.",
    )
    async def check_better_dates(plan_id: str) -> Out:
        try:
            ans = await engine.better_dates(plan_id)
        except KeyError as exc:
            return result(QedOutput(status="rejected", speech=str(exc.args[0])), error=True, card=False)
        return result(
            QedOutput(
                status="weather",
                speech=ans["message"],
                planId=plan_id,
                data=ans,
                voiceOptions=[o["change"].capitalize() for o in ans["options"]][:3],
            ),
            card=False,
        )

    @server.tool(
        title="Book a trip",
        annotations=act,
        meta=ui_meta("Booking…", "Booked"),
        description="Book a verified plan. Always confirms the total and price breakdown with the customer "
        "before any payment, and never books the same plan twice.",
    )
    async def book_trip(
        plan_id: str,
        ctx: Context,
        confirmation_token: Annotated[
            str | None, Field(description="Token from a previous confirmation_required response.")
        ] = None,
    ) -> Out:
        if not personal():
            return link_account("book trips")
        try:
            sp = engine.get(plan_id)
        except KeyError as exc:
            return result(QedOutput(status="rejected", speech=str(exc.args[0])), error=True, card=False)
        if sp.plan is None:
            return result(
                QedOutput(status="rejected", speech="That request had no valid plan to book."), error=True
            )
        if any(t.plan_id == plan_id and t.status in ("booked", "amended") for t in engine.trips.values()):
            booking = await engine.book(
                plan_id
            )  # idempotent: returns the existing receipt, never charges twice
            return result(
                QedOutput(
                    status=booking["status"],
                    speech=booking["message"],
                    planId=plan_id,
                    tripId=booking.get("tripId"),
                    data=booking,
                ),
                card=False,
            )
        b = sp.plan.breakdown
        summary = (
            f"Book this trip for {fmt_money(b['total'])}? That's {fmt_money(b['transport'])} transport, "
            f"{fmt_money(b['lodging'])} lodging, {fmt_money(b['dining'])} dining and "
            f"{fmt_money(b['activities'])} activities, charged once to your saved wallet."
        )
        token = hashlib.sha256(f"{plan_id}:{b['total']}".encode()).hexdigest()[:12]
        if can_elicit(ctx):
            answer = await ctx.elicit(summary, schema=Confirm)
            if answer.action != "accept" or not answer.data.confirm:
                return result(
                    QedOutput(status="cancelled", speech="Okay, I haven't booked anything.", planId=plan_id),
                    card=False,
                )
        elif confirmation_token != token:
            return result(
                QedOutput(
                    status="confirmation_required",
                    speech=summary,
                    planId=plan_id,
                    data={"confirmationToken": token, "breakdown": b},
                    voiceOptions=["Yes, book it", "No"],
                ),
                card=False,
            )
        booking = await engine.book(plan_id)
        trip_id = booking.get("tripId")
        if trip_id:
            SESSIONS[trip_id] = ctx.session
        data = dict(booking)
        if booking["status"] == "booked":
            data["suggestedRoutine"] = departure_routine(sp)
        return result(
            QedOutput(
                status=booking["status"], speech=booking["message"], planId=plan_id, tripId=trip_id, data=data
            ),
            error=booking["status"] in ("rolled_back", "rejected"),
            card=False,
        )

    @server.tool(
        title="Travel brief",
        annotations=read,
        description="What to wear each day, what to pack, food highlights and comfort notes for a planned trip.",
    )
    async def travel_brief(plan_id: str) -> Out:
        try:
            brief = await engine.brief(plan_id)
        except KeyError as exc:
            return result(QedOutput(status="rejected", speech=str(exc.args[0])), error=True, card=False)
        speech = (
            f"{brief['outlook']['summary']} Pack: {'; '.join(p.lower() for p in brief['packing'][2:5])}. "
            f"{brief['comfort']['notes'][0] if brief['comfort']['notes'] else ''}"
        )
        return result(QedOutput(status="brief", speech=speech, planId=plan_id, data=brief), card=False)

    @server.tool(
        title="Call the front desk",
        annotations=act,
        description="Place a short automated call to the planned hotel's front desk to ask practical "
        "questions (early check-in, parking, breakfast, accessibility details, cribs, shuttle). "
        "The caller always discloses it is an AI. Simulated in this build.",
    )
    async def call_front_desk(plan_id: str, questions: list[str], ctx: Context) -> Out:
        if not personal():
            return link_account("call on your behalf")
        try:
            sp = engine.get(plan_id)
        except KeyError as exc:
            return result(QedOutput(status="rejected", speech=str(exc.args[0])), error=True, card=False)
        if sp.plan is None:
            return result(
                QedOutput(status="rejected", speech="There's no hotel in this plan yet."), error=True
            )
        if can_elicit(ctx):
            ok = await ctx.elicit(
                f"I'll call {sp.plan.stay.stay.name}, say I'm an AI assistant calling for you, and "
                f"ask {len(questions[:5])} question(s). Go ahead?",
                schema=Confirm,
            )
            if ok.action != "accept" or not ok.data.confirm:
                return result(
                    QedOutput(status="cancelled", speech="Okay, no call made.", planId=plan_id), card=False
                )
        call = await engine.call_property(plan_id, questions)
        answers = "; ".join(f"{a['topic'].replace('_', ' ')}: {a['answer']}" for a in call["answers"])
        return result(
            QedOutput(
                status="called", speech=f"{sp.plan.stay.stay.name} says: {answers}", planId=plan_id, data=call
            ),
            card=False,
        )

    @server.tool(
        title="Household memory",
        annotations=act,
        description="List, remember or forget facts about the household that personalise every plan "
        "(mobility needs, diets, pets, home city, likes and dislikes). Saving asks for consent.",
    )
    async def household_memory(
        action: Literal["list", "remember", "forget", "forget_all"],
        ctx: Context,
        statement: str | None = None,
    ) -> Out:
        if not personal():
            return link_account("use household memory")
        store = engine.profile
        if action == "list":
            facts = store.public()
            speech = (
                ("I remember: " + "; ".join(f["text"] for f in facts["facts"]) + ".")
                if facts["facts"]
                else "I don't remember anything about your household yet."
            )
            learned = engine.learner.report()
            if (
                learned["phrases"]
                or learned["prefersValue"]
                or learned["mistakesLogged"]
                or learned["unknownPhrases"]
            ):
                speech += " " + engine.learner.summary()
            return result(
                QedOutput(status="memory", speech=speech, data={**facts, "learning": learned}), card=False
            )
        if action == "remember":
            cands = interpret(statement or "")
            if not cands:
                return result(
                    QedOutput(
                        status="not_understood",
                        speech="I couldn't turn that into something I can "
                        "use for planning. Try 'Grandma uses a wheelchair' or 'we love museums'.",
                    ),
                    card=False,
                )
            if can_elicit(ctx):
                ok = await ctx.elicit(
                    f'Remember for future trips: "{statement}"? You can ask me to forget it any time.',
                    schema=Confirm,
                )
                if ok.action != "accept" or not ok.data.confirm:
                    return result(
                        QedOutput(status="cancelled", speech="Okay, I won't remember that."), card=False
                    )
            saved = [store.remember(c, statement or "") for c in cands]
            return result(
                QedOutput(
                    status="remembered",
                    speech=f"Got it. I'll remember: {statement}",
                    data={"facts": [f.model_dump() for f in saved]},
                ),
                card=False,
            )
        if action == "forget_all":
            n = store.forget_all()
            phrases = engine.learner.forget()
            return result(
                QedOutput(
                    status="forgotten",
                    speech=f"Done. I forgot all {n} household facts and {phrases} learned phrase(s).",
                ),
                card=False,
            )
        gone = store.forget(statement or "")
        return result(
            QedOutput(
                status="forgotten",
                speech=("Forgotten: " + "; ".join(f.text for f in gone))
                if gone
                else "I didn't have anything matching that.",
                data={"removed": len(gone)},
            ),
            card=False,
        )

    @server.tool(
        title="Trip status",
        annotations=act,
        description="Show booked trips and live disruption alerts (cancellations). With accept_rebooking=true, "
        "books the verified replacement Plexis prepared.",
    )
    async def trip_status(ctx: Context, trip_id: str | None = None, accept_rebooking: bool = False) -> Out:
        trips = [t for t in engine.trips.values() if trip_id in (None, t.trip_id)]
        if not trips:
            return result(QedOutput(status="no_trips", speech="You have no booked trips yet."), card=False)
        trip = trips[-1]
        alert = next(
            (a for a in reversed(trip.alerts) if a.get("proposalPlanId") and not a.get("resolved")), None
        )
        if accept_rebooking and alert:
            if not personal():
                return link_account("rebook")
            booking = await engine.book(alert["proposalPlanId"])
            if booking["status"] == "booked":
                alert["resolved"] = True
                trip.status = "amended"
                trip.history.append({"rebookedTo": booking["tripId"], "receipt": booking["receipt"]})
                SESSIONS[booking["tripId"]] = ctx.session
            return result(
                QedOutput(
                    status=booking["status"],
                    speech=booking["message"],
                    tripId=booking.get("tripId"),
                    data=booking,
                ),
                card=False,
            )
        speech = alert["message"] if alert else f"Your trip {trip.trip_id} is on track. No disruptions."
        return result(
            QedOutput(
                status=trip.status if not alert else "disrupted",
                speech=speech,
                tripId=trip.trip_id,
                planId=trip.plan_id,
                data={"alerts": trip.alerts, "status": trip.status, "history": trip.history},
                voiceOptions=["Yes, rebook me", "Show other options"] if alert else [],
            ),
            card=False,
        )

    @server.tool(
        title="Verify a receipt",
        annotations=read,
        description="Independently verify a signed Plexis receipt: hash, Ed25519 signature, ledger chain link, "
        "and an offline re-check of every rule against the frozen partner data.",
    )
    async def verify_receipt(receipt_index: int) -> Out:
        v = engine.verify_receipt(receipt_index)
        speech = (
            f"Receipt {receipt_index} is authentic: signature valid, chain intact"
            + (
                f", and all {v['offlineRecheck']['checks']} rules re-checked offline."
                if v.get("offlineRecheck")
                else "."
            )
            if v["ok"]
            else f"Receipt {receipt_index} failed verification: {'; '.join(v['problems'])}"
        )
        return result(
            QedOutput(status="verified" if v["ok"] else "tampered", speech=speech, data=v),
            error=not v["ok"],
            card=False,
        )

    @server.resource(
        "qed://receipts/{index}",
        name="receipt",
        title="Signed receipt",
        mime_type="application/json",
        description="A ledger entry with its full signed certificate.",
    )
    def receipt_resource(index: str) -> dict[str, Any]:
        e = engine.ledger.get(int(index))
        return {"error": "not found"} if e is None else {**e.public(), "certificate": e.body}

    @server.resource(
        "qed://trips/{trip_id}",
        name="trip",
        title="Trip status",
        mime_type="application/json",
        description="Booked trip with live disruption alerts. Subscribe for updates.",
    )
    def trip_resource(trip_id: str) -> dict[str, Any]:
        t = engine.trips.get(trip_id)
        return (
            {"error": "not found"}
            if t is None
            else {
                "tripId": t.trip_id,
                "planId": t.plan_id,
                "status": t.status,
                "alerts": t.alerts,
                "history": t.history,
            }
        )

    @server.resource(
        "qed://ledger/head", name="ledger-head", title="Ledger head", mime_type="application/json"
    )
    def ledger_head() -> dict[str, Any]:
        head = engine.ledger.head()
        return {
            "head": head.public() if head else None,
            "verify": engine.ledger.verify_chain(),
            "publicKey": engine.signer.public_b64,
            "keyId": engine.signer.key_id,
        }

    @server.prompt(
        title="Weekend getaway", description="Plan a weekend away with the household's saved preferences."
    )
    def weekend_getaway(city: str, budget: str = "1500") -> str:
        return f"Plan a weekend trip to {city} under ${budget} for the household."

    return server


SESSIONS: dict[str, Any] = {}


def departure_routine(sp: Any) -> dict[str, Any]:
    """A suggested Alexa routine for departure day (returned as data; the customer enables it)."""
    out = sp.plan.outbound
    leave_by = max(0, out.depart_min - (120 if out.mode == "flight" else 45))
    h, m = divmod(leave_by, 60)
    return {
        "name": f"Trip to {world.city_name(sp.intent.destination)}",
        "trigger": {"date": out.date.isoformat(), "time": f"{max(0, h - 1):02d}:{m:02d}"},
        "actions": [
            f"Announce: leave by {h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'} for {out.carrier} {out.code}",
            "Read the weather for departure and destination",
            "Set thermostat to away mode after departure",
            "Turn off all lights at departure time",
        ],
        "note": "Suggested only. Enable it in the Alexa app.",
    }


class RateLimit:
    """Token bucket per caller (auth header or client address), applied to /mcp."""

    def __init__(self, app: ASGIApp, per_minute: int) -> None:
        self.app, self.rate, self.cap = app, per_minute / 60.0, float(per_minute)
        self.buckets: dict[str, tuple[float, float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp") or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        key = hashlib.sha256(
            headers.get(b"authorization", b"") or str(scope.get("client")).encode()
        ).hexdigest()[:16]
        now = time.monotonic()
        tokens, last = self.buckets.get(key, (self.cap, now))
        tokens = min(self.cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            tm.GUARD.labels("rate_limited").inc()
            resp = JSONResponse(
                {"error": "rate_limited", "retryAfterSeconds": int(1 / self.rate) + 1},
                status_code=429,
                headers={"Retry-After": str(int(1 / self.rate) + 1)},
            )
            return await resp(scope, receive, send)
        self.buckets[key] = (tokens - 1, now)
        return await self.app(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app: ASGIApp, *, strip_www_authenticate: bool = False) -> None:
        self.app, self.strip = app, strip_www_authenticate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not (self.strip and message["status"] == 401 and k.lower() == b"www-authenticate")
                ]
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if scope["path"].startswith(("/mcp", "/oauth")):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


def create_app(settings: Settings, *, partners: Any = None) -> Starlette:
    tm.setup(json_logs=settings.log_json, otel_console=settings.otel_console)
    partners = partners or McpPartners(
        settings.partners_url, timeout_s=settings.partner_timeout_s, cache_ttl_s=settings.cache_ttl_s
    )
    engine = Engine(settings, partners, seed_demo_household=settings.dev_mode)
    issuer = (settings.public_url or f"http://{settings.host}:{settings.port}").rstrip("/")
    authority = (
        Authority(settings.state_dir, issuer, settings.resource_url, settings.jwt_secret)
        if settings.auth_enabled
        else None
    )
    server = build_server(engine, settings, authority)

    async def notify(trip: Any, alert: dict[str, Any]) -> None:
        session = SESSIONS.get(trip.trip_id)
        if session is None:
            return
        with contextlib.suppress(Exception):
            await session.send_resource_updated(f"qed://trips/{trip.trip_id}")
        # Logging notifications are deprecated in protocol 2026-07-28 but are still how a
        # 2025-11-25 session (what Alexa+ speaks today) receives an unsolicited alert.
        with contextlib.suppress(Exception), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await session.send_log_message(
                level="warning", data={"type": "qed/disruption", **alert}, logger="qed.disruptions"
            )

    engine.alert_listeners.append(notify)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> Response:
        health = partners.health()
        chain = engine.ledger.verify_chain()
        ok = chain["ok"]
        return JSONResponse(
            {
                "ready": ok,
                "ledger": chain,
                "partners": health,
                "llm": engine.bedrock.available,
                "llmLastError": engine.bedrock.last_error,
            },
            status_code=200 if ok else 503,
        )

    @server.custom_route("/metrics", methods=["GET"])
    async def metrics(_: Request) -> Response:
        if hasattr(partners, "breakers"):
            for p, b in partners.breakers.items():
                tm.BREAKER.labels(p).set(1 if b.state == "open" else 0)
        return PlainTextResponse(tm.metrics_text().decode(), media_type="text/plain; version=0.0.4")

    if authority is not None:
        server.custom_route("/oauth/token", methods=["POST"])(authority.token)
        server.custom_route("/oauth/authorize", methods=["GET"])(authority.authorize)
        server.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])(authority.metadata)

    if settings.dev_mode:

        @server.custom_route("/dev/chaos", methods=["GET", "POST"])
        async def dev_chaos(request: Request) -> Response:
            body = await request.body() if request.method == "POST" else None
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.request(
                    request.method,
                    f"{settings.partners_url}/admin/chaos",
                    content=body,
                    headers={"content-type": "application/json"},
                )
            if request.method == "POST":
                with contextlib.suppress(Exception):
                    await engine.check_disruptions()
            return JSONResponse(r.json(), status_code=r.status_code)

        @server.custom_route("/dev/ledger", methods=["GET"])
        async def dev_ledger(_: Request) -> Response:
            n = engine.ledger.count()
            entries = [engine.ledger.get(i) for i in range(max(0, n - 30), n)]
            return JSONResponse(
                {
                    "verify": engine.ledger.verify_chain(),
                    "publicKey": engine.signer.public_b64,
                    "keyId": engine.signer.key_id,
                    "entries": [{**e.public(), "kind": e.kind} for e in entries if e],
                }
            )

        @server.custom_route("/dev/ledger/{index}", methods=["GET"])
        async def dev_ledger_entry(request: Request) -> Response:
            e = engine.ledger.get(int(request.path_params["index"]))
            return (
                JSONResponse({"error": "not found"}, status_code=404)
                if e is None
                else JSONResponse({**e.public(), "certificate": e.body})
            )

    origins = [*list(settings.allowed_origins), "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", f"{settings.host}:*"],
        allowed_origins=origins,
    )
    inner = server.streamable_http_app(
        streamable_http_path="/mcp", host=settings.host, transport_security=security
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with inner.router.lifespan_context(inner):
            if hasattr(partners, "start"):
                await partners.start()
            stop = asyncio.Event()

            async def monitor() -> None:
                while not stop.is_set():
                    with contextlib.suppress(Exception):
                        await engine.check_disruptions()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=3)

            async def warm() -> None:
                with contextlib.suppress(Exception):
                    start = world.today() + __import__("datetime").timedelta(days=14)
                    await engine.compile(
                        f"3 nights from Seattle to San Francisco on {start.isoformat()}",
                        sign=False,
                        use_profile=False,
                    )

            tasks = [asyncio.create_task(monitor()), asyncio.create_task(warm())]
            app.state.engine = engine
            try:
                yield
            finally:
                stop.set()
                for t in tasks:
                    t.cancel()
                if hasattr(partners, "close"):
                    await partners.close()
                with contextlib.suppress(Exception):
                    engine.ledger.backup()

    app = Starlette(routes=[Mount("/", app=inner)], lifespan=lifespan)
    app.add_middleware(RateLimit, per_minute=settings.rate_limit_per_minute)
    app.add_middleware(SecurityHeaders, strip_www_authenticate=settings.alexa_compat_401)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "authorization",
            "content-type",
            "mcp-session-id",
            "mcp-protocol-version",
            "last-event-id",
            "accept",
        ],
        expose_headers=["mcp-session-id", "mcp-protocol-version", "www-authenticate", "retry-after"],
    )
    return app
