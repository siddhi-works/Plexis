"""The deterministic front end: what each phrasing compiles to, with exact provenance."""

from __future__ import annotations

import pytest

from qed.compiler.rules import parse
from tests.conftest import WHEN


def kinds(intent):  # type: ignore[no-untyped-def]
    return {c.kind for c in intent.constraints}


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        (
            f"3 nights from Seattle to San Francisco on {WHEN} for 2 under $2,500",
            {"origin": "SEA", "destination": "SFO", "nights": 3, "travelers": 2, "kinds": {"budget_max"}},
        ),
        (
            f"Weekend in Portland from Seattle by train on {WHEN}",
            {"origin": "SEA", "destination": "PDX", "nights": 2, "kinds": {"transport_modes"}},
        ),
        (
            f"Family of 4 from Chicago to Miami {WHEN}, nonstop, kid-friendly, 2 museums",
            {"travelers": 4, "kinds": {"max_stops", "stay_rule", "category_min"}},
        ),
        (
            f"Take me and my wife from NYC to Boston on {WHEN} for 2 nights, cheapest, no red-eye, arrive by 6",
            {"travelers": 2, "objective": "cheapest", "kinds": {"no_red_eye", "arrive_before"}},
        ),
        (
            f"4 days in Denver from Austin starting {WHEN}, splurge, rated 4.5+",
            {"nights": 3, "objective": "best", "kinds": {"stay_min_rating"}},
        ),
        (
            f"3 nights SEA to SFO on {WHEN}, no flights before 8am, vegan, at least two Thai dinners",
            {"kinds": {"depart_after", "diet_all", "cuisine_min"}},
        ),
        (
            f"Road trip from LA to San Diego on {WHEN} for 2 nights with my dog, relaxed pace",
            {"destination": "SAN", "kinds": {"transport_modes", "stay_rule", "activities_per_day"}},
        ),
        (
            f"2 nights in Boston from New York on {WHEN} under $200 a night, gluten-free, avoid rain",
            {"kinds": {"stay_max_nightly", "diet_all", "dry_outdoor"}},
        ),
    ],
)
def test_compiles(text: str, expect: dict) -> None:  # type: ignore[type-arg]
    intent = parse(text)
    for field in ("origin", "destination", "nights", "travelers", "objective"):
        if field in expect:
            assert getattr(intent, field) == expect[field], (field, intent)
    assert expect["kinds"] <= kinds(intent), (expect["kinds"], kinds(intent))


def test_spans_point_at_the_words_that_justify_each_rule() -> None:
    text = f"3 nights from Seattle to San Francisco on {WHEN} under $2,500, no flights before 8am"
    intent = parse(text)
    for c in intent.constraints:
        assert c.span is not None
        assert text[c.span.start : c.span.end] == c.span.text
    budget = next(c for c in intent.constraints if c.kind == "budget_max")
    assert budget.amount == 2500 and "$2,500" in budget.span.text


def test_no_flights_before_does_not_ban_flying() -> None:
    intent = parse(f"3 nights from Seattle to San Francisco on {WHEN}, no flights before 8am")
    assert "transport_modes" not in kinds(intent)
    dep = next(c for c in intent.constraints if c.kind == "depart_after")
    assert dep.minutes == 8 * 60


def test_unknown_phrases_become_warnings_not_silent_drops() -> None:
    intent = parse(f"3 nights from Chicago to Miami on {WHEN} near the beach")
    warnings = [d for d in intent.diagnostics if d.code == "QED-W001"]
    assert any("beach" in d.message for d in warnings)
    assert intent.coverage < 1.0


def test_known_people_join_the_trip() -> None:
    intent = parse(
        f"3 nights to Denver from Austin on {WHEN} with Grandma and Maya", known_people=["Grandma", "Maya"]
    )
    assert intent.traveler_names == ["Grandma", "Maya"]
    assert intent.travelers == 3


def test_missing_fields_are_reported() -> None:
    intent = parse("Plan me a trip somewhere nice")
    assert set(intent.missing()) >= {"destination", "start_date", "nights"}


def test_negated_hostel_means_private() -> None:
    it = parse(f"3 nights in Chicago from Boston on {WHEN}, I don't want to stay in a hostel")
    kinds = next(c for c in it.constraints if c.kind == "stay_kinds").kinds
    assert "shared_room" not in kinds


def test_adults_and_kids_are_all_counted() -> None:
    assert parse(f"to Denver from Austin on {WHEN} for 3 nights, we're 2 adults and 2 kids").travelers == 4
