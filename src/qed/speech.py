"""Spoken summaries, rendered only from certified facts.

Alexa+ guidance is to return data and let the host compose speech; Plexis still
supplies a short, voice-ready summary (<= 3 options read aloud) for hosts and
simulators that want one. Every number in it comes from the certificate, and
the grounded-speech guard re-checks that before it is returned.
"""

from __future__ import annotations

from qed.domain.world import city_name
from qed.ir import FixIt, TripIntent
from qed.solver.plan import Plan
from qed.util import fmt_minutes, fmt_money


def _mode_phrase(plan: Plan) -> str:
    o = plan.outbound
    if o.mode == "flight":
        stop = "nonstop" if o.stops == 0 else f"{o.stops}-stop"
        return f"a {stop} {o.carrier} flight at {fmt_minutes(o.depart_min)}"
    if o.mode == "train":
        return f"the {fmt_minutes(o.depart_min)} {o.carrier} train"
    return f"a rental car, leaving at {fmt_minutes(o.depart_min)}"


def plan_speech(intent: TripIntent, plan: Plan, checks_passed: int) -> str:
    stay = plan.stay.stay
    budget = next((c for c in intent.constraints if c.kind == "budget_max"), None)
    cost = fmt_money(plan.total)
    budget_part = ""
    if budget is not None:
        limit = budget.amount * (intent.travelers if budget.per_person else 1)
        budget_part = (
            ", right on budget"
            if limit == plan.total
            else f", {fmt_money(limit - plan.total)} under your budget"
        )
    highlights = []
    for c in intent.constraints:
        if c.kind == "cuisine_min":
            m = next((x for x in plan.meals if c.cuisine in x.restaurant.cuisines), None)
            if m:
                highlights.append(f"{c.cuisine.title()} at {m.restaurant.name}")
        elif c.kind == "category_min":
            a = next((x for x in plan.activities if x.attraction.category == c.category), None)
            if a:
                highlights.append(f"the {a.attraction.name}")
    hl = (" Including " + " and ".join(highlights[:2]) + ".") if highlights else ""
    # Short enough to listen to; the screen carries the details.
    return (
        f"Your {city_name(intent.destination or '')} trip is ready: {_mode_phrase(plan)}, {stay.name}, "
        f"{cost} total{budget_part}.{hl} All {checks_passed} checks pass. Want me to book it?"
    )


def conflict_speech(message: str, fixits: list[FixIt]) -> str:
    if not fixits:
        return f"I can't find a plan that works. {message} Which one can I relax?"
    options = " Or ".join(
        f"I could {f.message}"
        + (f" for {fmt_money(f.resulting_cost)} total" if f.resulting_cost else "")
        + "."
        for f in fixits[:2]
    )
    return f"That combination is impossible. {message} {options} Which would you like?"


QUESTIONS = {
    "origin": "Where will you be leaving from?",
    "destination": "Where would you like to go?",
    "start_date": "What date do you want to leave?",
    "nights": "How many nights will you stay?",
}


def missing_speech(missing: list[str]) -> str:
    return " ".join(QUESTIONS[m] for m in missing[:2])
