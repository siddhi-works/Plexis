"""The compiler driver.

    utterance ──guard──▶ parse (rules ▸ Bedrock escalation) ──profile──▶ TripIntent
        ──ground (parallel, speculative, cached)──▶ Catalog
        ──CP-SAT──▶ plan ──verify (independent) ∥ Z3 cross-check──▶ certificate ──Ed25519 + ledger──▶ receipt
                 └─▶ infeasible ──MUS ▸ fix-its (re-solved) ∥ Z3 confirms conflict──▶ signed diagnosis

Everything the user hears is derived from a signed certificate.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import z3
from ortools import __version__ as ORTOOLS_VERSION

from qed import telemetry as tm
from qed.agent.learning import Learner
from qed.companion.brief import build_brief
from qed.companion.profile import ProfileStore
from qed.compiler import rules
from qed.config import Settings
from qed.domain import world
from qed.graph import Graph
from qed.guard import grounded, scan_untrusted, screen_input
from qed.ir import (
    CONSTRAINT_KINDS,
    ActivitiesPerDay,
    Diagnostic,
    ExcludeItem,
    IncludeItem,
    TripIntent,
)
from qed.llm import Bedrock
from qed.partners.client import CallLog, Fetched, PartnerError, Partners
from qed.receipts.ledger import Entry, Ledger, Signer, verify_signature
from qed.solver import z3check
from qed.solver.catalog import Catalog
from qed.solver.cpsat import TripModel
from qed.solver.diagnose import Conflict, conflict_diagnostic, explain_conflict, minimal_core, why_not
from qed.solver.plan import Plan
from qed.speech import conflict_speech, missing_speech, plan_speech
from qed.util import Stopwatch, canonical_json, digest, fmt_minutes, fmt_money, sha256_hex
from qed.verify.checker import Verdict, verify

MAX_NIGHTS = 14


@dataclass
class StoredPlan:
    plan_id: str
    intent: TripIntent
    catalog: Catalog
    plan: Plan | None
    verdict: Verdict | None
    certificate: dict[str, Any]
    entry: Entry | None
    conflict: Conflict | None = None
    created: float = field(default_factory=time.time)


@dataclass
class Trip:
    trip_id: str
    plan_id: str
    receipt_index: int
    holds: list[dict[str, Any]]
    auth_id: str | None
    status: str = "booked"
    alerts: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CompileResult:
    status: str  # planned | infeasible | needs_input | rejected
    intent: TripIntent | None
    stored: StoredPlan | None
    speech: str
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    timings: list[dict[str, Any]] = field(default_factory=list)
    partner_calls: list[dict[str, Any]] = field(default_factory=list)
    total_ms: float = 0.0
    weather_advice: dict[str, Any] | None = None
    graph: list[dict[str, Any]] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        settings: Settings,
        partners: Partners,
        *,
        state_dir: Path | None = None,
        seed_demo_household: bool = True,
    ) -> None:
        self.s = settings
        self.partners = partners
        self.state = state_dir or settings.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "snapshots").mkdir(exist_ok=True)
        self.signer = Signer(self.state / "keys")
        self.ledger = Ledger(
            self.state, self.signer, backup_every=settings.backup_every, s3_bucket=settings.backup_s3_bucket
        )
        self.profile = ProfileStore(self.state, seed_demo=seed_demo_household)
        self.learner = Learner(self.state)
        self.bedrock = Bedrock(settings)
        self.plans: dict[str, StoredPlan] = {}
        self.trips: dict[str, Trip] = {}
        self.alert_listeners: list[Any] = []

    # ------------------------------------------------------------------ compile

    async def compile(
        self,
        utterance: str,
        *,
        intent: TripIntent | None = None,
        overrides: dict[str, Any] | None = None,
        sign: bool = True,
        use_profile: bool = True,
        progress: Any = None,
    ) -> CompileResult:
        sw = Stopwatch()
        calls = CallLog()
        notes: list[str] = []

        async def report(step: int, msg: str) -> None:
            if progress is not None:
                try:
                    await progress(step, 7, msg)
                except Exception:  # noqa: S110 - progress is best effort
                    pass

        with sw.stage("guard"):
            verdict_in = screen_input(
                utterance, max_chars=self.s.max_request_chars, guardrail=self.bedrock.guardrail()
            )
        if not verdict_in.allowed:
            tm.GUARD.labels("input_rejected").inc()
            tm.PLANS.labels("rejected").inc()
            return CompileResult(
                "rejected",
                None,
                None,
                verdict_in.reason or "I can't process that request.",
                timings=sw.stages,
                total_ms=sw.elapsed_ms(),
            )
        text = verdict_in.text
        notes += verdict_in.notes

        await report(1, "Understanding your request")
        with sw.stage("parse") as st:
            if intent is None:
                intent = rules.parse(text, known_people=self.profile.people)
            st["coverage"] = intent.coverage
            st["parser"] = "rules"

        # Speculative grounding: start fetching as soon as the route and dates are known,
        # while the (optional) LLM and profile steps run.
        spec_key, spec_task = None, None
        graph: list[dict[str, Any]] = []
        if intent.origin and intent.destination and intent.start_date and intent.nights:
            spec_key = self._ground_key(intent)
            spec_task = asyncio.create_task(self._ground(intent, calls, t0=sw.t0, trace=graph))

        leftovers = [d.span.text for d in intent.diagnostics if d.code == "QED-W001" and d.span]
        if self.bedrock.available and (leftovers or intent.missing()):
            with sw.stage("llm-parse") as st:
                kept, dropped, unsupported = await self.bedrock.parse(text, leftovers or intent.missing())
                st["kept"], st["dropped"] = len(kept), len(dropped)
                self._merge_llm(intent, kept, dropped, unsupported)
                if kept:
                    intent.parser = "rules+bedrock"

        with sw.stage("profile"):
            if overrides:
                self._apply_overrides(intent, overrides)
            if use_profile:
                notes += self.profile.apply(intent)
            if not any(c.kind == "activities_per_day" for c in intent.constraints):
                intent.constraints.append(ActivitiesPerDay(origin="default", min=1, max=2))
            intent.renumber()

        missing = intent.missing()
        problems = self._validate(intent)
        if missing or problems:
            if spec_task:
                spec_task.cancel()
            tm.PLANS.labels("needs_input").inc()
            speech = missing_speech(missing) if missing else problems[0]
            return CompileResult(
                "needs_input",
                intent,
                None,
                speech,
                missing=missing,
                notes=notes + problems,
                timings=sw.stages,
                total_ms=sw.elapsed_ms(),
            )

        await report(2, "Asking partners for live options")
        with sw.stage("ground") as st:
            try:
                if spec_task is not None and spec_key == self._ground_key(intent):
                    catalog = await spec_task
                    st["speculative"] = True
                else:
                    if spec_task:
                        spec_task.cancel()
                    graph.clear()
                    catalog = await self._ground(intent, calls, t0=sw.t0, trace=graph)
            except PartnerError as exc:
                tm.PLANS.labels("partner_error").inc()
                return CompileResult(
                    "rejected",
                    intent,
                    None,
                    f"I couldn't reach a travel partner ({exc.partner}) and have no recent data to "
                    "fall back on. Please try again in a moment.",
                    timings=sw.stages,
                    partner_calls=calls.calls,
                    total_ms=sw.elapsed_ms(),
                )
            st["calls"] = len(calls.calls)
            catalog.affinity = dict(self.profile.data.affinity)
            self._resolve_items(intent, catalog)

        findings = scan_untrusted(
            [(q.stay.id, q.stay.name, q.stay.description) for q in catalog.stays]
            + [(r.id, r.name, r.description) for r in catalog.restaurants]
            + [(a.id, a.name, a.description) for a in catalog.attractions]
        )
        if findings:
            tm.GUARD.labels("untrusted_instructions_ignored").inc(len(findings))

        await report(3, "Solving")
        with sw.stage("solve") as st:
            model = TripModel(intent, catalog)
            res = await asyncio.to_thread(
                model.solve, time_limit=self.s.solver_time_limit_s, workers=self.s.solver_workers
            )
            st["status"], st["solverMs"] = res.status, res.wall_ms
        solve_stage = sw.stages[-1]
        graph.append(
            {
                "name": "solve (CP-SAT)",
                "deps": [n["name"] for n in graph if n["name"] != "research"],
                "startMs": solve_stage["startMs"],
                "durationMs": solve_stage["durationMs"],
                "status": "ok",
            }
        )

        conflict: Conflict | None = None
        verdict: Verdict | None = None
        cross: str | None = None
        if res.plan is not None:
            await report(4, "Verifying every rule independently")
            with sw.stage("verify ∥ cross-check") as st:
                plan = res.plan
                checks = Graph().add("verify", lambda _: asyncio.to_thread(verify, intent, catalog, plan))
                if self.s.z3_crosscheck:
                    checks.add(
                        "cross-check (Z3)",
                        lambda _: asyncio.to_thread(z3check.check_plan, intent, catalog, plan),
                    )
                done, nodes = await checks.run(t0=sw.t0)
                graph.extend({**n, "deps": ["solve (CP-SAT)"]} for n in nodes)
                verdict = done["verify"]
                cross = done.get("cross-check (Z3)", "skipped")
                st["checks"], st["z3"] = len(verdict.checks), cross
            if not verdict.ok:  # fail closed: never present an unverified plan
                tm.PLANS.labels("verification_failed").inc()
                failed = "; ".join(f"{c.label}: {c.evidence}" for c in verdict.failures())
                return CompileResult(
                    "rejected",
                    intent,
                    None,
                    f"I found a plan but it failed independent verification, so I won't offer it. ({failed})",
                    timings=sw.stages,
                    partner_calls=calls.calls,
                    total_ms=sw.elapsed_ms(),
                )
            status = "planned"
        elif res.status == "INFEASIBLE":
            await report(4, "Finding the smallest conflict and fixes")
            with sw.stage("diagnose") as st:
                core = (await asyncio.to_thread(model.solve, time_limit=1.0, want_core=True)).core
                mus, calls_mus = await asyncio.to_thread(minimal_core, model, core)
                st["mus"] = len(mus)
            with sw.stage("fixits ∥ z3") as st:
                fix_model = TripModel(intent, catalog)
                z3_task = (
                    asyncio.create_task(asyncio.to_thread(z3check.check_conflict, intent, catalog, mus))
                    if self.s.z3_crosscheck
                    else None
                )
                conflict = await asyncio.to_thread(explain_conflict, fix_model, core, mus=mus)
                conflict.solve_calls += calls_mus
                cross = await z3_task if z3_task else "skipped"
                st["fixits"], st["z3"], st["solveCalls"] = len(conflict.fixits), cross, conflict.solve_calls
            intent.diagnostics.append(conflict_diagnostic(conflict))
            status = "infeasible"
        else:
            tm.PLANS.labels("timeout").inc()
            return CompileResult(
                "rejected",
                intent,
                None,
                "Planning took too long. Try narrowing the request.",
                timings=sw.stages,
                partner_calls=calls.calls,
                total_ms=sw.elapsed_ms(),
            )

        await report(5, "Signing the certificate")
        with sw.stage("certify"):
            cert = self._certificate(intent, catalog, res, verdict, conflict, cross, findings, calls, notes)
            entry = None
            if sign:
                entry = await asyncio.to_thread(
                    self.ledger.append, "plan" if status == "planned" else "infeasibility", cert
                )
                self._save_snapshot(catalog)
            plan_id = sha256_hex(canonical_json(cert))[:12]
            stored = StoredPlan(plan_id, intent, catalog, res.plan, verdict, cert, entry, conflict)
            self.plans[plan_id] = stored

        with sw.stage("speech"):
            if status == "planned" and res.plan is not None and verdict is not None:
                speech = plan_speech(intent, res.plan, len(verdict.checks))
            else:
                assert conflict is not None
                speech = conflict_speech(conflict_diagnostic(conflict).message, conflict.fixits)
            ok, _ = grounded(
                speech,
                orjson.dumps(cert).decode()
                + " "
                + " ".join(
                    str(x)
                    for x in [
                        intent.nights,
                        len(res.plan.meals) if res.plan else 0,
                        len(res.plan.activities) if res.plan else 0,
                        len(verdict.checks) if verdict else 0,
                    ]
                ),
            )
            if not ok:
                tm.GUARD.labels("speech_not_grounded").inc()
            if self.bedrock.available:
                better = await self.bedrock.rephrase(speech, orjson.dumps(cert).decode())
                if better:
                    speech = better

        advice = None
        if status == "planned":
            bad = len(catalog.bad_weather_days())
            if bad and bad * 2 >= intent.nights + 1:
                advice = {
                    "wetDays": bad,
                    "days": intent.nights + 1,
                    "message": f"{bad} of {intent.nights + 1} days look wet. Ask me to check better dates.",
                }

        total = sw.elapsed_ms()
        tm.PLANS.labels(status).inc()
        tm.COMPILE.observe(total / 1000)
        for stg in sw.stages:
            tm.STAGE.labels(stg["name"]).observe(stg["durationMs"] / 1000)
        return CompileResult(
            status,
            intent,
            stored,
            speech,
            notes=notes,
            timings=sw.stages,
            partner_calls=calls.calls,
            total_ms=total,
            weather_advice=advice,
            graph=graph,
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _ground_key(i: TripIntent) -> tuple[Any, ...]:
        return (i.origin, i.destination, i.start_date, i.nights, i.travelers)

    async def _ground(
        self,
        intent: TripIntent,
        calls: CallLog,
        *,
        t0: float | None = None,
        trace: list[dict[str, Any]] | None = None,
    ) -> Catalog:
        """Fetch everything a plan needs as one task graph: seven independent partner calls at once."""
        assert intent.origin and intent.destination and intent.start_date and intent.nights
        end = intent.start_date + timedelta(days=intent.nights)
        p, dest = self.partners, intent.destination
        g = (
            Graph()
            .add("transport.outbound", lambda _: p.transport(intent.origin, dest, intent.start_date))  # type: ignore[arg-type]
            .add("transport.return", lambda _: p.transport(dest, intent.origin, end))  # type: ignore[arg-type]
            .add("stays", lambda _: p.stays(dest, intent.start_date, intent.nights, intent.travelers))  # type: ignore[arg-type]
            .add("dining", lambda _: p.restaurants(dest))
            .add("experiences", lambda _: p.attractions(dest))
            .add(
                "weather",
                lambda _: p.forecast(dest, intent.start_date, intent.nights + 1),  # type: ignore[arg-type]
                optional=True,
            )  # a missing forecast must never block a trip
            .add("research", lambda _: self.highlights(dest), optional=True)
        )
        results, nodes = await g.run(t0=t0)
        if trace is not None:
            trace.extend(nodes)
        names = [
            n
            for n in ("transport.outbound", "transport.return", "stays", "dining", "experiences", "weather")
            if n in results
        ]
        fetched: list[Fetched] = [results[n][1] for n in names]
        for f in fetched:
            calls.add(f)
            tm.PARTNER.labels(f.partner, f.freshness).inc()
        degraded = [
            f"{n}: served from cache because the partner is unavailable"
            for n, f in zip(names, fetched, strict=True)
            if f.freshness == "stale"
        ]
        if "weather" not in results:
            degraded.append("weather: forecast unavailable, planned without it")
        return Catalog(
            outbound=results["transport.outbound"][0],
            inbound=results["transport.return"][0],
            stays=results["stays"][0],
            restaurants=results["dining"][0],
            attractions=results["experiences"][0],
            weather=results["weather"][0] if "weather" in results else [],
            sources={n: f.digest for n, f in zip(names, fetched, strict=True)},
            degraded=degraded,
            highlights=results.get("research") or {},
        )

    async def highlights(self, city: str) -> dict[str, Any]:
        """Real places worth seeing (research partner), cached for a week."""
        f = await self.partners.fetch("guide", "get_highlights", {"city": city})
        return f.data

    async def prefetch(self, intent: TripIntent) -> None:
        """Warm partner caches while the conversation is still collecting details."""
        if intent.origin and intent.destination and intent.start_date and intent.nights:
            with contextlib.suppress(Exception):
                await self._ground(intent, CallLog())

    def _validate(self, intent: TripIntent) -> list[str]:
        problems = []
        if intent.origin and intent.origin == intent.destination:
            problems.append("Your origin and destination are the same city.")
        if intent.start_date and intent.start_date <= world.today():
            problems.append("That date has already started or passed. When would you like to leave?")
        if intent.nights and intent.nights > MAX_NIGHTS:
            problems.append(f"Plexis plans trips of up to {MAX_NIGHTS} nights.")
        return problems

    def _apply_overrides(self, intent: TripIntent, o: dict[str, Any]) -> None:
        def city(v: Any) -> str | None:
            if not v:
                return None
            v = str(v).strip()
            return v.upper() if v.upper() in world.CITIES else world.CITY_ALIASES.get(v.lower())

        if o.get("origin"):
            intent.origin = city(o["origin"]) or intent.origin
        if o.get("destination"):
            intent.destination = city(o["destination"]) or intent.destination
        if o.get("start_date"):
            try:
                intent.start_date = date.fromisoformat(str(o["start_date"]))
            except ValueError:
                sub = rules.parse(f"on {o['start_date']}")
                intent.start_date = sub.start_date or intent.start_date
        if o.get("nights"):
            intent.nights = int(o["nights"])
        if o.get("travelers"):
            intent.travelers = int(o["travelers"])

    def _merge_llm(
        self, intent: TripIntent, kept: list[dict[str, Any]], dropped: list[str], unsupported: list[str]
    ) -> None:
        for p in kept:
            cls = CONSTRAINT_KINDS.get(p["kind"])
            if cls is None:
                continue
            try:
                c = cls(**p.get("params", {}))
            except Exception:
                intent.diagnostics.append(
                    Diagnostic(
                        severity="warning",
                        code="QED-W010",
                        message=f"Discarded an invalid {p['kind']} proposal from Bedrock.",
                    )
                )
                continue
            low = intent.utterance.lower()
            start = low.find(p["quote"].lower())
            from qed.ir import Span

            c.span = Span(
                start=start,
                end=start + len(p["quote"]),
                text=intent.utterance[start : start + len(p["quote"])],
            )
            intent.constraints.append(c)
            self.learner.learn_from_model(p["quote"], c)  # next time the phrasebook handles it, no model call
            intent.diagnostics = [
                d
                for d in intent.diagnostics
                if not (d.code == "QED-W001" and d.span and d.span.text.lower() in p["quote"].lower())
            ]
        for d in dropped:
            intent.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    code="QED-W011",
                    message=f"Ignored an LLM proposal without supporting words: {d}",
                )
            )
        for u in unsupported:
            intent.diagnostics.append(
                Diagnostic(
                    severity="warning",
                    code="QED-W012",
                    message=f"'{u}' can't be expressed as a checkable rule yet.",
                )
            )

    def _resolve_items(self, intent: TripIntent, catalog: Catalog) -> None:
        names: dict[str, str] = {}
        for q in catalog.stays:
            names[q.stay.name.lower()] = q.stay.id
        for r in catalog.restaurants:
            names[r.name.lower()] = r.id
        for a in catalog.attractions:
            names[a.name.lower()] = a.id
        for c in intent.constraints:
            if c.kind in ("include_item", "exclude_item") and not c.item_id:
                key = c.name.lower().strip()
                hit = names.get(key) or next((v for k, v in names.items() if key in k or k in key), None)
                if not hit:
                    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.6)
                    hit = names[close[0]] if close else None
                if hit:
                    c.item_id = hit
                else:
                    intent.diagnostics.append(
                        Diagnostic(
                            severity="warning",
                            code="QED-W020",
                            constraint_ids=[c.id],
                            message=f"No partner offers '{c.name}' in {world.city_name(intent.destination or 'SEA')}.",
                        )
                    )

    def _certificate(
        self,
        intent: TripIntent,
        catalog: Catalog,
        res: Any,
        verdict: Verdict | None,
        conflict: Conflict | None,
        cross: str | None,
        findings: list[dict[str, str]],
        calls: CallLog,
        notes: list[str],
    ) -> dict[str, Any]:
        from qed.guard import redact

        cons = []
        for c in intent.constraints:
            d = c.model_dump(mode="json", exclude={"span"})
            cons.append({**d, "text": c.describe(), "words": c.span.text if c.span else None})
        cert: dict[str, Any] = {
            "qed": "1.0",
            "kind": "plan" if res.plan is not None else "infeasibility",
            "issuedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": {
                "utterance": redact(intent.utterance),
                "utteranceSha256": sha256_hex(intent.utterance),
                "parser": intent.parser,
                "coverage": intent.coverage,
            },
            "intent": {
                "origin": intent.origin,
                "destination": intent.destination,
                "startDate": intent.start_date.isoformat() if intent.start_date else None,
                "nights": intent.nights,
                "travelers": intent.travelers,
                "objective": intent.objective,
                "travelerNames": intent.traveler_names,
                "constraints": cons,
            },
            "solver": {
                "engine": f"OR-Tools CP-SAT {ORTOOLS_VERSION}",
                "status": res.status,
                "wallMs": res.wall_ms,
                "optimalityGapPct": res.values.get("gapPct"),
            },
            "crossCheck": {
                "engine": "arithmetic cost floor"
                if cross and "cost floor" in cross
                else f"Z3 {z3.get_version_string()}",
                "result": cross,
            },
            "data": {
                "sources": catalog.sources,
                "snapshot": digest(catalog.snapshot()),
                "degraded": catalog.degraded,
            },
            "untrustedText": {
                "findings": findings,
                "policy": "partner free text is never read by planning logic",
            },
            "notes": notes,
        }
        if res.plan is not None and verdict is not None:
            p: Plan = res.plan
            cert["plan"] = {
                "outbound": _tx(p.outbound),
                "return": _tx(p.inbound),
                "stay": {
                    "id": p.stay.stay.id,
                    "name": p.stay.stay.name,
                    "rating": p.stay.stay.rating,
                    "kind": p.stay.stay.kind,
                    "accessible": p.stay.stay.accessible,
                    "nights": p.stay.nights,
                    "units": p.stay.units,
                    "nightly": p.stay.nightly,
                    "total": p.stay.total_price,
                },
                "meals": [
                    {
                        "slot": m.slot,
                        "day": m.day,
                        "date": m.date.isoformat(),
                        "meal": m.meal,
                        "id": m.restaurant.id,
                        "name": m.restaurant.name,
                        "cuisines": m.restaurant.cuisines,
                        "diets": m.restaurant.diets,
                        "costPerPerson": m.restaurant.avg_cost,
                    }
                    for m in p.meals
                ],
                "activities": [
                    {
                        "day": a.day,
                        "date": a.date.isoformat(),
                        "id": a.attraction.id,
                        "name": a.attraction.name,
                        "category": a.attraction.category,
                        "indoor": a.attraction.indoor,
                        "pricePerPerson": a.attraction.price,
                        "minutes": a.attraction.minutes,
                    }
                    for a in p.activities
                ],
                "breakdown": p.breakdown,
            }
            cert["checks"] = [c.as_dict() for c in verdict.checks]
        if conflict is not None:
            cert["conflict"] = {
                "mus": conflict.mus,
                "rules": [conflict.texts[g] for g in conflict.mus],
                "origins": conflict.origins,
                "fixits": [f.model_dump() for f in conflict.fixits],
            }
        return cert

    def _save_snapshot(self, catalog: Catalog) -> None:
        path = self.state / "snapshots" / f"{digest(catalog.snapshot())}.json"
        if not path.exists():
            path.write_bytes(orjson.dumps(catalog.snapshot()))

    def get(self, plan_id: str) -> StoredPlan:
        sp = self.plans.get(plan_id)
        if sp is None:
            raise KeyError(f"unknown plan {plan_id}; plan a trip first")
        return sp

    # ------------------------------------------------------------------ refine & fix

    async def refine(self, plan_id: str, change: str, **kw: Any) -> CompileResult:
        base = self.get(plan_id).intent
        return await self.compile(
            f"{base.utterance} | {change}", intent=apply_refinement(base, change, self.profile.people), **kw
        )

    async def apply_fix(self, plan_id: str, fix_id: str, **kw: Any) -> CompileResult:
        sp = self.get(plan_id)
        fix = next((f for f in (sp.conflict.fixits if sp.conflict else []) if f.id == fix_id), None)
        if fix is None:
            raise KeyError(f"unknown fix {fix_id}")
        new = sp.intent.model_copy(deep=True)
        new.diagnostics = [d for d in new.diagnostics if d.code != "QED-E100"]
        if fix.action == "drop":
            new.constraints = [c for c in new.constraints if c.id != fix.constraint_id]
        else:
            for c in new.constraints:
                if c.id == fix.constraint_id:
                    for k, v in fix.patch.items():
                        setattr(c, k, v)
        new.utterance = f"{sp.intent.utterance} | fix: {fix.message}"
        return await self.compile(new.utterance, intent=new, use_profile=False, **kw)

    # ------------------------------------------------------------------ explanations

    async def why(self, plan_id: str, question: str) -> dict[str, Any]:
        sp = self.get(plan_id)
        if sp.plan is None:
            return {"answer": "There's no plan yet; the request was infeasible.", "kind": "none"}
        cat, plan = sp.catalog, sp.plan
        items: dict[str, tuple[str, str]] = {}
        for q in cat.stays:
            items[q.stay.name.lower()] = (q.stay.id, q.stay.name)
        for r in cat.restaurants:
            items[r.name.lower()] = (r.id, r.name)
        for a in cat.attractions:
            items[a.name.lower()] = (a.id, a.name)
        for t in cat.outbound + cat.inbound:
            items[t.code.lower()] = (t.id, f"{t.carrier} {t.code}")
        low = question.lower()
        hit = next((v for k, v in sorted(items.items(), key=lambda kv: -len(kv[0])) if k in low), None)
        if hit is None:
            words = re.findall(r"[a-z']+", low)
            for n in range(4, 0, -1):
                for i in range(len(words) - n + 1):
                    close = difflib.get_close_matches(
                        " ".join(words[i : i + n]), list(items), n=1, cutoff=0.75
                    )
                    if close:
                        hit = items[close[0]]
                        break
                if hit:
                    break
        if hit is None:
            return {"kind": "unknown", "answer": "Which hotel, restaurant, activity or flight do you mean?"}
        item_id, name = hit
        if item_id in plan.item_ids():
            reasons = [
                c.label
                for c in (sp.verdict.checks if sp.verdict else [])
                if c.passed and c.origin in ("user", "profile") and (name.lower() in c.evidence.lower())
            ]
            alt = await asyncio.to_thread(_runner_up, sp, item_id)
            answer = f"{name} is in your plan. It meets: " + (
                "; ".join(reasons) if reasons else "all your rules"
            )
            answer += f". {alt}" if alt else "."
            return {"kind": "why", "itemId": item_id, "answer": answer}
        wn = await asyncio.to_thread(why_not, sp.intent, cat, item_id, plan.total, name)
        return {
            "kind": "why_not",
            "itemId": item_id,
            "feasible": wn.feasible,
            "blocking": wn.blocking,
            "costDelta": wn.cost_delta,
            "answer": wn.message,
        }

    # ------------------------------------------------------------------ weather

    async def better_dates(self, plan_id: str) -> dict[str, Any]:
        sp = self.get(plan_id)
        it = sp.intent
        assert it.start_date and it.nights and it.destination
        days = it.nights + 1
        current_bad = len(sp.catalog.bad_weather_days())
        earliest = world.today() + timedelta(days=1)
        offsets = [
            o for o in (-4, -3, -2, -1, 1, 2, 3, 4, 5, 6, 7) if it.start_date + timedelta(days=o) >= earliest
        ]
        forecasts = await asyncio.gather(
            *(
                self.partners.forecast(it.destination, it.start_date + timedelta(days=o), days)
                for o in offsets
            )
        )
        scored = sorted(
            (
                (sum(1 for w in f[0] if w.condition in ("rain", "storm", "snow")), abs(o), o)
                for o, f in zip(offsets, forecasts, strict=True)
            )
        )
        options = []
        for bad, _, off in scored[:3]:
            if bad >= current_bad:
                continue
            alt = it.model_copy(deep=True)
            alt.start_date = it.start_date + timedelta(days=off)
            alt.diagnostics = []
            res = await self.compile(
                f"{it.utterance} | shifted {off:+d} days", intent=alt, sign=False, use_profile=False
            )
            if res.status == "planned" and res.stored and res.stored.plan:
                total = res.stored.plan.total
                options.append(
                    {
                        "start": alt.start_date.isoformat(),
                        "shiftDays": off,
                        "wetDays": bad,
                        "dryDays": days - bad,
                        "total": total,
                        "delta": total - (sp.plan.total if sp.plan else total),
                        "change": f"leave on {alt.start_date.strftime('%B')} {alt.start_date.day}",
                    }
                )
            if len(options) == 2:
                break
        msg = f"Your dates have {current_bad} wet day(s) of {days}. " + (
            "; ".join(
                f"Leaving {o['start']} gives {o['dryDays']} dry days for {fmt_money(o['total'])} "
                f"({'+' if o['delta'] >= 0 else '-'}{fmt_money(abs(o['delta']))})"
                for o in options
            )
            if options
            else "Nearby dates don't look drier, so I'd keep your plan."
        )
        return {"currentWetDays": current_bad, "days": days, "options": options, "message": msg}

    # ------------------------------------------------------------------ booking saga

    async def book(self, plan_id: str) -> dict[str, Any]:
        sp = self.get(plan_id)
        if sp.plan is None or sp.entry is None:
            return {"status": "rejected", "message": "Only a verified, signed plan can be booked."}
        idem = f"book:{sp.entry.cert_hash}"
        existing = next(
            (t for t in self.trips.values() if t.plan_id == plan_id and t.status in ("booked", "amended")),
            None,
        )
        if existing:
            entry = self.ledger.get(existing.receipt_index)
            return {
                "status": "already_booked",
                "tripId": existing.trip_id,
                "receipt": entry.public() if entry else None,
                "message": "This plan is already booked; I didn't charge you twice.",
            }
        plan = sp.plan
        if plan.total > self.s.max_booking_usd:
            return {
                "status": "rejected",
                "message": f"Bookings above {fmt_money(self.s.max_booking_usd)} need a human "
                "agent by policy.",
            }
        steps: list[dict[str, Any]] = []

        # Pre-commit revalidation: prices must still match what was certified.
        t0 = time.perf_counter()
        it = sp.intent
        assert it.start_date and it.nights and it.destination and it.origin
        fresh_out, _ = await self.partners.transport(it.origin, it.destination, it.start_date)
        fresh_in, _ = await self.partners.transport(
            it.destination, it.origin, it.start_date + timedelta(days=it.nights)
        )
        fresh = {t.id: t for t in fresh_out + fresh_in}
        drift = [
            t for t in (plan.outbound, plan.inbound) if t.id not in fresh or fresh[t.id].price != t.price
        ]
        steps.append(
            {
                "step": "revalidate",
                "status": "ok" if not drift else "changed",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        )
        if drift:
            return {
                "status": "repriced",
                "steps": steps,
                "message": "Prices or availability changed since I verified this plan, so I didn't book. "
                "Ask me to re-plan and I'll certify the new numbers.",
            }

        holds: list[dict[str, Any]] = []
        auth_id: str | None = None
        key = sp.entry.cert_hash[:16]

        async def hold(partner: str, tool: str, args: dict[str, Any], label: str) -> None:
            t1 = time.perf_counter()
            rec = await self.partners.write(partner, tool, {**args, "idempotency_key": f"{key}:{label}"})
            holds.append({"partner": partner, "holdId": rec["holdId"], "item": label})
            steps.append(
                {
                    "step": "hold",
                    "partner": partner,
                    "item": label,
                    "status": "held",
                    "ms": round((time.perf_counter() - t1) * 1000, 1),
                }
            )

        try:
            await asyncio.gather(
                hold(
                    "transport",
                    "hold_transport",
                    {"option_id": plan.outbound.id, "travelers": plan.travelers},
                    plan.outbound.code,
                ),
                hold(
                    "transport",
                    "hold_transport",
                    {"option_id": plan.inbound.id, "travelers": plan.travelers},
                    plan.inbound.code,
                ),
                hold(
                    "stays",
                    "hold_stay",
                    {
                        "stay_id": plan.stay.stay.id,
                        "check_in": plan.stay.check_in.isoformat(),
                        "nights": plan.stay.nights,
                    },
                    plan.stay.stay.name,
                ),
                *(
                    hold(
                        "dining",
                        "hold_table",
                        {
                            "restaurant_id": m.restaurant.id,
                            "date_iso": m.date.isoformat(),
                            "meal": m.meal,
                            "party": plan.travelers,
                        },
                        f"{m.slot} {m.restaurant.name}",
                    )
                    for m in plan.meals
                ),
                *(
                    hold(
                        "experiences",
                        "hold_tickets",
                        {
                            "attraction_id": a.attraction.id,
                            "date_iso": a.date.isoformat(),
                            "party": plan.travelers,
                        },
                        a.attraction.name,
                    )
                    for a in plan.activities
                ),
            )
            auth = await self.partners.write(
                "wallet", "authorize", {"amount": plan.total, "idempotency_key": key}
            )
            auth_id = auth["authId"]
            steps.append(
                {"step": "authorize", "partner": "wallet", "status": "authorized", "amount": plan.total}
            )
            for h in holds:
                await self.partners.write(h["partner"], "confirm_hold", {"hold_id": h["holdId"]})
                steps.append(
                    {"step": "confirm", "partner": h["partner"], "item": h["item"], "status": "confirmed"}
                )
            await self.partners.write("wallet", "capture", {"auth_id": auth_id})
            steps.append({"step": "capture", "partner": "wallet", "status": "captured"})
        except Exception as exc:
            steps.append({"step": "failure", "status": "error", "detail": str(exc)[:160]})
            for h in reversed(holds):
                try:
                    await self.partners.write(h["partner"], "release_hold", {"hold_id": h["holdId"]})
                    steps.append(
                        {
                            "step": "compensate",
                            "partner": h["partner"],
                            "item": h["item"],
                            "status": "released",
                        }
                    )
                except Exception as rel_exc:
                    steps.append(
                        {"step": "compensate", "partner": h["partner"], "status": f"failed: {rel_exc}"[:80]}
                    )
            if auth_id:
                try:
                    await self.partners.write("wallet", "void", {"auth_id": auth_id})
                    steps.append({"step": "compensate", "partner": "wallet", "status": "voided"})
                except Exception:  # noqa: S110
                    pass
            tm.BOOKINGS.labels("rolled_back").inc()
            return {
                "status": "rolled_back",
                "steps": steps,
                "message": "A partner failed mid-booking, so I undid everything: no holds remain and you were "
                "not charged.",
            }

        receipt = {
            "qed": "1.0",
            "kind": "booking",
            "planCertificate": sp.entry.cert_hash,
            "planLedgerIndex": sp.entry.index,
            "total": plan.total,
            "travelers": plan.travelers,
            "holds": holds,
            "payment": {"authId": auth_id, "status": "captured"},
            "issuedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        entry = await asyncio.to_thread(self.ledger.append, "booking", receipt, idem_key=idem)
        trip = Trip(
            trip_id=f"T-{uuid.uuid4().hex[:8]}",
            plan_id=plan_id,
            receipt_index=entry.index,
            holds=holds,
            auth_id=auth_id,
        )
        self.trips[trip.trip_id] = trip
        self.profile.learn_from_booking(
            [a.attraction.category for a in plan.activities],
            [c for m in plan.meals for c in m.restaurant.cuisines],
        )
        tm.BOOKINGS.labels("booked").inc()
        return {
            "status": "booked",
            "tripId": trip.trip_id,
            "receipt": entry.public(),
            "steps": steps,
            "message": f"Booked. {fmt_money(plan.total)} charged once; receipt #{entry.index} is signed and "
            "chained in the ledger. I'll watch your flights and tell you if anything changes.",
        }

    # ------------------------------------------------------------------ receipts

    def verify_receipt(self, index: int) -> dict[str, Any]:
        e = self.ledger.get(index)
        if e is None:
            return {"ok": False, "problems": [f"no ledger entry {index}"]}
        problems = []
        if sha256_hex(canonical_json(e.body)) != e.cert_hash:
            problems.append("certificate hash mismatch")
        if not verify_signature(self.signer.public_b64, e.entry_hash.encode(), e.signature):
            problems.append("signature invalid")
        prev = self.ledger.get(index - 1) if index > 0 else None
        if prev is not None and prev.entry_hash != e.prev_hash:
            problems.append("chain link broken")
        rechecked = None
        if e.kind == "plan":
            snap_path = self.state / "snapshots" / f"{e.body['data']['snapshot']}.json"
            if snap_path.exists():
                rechecked = _recheck_offline(
                    e.body, Catalog.from_snapshot(orjson.loads(snap_path.read_bytes()))
                )
                if rechecked and not rechecked["ok"]:
                    problems.append("offline re-verification failed")
        return {
            "ok": not problems,
            "entry": e.public(),
            "kind": e.kind,
            "problems": problems,
            "publicKey": self.signer.public_b64,
            "keyId": self.signer.key_id,
            "offlineRecheck": rechecked,
        }

    # ------------------------------------------------------------------ brief & calls

    async def brief(self, plan_id: str) -> dict[str, Any]:
        sp = self.get(plan_id)
        if sp.plan is None:
            raise KeyError("no plan to brief")
        it = sp.intent
        assert it.destination and it.start_date
        advisory = world.advisory(it.destination, it.start_date.month)
        people = ["you", *it.traveler_names]
        if re.search(r"\b(?:dog|pet)\b", it.utterance.lower()):
            people.append("dog")
        return build_brief(
            sp.plan, sp.catalog.weather, advisory, travelers=people, city_name=world.city_name(it.destination)
        )

    async def call_property(self, plan_id: str, questions: list[str]) -> dict[str, Any]:
        sp = self.get(plan_id)
        if sp.plan is None:
            raise KeyError("no plan")
        f = await self.partners.fetch(
            "frontdesk",
            "call_property",
            {
                "stay_id": sp.plan.stay.stay.id,
                "questions": questions[:5],
                "on_behalf_of": "a guest planning a stay",
            },
            read=False,
        )
        data = dict(f.data)
        data["provenance"] = "phone call (simulated); answers are unverified until confirmed in writing"
        return data

    # ------------------------------------------------------------------ disruption monitor

    async def check_disruptions(self) -> list[dict[str, Any]]:
        new_alerts = []
        for trip in list(self.trips.values()):
            if trip.status not in ("booked", "amended"):
                continue
            sp = self.plans.get(trip.plan_id)
            if sp is None or sp.plan is None:
                continue
            legs = {sp.plan.outbound.id: "outbound", sp.plan.inbound.id: "return"}
            try:
                statuses = await self.partners.statuses(list(legs))
            except Exception:  # noqa: S112
                continue
            for st in statuses:
                if st.status != "cancelled" or any(a["optionId"] == st.option_id for a in trip.alerts):
                    continue
                tm.DISRUPTIONS.labels("cancellation").inc()
                alert = await self._replan_after_cancellation(trip, sp, st.option_id, legs[st.option_id])
                trip.alerts.append(alert)
                new_alerts.append(alert)
                for listener in self.alert_listeners:
                    try:
                        await listener(trip, alert)
                    except Exception:  # noqa: S110
                        pass
        return new_alerts

    async def _replan_after_cancellation(
        self, trip: Trip, sp: StoredPlan, option_id: str, leg: str
    ) -> dict[str, Any]:
        it = sp.intent.model_copy(deep=True)
        it.diagnostics = []
        cancelled = sp.plan.outbound if leg == "outbound" else sp.plan.inbound  # type: ignore[union-attr]
        it.constraints.append(
            ExcludeItem(
                name=f"{cancelled.carrier} {cancelled.code}",
                item_id=option_id,
                origin="policy",
                because="This option was cancelled by the carrier.",
            )
        )
        keep = sp.plan.stay.stay  # type: ignore[union-attr]
        it.constraints.append(
            IncludeItem(
                name=keep.name,
                item_id=keep.id,
                origin="policy",
                because="Keep the lodging you already booked.",
            )
        )
        res = await self.compile(
            f"{sp.intent.utterance} | replan: {cancelled.code} cancelled", intent=it, use_profile=False
        )
        base = {
            "tripId": trip.trip_id,
            "optionId": option_id,
            "leg": leg,
            "kind": "cancellation",
            "detectedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "cancelled": f"{cancelled.carrier} {cancelled.code} at {fmt_minutes(cancelled.depart_min)}",
        }
        if res.status == "planned" and res.stored and res.stored.plan:
            new = res.stored.plan.outbound if leg == "outbound" else res.stored.plan.inbound
            delta = res.stored.plan.total - sp.plan.total  # type: ignore[union-attr]
            return {
                **base,
                "proposalPlanId": res.stored.plan_id,
                "replacement": f"{new.carrier} {new.code} at {fmt_minutes(new.depart_min)}",
                "costDelta": delta,
                "message": f"Heads up: your {leg} {cancelled.carrier} {cancelled.code} was cancelled. I found "
                f"{new.carrier} {new.code} at {fmt_minutes(new.depart_min)} that keeps your hotel and "
                f"every rule, {_delta_words(delta)}. Want me to rebook?",
            }
        return {
            **base,
            "proposalPlanId": None,
            "message": f"Your {leg} {cancelled.code} was cancelled and no replacement keeps all your rules. "
            "Ask me to re-plan with different dates.",
        }


def _delta_words(delta: int) -> str:
    if delta > 0:
        return f"for {fmt_money(delta)} more"
    if delta < 0:
        return f"and it saves {fmt_money(-delta)}"
    return "at the same price"


def _tx(t: Any) -> dict[str, Any]:
    return {
        "id": t.id,
        "mode": t.mode,
        "carrier": t.carrier,
        "code": t.code,
        "date": t.date.isoformat(),
        "depart": fmt_minutes(t.depart_min),
        "arrive": fmt_minutes(t.arrive_min),
        "departMin": t.depart_min,
        "arriveMin": t.arrive_min,
        "stops": t.stops,
        "price": t.price,
        "priceBasis": t.price_basis,
        "redEye": t.red_eye,
    }


def _runner_up(sp: StoredPlan, item_id: str) -> str:
    """How much worse the best plan is without this item (contrastive 'why this')."""
    it = sp.intent.model_copy(deep=True)
    it.constraints.append(ExcludeItem(name="runner-up probe", item_id=item_id))
    it.renumber()
    res = TripModel(it, sp.catalog).solve(time_limit=1.0)
    if res.plan is None or sp.plan is None:
        return "Without it, no plan satisfies everything you asked for."
    delta = res.plan.total - sp.plan.total
    if delta > 0:
        return f"The best plan without it costs {fmt_money(delta)} more."
    return "The best alternative costs about the same but scores lower on ratings, timing or weather."


def _recheck_offline(cert: dict[str, Any], catalog: Catalog) -> dict[str, Any] | None:
    """Rebuild the plan from the signed ids and re-run the independent checker on frozen data."""
    try:
        from qed.solver.plan import ActivityChoice, MealChoice

        p = cert["plan"]
        tx = {t.id: t for t in catalog.outbound + catalog.inbound}
        stays = {q.stay.id: q for q in catalog.stays}
        rest = {r.id: r for r in catalog.restaurants}
        attr = {a.id: a for a in catalog.attractions}
        plan = Plan(
            outbound=tx[p["outbound"]["id"]],
            inbound=tx[p["return"]["id"]],
            stay=stays[p["stay"]["id"]],
            meals=[
                MealChoice(
                    slot=m["slot"],
                    day=m["day"],
                    date=date.fromisoformat(m["date"]),
                    meal=m["meal"],
                    restaurant=rest[m["id"]],
                )
                for m in p["meals"]
            ],
            activities=[
                ActivityChoice(day=a["day"], date=date.fromisoformat(a["date"]), attraction=attr[a["id"]])
                for a in p["activities"]
            ],
            travelers=cert["intent"]["travelers"],
            status="OPTIMAL",
        )
        plan.compute_breakdown()
        intent_d = cert["intent"]
        intent = TripIntent(
            utterance=cert["request"]["utterance"],
            origin=intent_d["origin"],
            destination=intent_d["destination"],
            start_date=date.fromisoformat(intent_d["startDate"]),
            nights=intent_d["nights"],
            travelers=intent_d["travelers"],
            objective=intent_d["objective"],
            constraints=[
                {k: v for k, v in c.items() if k not in ("text", "words")} for c in intent_d["constraints"]
            ],
        )
        v = verify(intent, catalog, plan)
        return {"ok": v.ok, "checks": len(v.checks), "failures": [c.label for c in v.failures()]}
    except Exception as exc:
        return {"ok": False, "checks": 0, "failures": [f"could not rebuild plan: {exc}"[:120]]}


REMOVALS = {
    "budget_max": r"\b(?:no|drop|remove|forget|ignore|without)\s+(?:the\s+)?budget\b|\bbudget doesn'?t matter\b",
    "stay_accessible": r"\b(?:doesn'?t|does not|don'?t|no longer)\s+need\s+(?:to be\s+)?(?:wheelchair\s+)?accessible\b",
    "depart_after": r"\b(?:early flights? (?:are|is) (?:fine|ok|okay)|any departure time)\b",
    "no_red_eye": r"\bred[\s-]?eyes? (?:are|is) (?:fine|ok|okay)\b",
    "max_stops": r"\bstops? (?:are|is) (?:fine|ok|okay)\b|\bconnections? (?:are|is) (?:fine|ok|okay)\b",
    "transport_modes": r"\b(?:flying|flights?) (?:is|are) (?:fine|ok|okay)\b",
    "diet_all": r"\b(?:any|all) food is fine\b",
}


def _key(c: Any) -> tuple[Any, ...]:
    return (
        c.kind,
        getattr(c, "cuisine", None),
        getattr(c, "category", None),
        getattr(c, "diet", None),
        getattr(c, "rule", None),
        getattr(c, "leg", None),
        getattr(c, "name", None),
        getattr(c, "amenity", None),
    )


def apply_refinement(base: TripIntent, change: str, known_people: list[str]) -> TripIntent:
    delta = rules.parse(change, known_people=known_people)
    new = base.model_copy(deep=True)
    new.utterance = f"{base.utterance} | {change}"
    new.diagnostics = [d for d in delta.diagnostics if d.code != "QED-W001"]
    low = change.lower()
    for kind, pat in REMOVALS.items():
        if re.search(pat, low):
            new.constraints = [c for c in new.constraints if c.kind != kind]
    if re.search(r"\b(?:cheaper|less expensive|save (?:some )?money|lower (?:the )?cost)\b", low):
        new.objective = "cheapest"
    elif re.search(r"\b(?:nicer|fancier|upgrade|better (?:hotel|place))\b", low):
        new.objective = "best"
    elif delta.provenance.get("objective"):
        new.objective = delta.objective
    for field_name in ("origin", "destination", "start_date", "nights"):
        if field_name in delta.provenance:
            setattr(new, field_name, getattr(delta, field_name))
    if "travelers" in delta.provenance:
        new.travelers = delta.travelers
    for name in delta.traveler_names:
        if name not in new.traveler_names:
            new.traveler_names.append(name)
            new.travelers += 1
    for c in delta.constraints:
        new.constraints = [x for x in new.constraints if _key(x) != _key(c) or x.origin == "policy"]
        new.constraints.append(c)
    return new.renumber()
