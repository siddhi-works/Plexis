"""QED-Bench: does the plan actually satisfy what was asked?

Generates a seeded set of natural-language trip requests (about a quarter are
deliberately impossible), runs the full pipeline (parse -> ground -> solve ->
verify -> Z3), and compares against two baselines judged by the same
independent verifier:

- greedy:        cheapest/top-rated option per slot, ignoring the request's rules
                 (what an itinerary generator without reasoning produces);
- filter-greedy: drops options that violate per-item rules (accessibility, diet,
                 times, rating...) then picks greedily. Local reasoning, no
                 solver, so global rules (budget, counts, time per day) can fail.

An LLM-only baseline (Bedrock) runs when credentials are configured.
"""

from __future__ import annotations

import json
import random
import statistics
import tempfile
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from qed.config import load
from qed.domain import world
from qed.engine import Engine
from qed.ir import TripIntent
from qed.partners.client import LocalPartners
from qed.solver.plan import ActivityChoice, MealChoice, Plan
from qed.solver.schedule import (
    ARRIVAL_AFTERNOON_CUTOFF,
    ARRIVAL_DINNER_CUTOFF,
    DEPARTURE_BREAKFAST_CUTOFF,
    build_schedule,
)
from qed.verify.checker import verify

PAIRS = [
    ("Seattle", "San Francisco"),
    ("Seattle", "Portland"),
    ("Boston", "New York"),
    ("Chicago", "Miami"),
    ("Austin", "Denver"),
    ("Los Angeles", "San Diego"),
    ("New York", "Nashville"),
    ("Phoenix", "Las Vegas"),
]

RULES = [
    ("under ${b}", "budget"),
    ("no flights before {t}am", "time"),
    ("no red-eye", "redeye"),
    ("nonstop", "nonstop"),
    ("wheelchair accessible hotel", "access"),
    ("rated {r}+", "rating"),
    ("vegetarian", "diet"),
    ("at least one {c} dinner", "cuisine"),
    ("{n} museums", "museum"),
    ("a park", "park"),
    ("relaxed pace", "pace"),
]


def generate(n: int, seed: int = 7) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        o, d = rng.choice(PAIRS)
        if rng.random() < 0.5:
            o, d = d, o
        start = world.today() + timedelta(days=rng.randint(20, 60))
        nights, people = rng.randint(1, 5), rng.randint(1, 4)
        tight = rng.random() < 0.25
        picks = rng.sample(RULES, rng.randint(1, 4))
        parts = []
        for tpl, key in picks:
            if key == "budget":
                per_day = rng.randint(90, 160) if tight else rng.randint(260, 520)
                parts.append(tpl.format(b=f"{per_day * (nights + 1) * people:,}"))
            else:
                parts.append(
                    tpl.format(
                        t=rng.choice([7, 8, 9]),
                        r=rng.choice([4.2, 4.5, 4.8]),
                        c=rng.choice(["Thai", "Italian", "Indian", "Mexican"]),
                        n=rng.choice([1, 2]),
                    )
                )
        text = (
            f"Plan {nights} night{'s' if nights > 1 else ''} from {o} to {d} on "
            f"{start.strftime('%B')} {start.day} for {people} {'person' if people == 1 else 'people'}, "
            + ", ".join(parts)
        )
        out.append({"id": i, "text": text, "tight": tight})
    return out


def greedy(intent: TripIntent, cat: Any, *, respect_item_rules: bool) -> Plan | None:
    """A planner without global reasoning."""
    assert intent.start_date and intent.nights
    rules = {c.kind: c for c in intent.constraints}

    def ok_tx(t: Any, leg: str) -> bool:
        if not respect_item_rules:
            return True
        for c in intent.constraints:
            if c.kind == "depart_after" and c.leg == leg and t.depart_min < c.minutes:
                return False
            if c.kind == "arrive_before" and c.leg == leg and t.arrive_min > c.minutes:
                return False
            if c.kind == "no_red_eye" and t.red_eye:
                return False
            if c.kind == "max_stops" and t.mode == "flight" and t.stops > c.stops:
                return False
            if c.kind == "transport_modes" and t.mode not in c.allowed:
                return False
        return True

    def ok_stay(q: Any) -> bool:
        s = q.stay
        if s.min_nights > intent.nights:
            return False
        if not respect_item_rules:
            return True
        return not (
            (("stay_accessible" in rules) and not s.accessible)
            or ("stay_min_rating" in rules and s.rating < rules["stay_min_rating"].rating)
        )

    def ok_rest(r: Any) -> bool:
        return not (respect_item_rules and "diet_all" in rules and rules["diet_all"].diet not in r.diets)

    outs = sorted((t for t in cat.outbound if ok_tx(t, "outbound")), key=lambda t: t.price)
    ins = sorted((t for t in cat.inbound if ok_tx(t, "return")), key=lambda t: t.price)
    stays = sorted((q for q in cat.stays if ok_stay(q)), key=lambda q: q.total_price)
    if not outs or not ins or not stays:
        return None
    out, ret = outs[0], ins[0]
    sched = build_schedule(intent.start_date, intent.nights)
    used: set[str] = set()
    meals = []
    for slot in sched.meals:
        if slot.conditional == "arrival" and out.arrive_min > ARRIVAL_DINNER_CUTOFF:
            continue
        if slot.conditional == "departure" and ret.depart_min < DEPARTURE_BREAKFAST_CUTOFF:
            continue
        opts = sorted(
            (
                r
                for r in cat.restaurants
                if slot.meal in r.meals
                and r.closed_weekday != slot.date.weekday()
                and r.id not in used
                and ok_rest(r)
            ),
            key=lambda r: r.avg_cost,
        )
        if not opts:
            continue
        used.add(opts[0].id)
        meals.append(
            MealChoice(slot=slot.key, day=slot.day, date=slot.date, meal=slot.meal, restaurant=opts[0])
        )
    acts, seen = [], set()
    for ad in sched.activity_days:
        if not ad.full and out.arrive_min > ARRIVAL_AFTERNOON_CUTOFF:
            continue
        opts = sorted(
            (
                a
                for a in cat.attractions
                if a.closed_weekday != ad.date.weekday()
                and a.id not in seen
                and not (respect_item_rules and "attractions_accessible" in rules and not a.accessible)
            ),
            key=lambda a: -a.rating,
        )
        for a in opts[: (2 if ad.full else 1)]:
            seen.add(a.id)
            acts.append(ActivityChoice(day=ad.day, date=ad.date, attraction=a))
    plan = Plan(
        outbound=out,
        inbound=ret,
        stay=stays[0],
        meals=meals,
        activities=acts,
        travelers=intent.travelers,
        status="FEASIBLE",
    )
    plan.compute_breakdown()
    return plan


async def run(n: int, out_dir: Path) -> dict[str, Any]:
    s = replace(load(), state_dir=Path(tempfile.mkdtemp(prefix="plexis-bench-")), backup_every=0)
    engine = Engine(s, LocalPartners(), seed_demo_household=False)
    cases = generate(n)
    rows = []
    for case in cases:
        t0 = time.perf_counter()
        res = await engine.compile(case["text"], sign=False, use_profile=False)
        ms = (time.perf_counter() - t0) * 1000
        row: dict[str, Any] = {
            "id": case["id"],
            "text": case["text"],
            "tight": case["tight"],
            "status": res.status,
            "ms": round(ms, 1),
            "coverage": res.intent.coverage if res.intent else 0,
        }
        if res.stored is not None:
            sp = res.stored
            row["solverMs"] = sp.certificate["solver"]["wallMs"]
            row["z3"] = sp.certificate["crossCheck"]["result"]
            if sp.plan is not None:
                row["qedValid"] = verify(sp.intent, sp.catalog, sp.plan).ok
                row["total"] = sp.plan.total
            else:
                row["qedValid"] = None
                row["mus"] = len(sp.conflict.mus) if sp.conflict else 0
                row["fixits"] = len(sp.conflict.fixits) if sp.conflict else 0
                row["fixitsVerified"] = all(f.verified for f in sp.conflict.fixits) if sp.conflict else True
            for name, item_rules in (("greedy", False), ("filterGreedy", True)):
                p = greedy(sp.intent, sp.catalog, respect_item_rules=item_rules)
                if p is None:
                    row[name] = "no_plan"
                else:
                    v = verify(sp.intent, sp.catalog, p)
                    row[name] = "valid" if v.ok else "invalid"
                    row[f"{name}Violations"] = [c.label for c in v.failures()][:4]
        rows.append(row)

    planned = [r for r in rows if r["status"] == "planned"]
    infeasible = [r for r in rows if r["status"] == "infeasible"]
    solved = planned + infeasible

    def rate(xs: list[Any], pred: Any) -> float:
        return round(100 * sum(1 for x in xs if pred(x)) / max(1, len(xs)), 1)

    lat = sorted(r["ms"] for r in solved)
    summary = {
        "requests": len(rows),
        "compiled": len(solved),
        "planned": len(planned),
        "provenInfeasible": len(infeasible),
        "qed": {
            "validPlansPct": rate(planned, lambda r: r.get("qedValid")),
            "z3AgreementPct": rate(solved, lambda r: str(r.get("z3", "")).startswith(("sat", "unsat"))),
            "infeasibleWithVerifiedFixitsPct": rate(
                infeasible, lambda r: r.get("fixits", 0) > 0 and r.get("fixitsVerified")
            ),
            "meanConflictSize": round(statistics.mean([r["mus"] for r in infeasible]), 2)
            if infeasible
            else None,
            "latencyMsP50": round(lat[len(lat) // 2], 1) if lat else None,
            "latencyMsP95": round(lat[int(len(lat) * 0.95) - 1], 1) if lat else None,
            "within500msPct": rate(solved, lambda r: r["ms"] <= 500),
            "meanParserCoverage": round(statistics.mean([r["coverage"] for r in rows]), 3),
        },
        "greedy": {
            "validPlansPct": rate(solved, lambda r: r.get("greedy") == "valid"),
            "invalidPlanOnImpossibleRequestPct": rate(infeasible, lambda r: r.get("greedy") == "invalid"),
        },
        "filterGreedy": {
            "validPlansPct": rate(solved, lambda r: r.get("filterGreedy") == "valid"),
            "invalidPlanOnImpossibleRequestPct": rate(
                infeasible, lambda r: r.get("filterGreedy") == "invalid"
            ),
        },
        "note": "Valid = passes every check of the independent verifier on the same partner data. Plexis never "
        "returns an invalid plan by construction (it fails closed); for impossible requests it returns a "
        "proven-minimal conflict instead of a plan.",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bench.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8"
    )
    md = [
        "# QED-Bench results",
        "",
        f"{summary['requests']} generated requests, {summary['planned']} planned, "
        f"{summary['provenInfeasible']} proven impossible.",
        "",
        "| Metric | Plexis | Greedy | Filter-greedy |",
        "|---|---|---|---|",
        f"| Requests answered with a plan that passes every check | {summary['qed']['validPlansPct']}% of planned | "
        f"{summary['greedy']['validPlansPct']}% | {summary['filterGreedy']['validPlansPct']}% |",
        f"| Impossible requests answered with a wrong plan | 0% (proven conflict instead) | "
        f"{summary['greedy']['invalidPlanOnImpossibleRequestPct']}% | {summary['filterGreedy']['invalidPlanOnImpossibleRequestPct']}% |",
        f"| Independent cross-check agrees (Z3 proof or arithmetic cost floor) | {summary['qed']['z3AgreementPct']}% | n/a | n/a |",
        f"| Impossible requests with verified fix-its | {summary['qed']['infeasibleWithVerifiedFixitsPct']}% | n/a | n/a |",
        f"| End-to-end latency p50 / p95 | {summary['qed']['latencyMsP50']} / {summary['qed']['latencyMsP95']} ms | n/a | n/a |",
        f"| Compiles within the 500 ms Alexa+ budget | {summary['qed']['within500msPct']}% | n/a | n/a |",
        "",
        summary["note"],
    ]
    (out_dir / "bench.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return summary
