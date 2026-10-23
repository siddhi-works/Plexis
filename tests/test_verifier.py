"""Mutation tests for the proof checker: corrupt a valid plan, the checker must notice."""

from __future__ import annotations

import pytest

from qed.ir import BudgetMax, DietAll, StayAccessible
from qed.solver.cpsat import TripModel
from qed.solver.plan import Plan
from qed.verify.checker import verify
from tests.conftest import TRIP_DAY
from tests.helpers import catalog_for, make_intent


@pytest.fixture
def solved():  # type: ignore[no-untyped-def]
    it = make_intent(
        "SEA", "SFO", TRIP_DAY, 3, 2, [BudgetMax(amount=3000), StayAccessible(), DietAll(diet="vegetarian")]
    )
    cat = catalog_for(it)
    plan = TripModel(it, cat).solve(workers=4).plan
    assert plan is not None and verify(it, cat, plan).ok
    return it, cat, plan


def failing(it, cat, plan: Plan) -> set[str]:  # type: ignore[no-untyped-def]
    return {c.id for c in verify(it, cat, plan).failures()}


def test_swapping_in_an_inaccessible_stay_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    bad = next(q for q in cat.stays if not q.stay.accessible)
    mutated = plan.model_copy(update={"stay": bad})
    mutated.compute_breakdown()
    assert "c2" in failing(it, cat, mutated)


def test_a_tampered_price_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    cheaper = plan.outbound.model_copy(update={"price": 1})
    mutated = plan.model_copy(update={"outbound": cheaper})
    mutated.compute_breakdown()
    assert "p_transport" in failing(it, cat, mutated)


def test_a_wrong_total_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    mutated = plan.model_copy(deep=True)
    mutated.breakdown["total"] -= 100
    assert "a_total" in failing(it, cat, mutated)


def test_a_repeated_restaurant_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    meals = [m.model_copy() for m in plan.meals]
    same_type = [i for i, m in enumerate(meals) if m.meal == meals[1].meal]
    if len(same_type) < 2:
        pytest.skip("not enough meals of one type")
    meals[same_type[1]] = meals[same_type[1]].model_copy(
        update={"restaurant": meals[same_type[0]].restaurant}
    )
    mutated = plan.model_copy(update={"meals": meals})
    mutated.compute_breakdown()
    assert "k_diverse_dining" in failing(it, cat, mutated)


def test_an_activity_on_its_closed_day_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    closed = next((a for a in cat.attractions if a.closed_weekday is not None), None)
    full_day = next((a for a in plan.activities if a.day >= 2), None)
    if closed is None or full_day is None:
        pytest.skip("no closed attraction in this city")
    from datetime import timedelta

    day = (
        next(
            d
            for d in range(2, it.nights + 1)
            if (it.start_date + timedelta(days=d - 1)).weekday() == closed.closed_weekday
        )
        if any(
            (it.start_date + timedelta(days=d - 1)).weekday() == closed.closed_weekday
            for d in range(2, it.nights + 1)
        )
        else None
    )
    if day is None:
        pytest.skip("closure day not inside the trip")
    acts = [
        full_day.model_copy(
            update={"attraction": closed, "day": day, "date": it.start_date + timedelta(days=day - 1)}
        )
    ]
    mutated = plan.model_copy(update={"activities": acts})
    mutated.compute_breakdown()
    assert "s_activities" in failing(it, cat, mutated)


def test_a_non_vegetarian_meal_is_caught(solved) -> None:  # type: ignore[no-untyped-def]
    it, cat, plan = solved
    meat = next(r for r in cat.restaurants if "vegetarian" not in r.diets and "dinner" in r.meals)
    meals = [m.model_copy() for m in plan.meals]
    i = next(i for i, m in enumerate(meals) if m.meal == "dinner")
    meals[i] = meals[i].model_copy(update={"restaurant": meat})
    mutated = plan.model_copy(update={"meals": meals})
    mutated.compute_breakdown()
    assert "c3" in failing(it, cat, mutated)
