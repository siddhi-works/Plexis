"""The compiler driver end to end with in-process partners: profile, refine, saga, disruptions."""

from __future__ import annotations

import time

import pytest

from qed.engine import Engine
from tests.conftest import WHEN

pytestmark = pytest.mark.asyncio

FAMILY = f"Plan a 3 night trip to San Francisco on {WHEN} with Grandma and Maya under $3,500, a museum"


async def test_profile_facts_become_labelled_constraints(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    assert res.status == "planned"
    origins = {(c.kind, c.origin) for c in res.intent.constraints}
    assert ("stay_accessible", "profile") in origins and ("diet_all", "profile") in origins
    assert res.intent.origin == "SEA"  # saved home city
    plan = res.stored.plan
    assert plan.stay.stay.accessible and all("vegetarian" in m.restaurant.diets for m in plan.meals)
    assert res.stored.certificate["crossCheck"]["result"] == "sat"


async def test_anonymous_planning_ignores_the_profile(engine: Engine) -> None:
    res = await engine.compile(
        FAMILY.replace("to San Francisco", "from Seattle to San Francisco"), use_profile=False
    )
    assert all(c.origin != "profile" for c in res.intent.constraints)


async def test_infeasible_request_gets_verified_fixits_then_a_plan(engine: Engine) -> None:
    res = await engine.compile(f"3 nights from Seattle to San Francisco on {WHEN} for 2 under $700")
    assert res.status == "infeasible"
    conflict = res.stored.conflict
    assert conflict.fixits and all(f.verified for f in conflict.fixits)
    fixed = await engine.apply_fix(res.stored.plan_id, conflict.fixits[0].id)
    assert fixed.status == "planned"


async def test_refinement_replaces_rules_of_the_same_kind(engine: Engine) -> None:
    res = await engine.compile(f"3 nights from Seattle to Portland on {WHEN} under $3,000", use_profile=False)
    ref = await engine.refine(res.stored.plan_id, "budget $2,000 and make it cheaper", use_profile=False)
    budgets = [c for c in ref.intent.constraints if c.kind == "budget_max"]
    assert len(budgets) == 1 and budgets[0].amount == 2000
    assert ref.intent.objective == "cheapest"


async def test_booking_is_a_saga_and_idempotent(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    booked = await engine.book(res.stored.plan_id)
    assert booked["status"] == "booked"
    steps = [s["step"] for s in booked["steps"]]
    assert steps[0] == "revalidate" and "authorize" in steps and steps[-1] == "capture"
    again = await engine.book(res.stored.plan_id)
    assert again["status"] == "already_booked"
    assert engine.verify_receipt(booked["receipt"]["index"])["ok"]


async def test_a_failed_confirmation_rolls_everything_back(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    engine.partners.fail_confirm.add("stays")  # type: ignore[attr-defined]
    out = await engine.book(res.stored.plan_id)
    assert out["status"] == "rolled_back"
    statuses = {h["status"] for h in engine.partners.holds.values()}  # type: ignore[attr-defined]
    assert "held" not in statuses, "every hold must be released or never confirmed"
    assert any(s["step"] == "compensate" for s in out["steps"])
    assert not engine.trips


async def test_cancellation_triggers_a_verified_replan(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    booked = await engine.book(res.stored.plan_id)
    out_id = res.stored.plan.outbound.id
    engine.partners.cancelled.add(out_id)  # type: ignore[attr-defined]
    seen = []

    async def listener(trip, alert):  # type: ignore[no-untyped-def]
        seen.append(alert)

    engine.alert_listeners.append(listener)
    alerts = await engine.check_disruptions()
    assert alerts and seen
    proposal = engine.plans[alerts[0]["proposalPlanId"]]
    assert proposal.plan.outbound.id != out_id
    assert proposal.plan.stay.stay.id == res.stored.plan.stay.stay.id  # kept the booked hotel
    assert proposal.verdict.ok
    assert booked["tripId"] == alerts[0]["tripId"]


async def test_offline_recheck_of_a_signed_plan(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    report = engine.verify_receipt(res.stored.entry.index)
    assert report["ok"] and report["offlineRecheck"]["ok"]


async def test_brief_is_general_and_sourced(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    brief = await engine.brief(res.stored.plan_id)
    assert brief["packing"] and brief["days"]
    assert "not medical advice" in brief["comfort"]["disclaimer"]


async def test_front_desk_calls_disclose_ai(engine: Engine) -> None:
    res = await engine.compile(FAMILY)
    call = await engine.call_property(res.stored.plan_id, ["Can we check in early?"])
    assert call["disclosed_ai"] is True
    assert "AI assistant" in call["transcript"][1]["text"]
    assert "unverified" in call["provenance"]


async def test_payment_details_never_reach_the_compiler(engine: Engine) -> None:
    res = await engine.compile("book Miami with my card 4111 1111 1111 1111")
    assert res.status == "rejected" and "card" in res.speech


async def test_fast_path_latency_slo(engine: Engine) -> None:
    await engine.compile(FAMILY, sign=False)  # warm caches
    times = []
    for _ in range(8):
        t0 = time.perf_counter()
        res = await engine.compile(FAMILY, sign=False)
        times.append((time.perf_counter() - t0) * 1000)
        assert res.status == "planned"
    times.sort()
    assert times[len(times) // 2] < 500, times
