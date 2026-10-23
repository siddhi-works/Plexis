"""Property-based and metamorphic tests over randomly generated requests.

Properties checked for every generated request:
- soundness: any plan the solver returns passes the independent verifier and Z3;
- conflicts: any reported conflict is UNSAT under Z3 and minimal;
- monotonicity (metamorphic): adding a rule never improves the optimal objective;
- relaxing the budget never turns a feasible request infeasible.
"""

from __future__ import annotations

from datetime import timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from qed.ir import (
    AttractionsAccessible,
    BudgetMax,
    CategoryMin,
    CuisineMin,
    DepartAfter,
    DietAll,
    MaxStops,
    NoRedEye,
    StayAccessible,
    StayAmenity,
    StayKinds,
    StayMinRating,
    StayRuleC,
)
from qed.solver import z3check
from qed.solver.cpsat import TripModel
from qed.solver.diagnose import minimal_core
from qed.verify.checker import verify
from tests.conftest import TRIP_DAY
from tests.helpers import catalog_for, make_intent

PAIRS = [("SEA", "SFO"), ("SEA", "PDX"), ("BOS", "JFK"), ("ORD", "MIA"), ("AUS", "DEN"), ("LAX", "SAN")]

rule = st.one_of(
    st.builds(BudgetMax, amount=st.integers(600, 4000)),
    st.builds(DepartAfter, minutes=st.sampled_from([360, 480, 600, 720])),
    st.just(NoRedEye()),
    st.builds(MaxStops, stops=st.sampled_from([0, 1])),
    st.just(StayAccessible()),
    st.builds(StayMinRating, rating=st.sampled_from([4.0, 4.4, 4.7])),
    st.builds(
        CuisineMin, cuisine=st.sampled_from(["thai", "italian", "indian", "seafood"]), count=st.integers(1, 2)
    ),
    st.builds(DietAll, diet=st.sampled_from(["vegetarian", "vegan", "gluten_free"])),
    st.builds(CategoryMin, category=st.sampled_from(["museum", "park", "garden"]), count=st.integers(1, 2)),
    st.just(AttractionsAccessible()),
    st.builds(StayRuleC, rule=st.sampled_from(["air_conditioned", "no_air_conditioning"])),
    st.builds(
        StayKinds, kinds=st.sampled_from([["shared_room"], ["hotel_room", "entire_home", "private_room"]])
    ),
    st.builds(
        StayAmenity,
        amenity=st.sampled_from(["pool", "breakfast_included", "free_parking", "kitchen", "gym", "view"]),
        want=st.booleans(),
    ),
)

request = st.tuples(
    st.sampled_from(PAIRS),
    st.integers(1, 4),
    st.integers(1, 4),
    st.integers(0, 20),
    st.lists(rule, max_size=4),
    st.sampled_from(["cheapest", "balanced", "best"]),
)

CFG = settings(max_examples=18, deadline=None, suppress_health_check=[HealthCheck.too_slow], derandomize=True)


def build(req):  # type: ignore[no-untyped-def]
    (o, d), nights, travelers, offset, rules, objective = req
    it = make_intent(
        o, d, TRIP_DAY + timedelta(days=offset), nights, travelers, [r.model_copy() for r in rules], objective
    )
    return it, catalog_for(it)


@CFG
@given(request)
def test_soundness_and_conflicts(req) -> None:  # type: ignore[no-untyped-def]
    it, cat = build(req)
    model = TripModel(it, cat)
    res = model.solve(workers=4, time_limit=2.0)
    if res.plan is not None:
        v = verify(it, cat, res.plan)
        assert v.ok, [(c.id, c.evidence) for c in v.failures()]
        assert z3check.check_plan(it, cat, res.plan) == "sat"
    elif res.status == "INFEASIBLE":
        mus, _ = minimal_core(model, model.solve(want_core=True).core)
        assert z3check.check_conflict(it, cat, mus).startswith("unsat")
        for gid in mus:
            assert model.solve([g for g in mus if g != gid], want_core=True).status != "INFEASIBLE"


@CFG
@given(request, rule)
def test_adding_a_rule_never_improves_the_optimum(req, extra) -> None:  # type: ignore[no-untyped-def]
    it, cat = build(req)
    base = TripModel(it, cat).solve(workers=4, time_limit=2.0)
    tighter = it.model_copy(deep=True)
    tighter.constraints.append(extra.model_copy())
    tighter.renumber()
    more = TripModel(tighter, cat).solve(workers=4, time_limit=2.0)
    if base.status == "INFEASIBLE":
        assert more.status == "INFEASIBLE"
    if base.status == "OPTIMAL" and more.status == "OPTIMAL":
        assert more.objective >= base.objective - 1e-6


@CFG
@given(request, st.integers(1, 1500))
def test_raising_the_budget_never_breaks_feasibility(req, raise_by) -> None:  # type: ignore[no-untyped-def]
    it, cat = build(req)
    budgets = [c for c in it.constraints if c.kind == "budget_max"]
    res = TripModel(it, cat).solve(workers=4, time_limit=2.0)
    if not budgets or res.plan is None:
        return
    looser = it.model_copy(deep=True)
    for c in looser.constraints:
        if c.kind == "budget_max":
            c.amount += raise_by
    assert TripModel(looser, cat).solve(workers=4, time_limit=2.0).plan is not None
