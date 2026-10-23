"""Solver soundness: plans verify, conflicts are minimal, fixes really fix, two solvers agree."""

from __future__ import annotations

from qed.ir import BudgetMax, DepartAfter, StayAccessible, StayMinRating
from qed.solver import z3check
from qed.solver.cpsat import TripModel
from qed.solver.diagnose import explain_conflict, minimal_core, why_not
from qed.verify.checker import verify
from tests.conftest import TRIP_DAY
from tests.helpers import catalog_for, make_intent


def test_plan_is_optimal_verified_and_confirmed_by_z3() -> None:
    it = make_intent(
        "SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=2600), DepartAfter(minutes=480), StayAccessible()]
    )
    cat = catalog_for(it)
    res = TripModel(it, cat).solve(workers=4)
    assert res.status == "OPTIMAL" and res.plan is not None
    v = verify(it, cat, res.plan)
    assert v.ok, [c.label for c in v.failures()]
    assert res.plan.outbound.depart_min >= 480
    assert res.plan.stay.stay.accessible
    assert z3check.check_plan(it, cat, res.plan) == "sat"


def test_conflict_is_minimal_and_independently_unsat() -> None:
    it = make_intent(
        "SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=900), StayAccessible(), StayMinRating(rating=4.7)]
    )
    cat = catalog_for(it)
    model = TripModel(it, cat)
    assert model.solve().status == "INFEASIBLE"
    core = model.solve(want_core=True).core
    mus, _ = minimal_core(model, core)
    assert mus, "an infeasible request must name at least one rule"
    assert z3check.check_conflict(it, cat, mus).startswith("unsat")
    for gid in mus:  # minimality: dropping any one rule removes this conflict
        rest = [g for g in mus if g != gid]
        assert model.solve(rest, want_core=True).status != "INFEASIBLE"


def test_budget_fixit_is_exact_and_verified() -> None:
    it = make_intent("SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=900)])
    cat = catalog_for(it)
    model = TripModel(it, cat)
    conflict = explain_conflict(model, model.solve(want_core=True).core)
    fix = next(f for f in conflict.fixits if f.constraint_id == "c1")
    assert fix.verified and fix.resulting_cost is not None
    need = fix.patch["amount"]
    ok = make_intent("SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=need)])
    short = make_intent("SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=need - 1)])
    assert TripModel(ok, cat).solve().plan is not None
    assert TripModel(short, cat).solve().status == "INFEASIBLE"


def test_why_not_names_the_blocking_rule() -> None:
    it = make_intent("SEA", "SFO", TRIP_DAY, 3, 2, [StayAccessible()])
    cat = catalog_for(it)
    plan = TripModel(it, cat).solve().plan
    assert plan is not None
    blocked = next(q for q in cat.stays if not q.stay.accessible)
    ans = why_not(it, cat, blocked.stay.id, plan.total, blocked.stay.name)
    assert not ans.feasible
    assert any("wheelchair" in b for b in ans.blocking)


def test_groups_never_get_oversold_flights_and_cars_come_back() -> None:
    for o, d in [("SEA", "SFO"), ("AUS", "DEN"), ("BOS", "JFK"), ("LAX", "SAN")]:
        it = make_intent(o, d, TRIP_DAY, 2, 6, [], "cheapest")
        cat = catalog_for(it)
        plan = TripModel(it, cat).solve(workers=4).plan
        if plan is None:
            continue
        assert plan.outbound.seats_left >= 6 and plan.inbound.seats_left >= 6
        assert (plan.outbound.mode == "drive") == (plan.inbound.mode == "drive")
        assert verify(it, cat, plan).ok and z3check.check_plan(it, cat, plan) == "sat"
