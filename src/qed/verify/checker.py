"""Independent plan checker: the proof checker in Plexis's proof-carrying plans.

It shares no code with either solver encoding. It reads the concrete plan and
the frozen partner data and recomputes every rule, every price and every
schedule fact directly. A plan is only certified when every check passes, so a
bug in an encoder can produce "no plan", but never a wrong plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from qed.ir import TripIntent
from qed.solver.catalog import Catalog
from qed.solver.plan import Plan
from qed.solver.schedule import (
    ARRIVAL_AFTERNOON_CUTOFF,
    ARRIVAL_DINNER_CUTOFF,
    DEPARTURE_BREAKFAST_CUTOFF,
    build_schedule,
)
from qed.util import WEEKDAYS, fmt_minutes, fmt_money


@dataclass
class Check:
    id: str
    label: str
    origin: str
    passed: bool
    evidence: str
    because: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "label": self.label,
            "origin": self.origin,
            "passed": self.passed,
            "evidence": self.evidence,
        }
        if self.because:
            d["because"] = self.because
        return d


@dataclass
class Verdict:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(
        self, cid: str, label: str, origin: str, passed: bool, evidence: str, because: str | None = None
    ) -> None:
        self.checks.append(Check(cid, label, origin, bool(passed), evidence, because))

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def verify(intent: TripIntent, catalog: Catalog, plan: Plan) -> Verdict:
    v = Verdict()
    assert intent.start_date and intent.nights
    sched = build_schedule(intent.start_date, intent.nights)
    end = intent.start_date + timedelta(days=intent.nights)
    P = intent.travelers
    out, ret, quote = plan.outbound, plan.inbound, plan.stay
    stay = quote.stay

    # --- provenance: every chosen record must exist, unchanged, in what partners returned.
    known = {t.id: t for t in catalog.outbound + catalog.inbound}
    v.add(
        "p_transport",
        "transport matches partner data",
        "integrity",
        known.get(out.id) == out and known.get(ret.id) == ret,
        f"{out.code} and {ret.code} found unchanged",
    )
    q = next((x for x in catalog.stays if x.stay.id == stay.id), None)
    v.add(
        "p_stay",
        "lodging quote matches partner data",
        "integrity",
        q == quote,
        f"{stay.name} quote {fmt_money(quote.total_price)}",
    )
    rest = {r.id: r for r in catalog.restaurants}
    attr = {a.id: a for a in catalog.attractions}
    v.add(
        "p_dining",
        "restaurants match partner data",
        "integrity",
        all(rest.get(m.restaurant.id) == m.restaurant for m in plan.meals),
        f"{len(plan.meals)} meal(s) checked",
    )
    v.add(
        "p_activities",
        "activities match partner data",
        "integrity",
        all(attr.get(a.attraction.id) == a.attraction for a in plan.activities),
        f"{len(plan.activities)} activit(ies) checked",
    )

    # --- structure
    v.add(
        "s_route",
        "outbound and return route the right cities on the right days",
        "structure",
        out.origin == intent.origin
        and out.destination == intent.destination
        and out.date == intent.start_date
        and ret.origin == intent.destination
        and ret.destination == intent.origin
        and ret.date == end,
        f"{out.origin}→{out.destination} {out.date}, {ret.origin}→{ret.destination} {ret.date}",
    )
    v.add(
        "s_stay",
        "lodging covers every night for every traveler",
        "structure",
        stay.city == intent.destination
        and quote.check_in == intent.start_date
        and quote.nights == intent.nights
        and quote.units * stay.max_guests >= P
        and quote.total_price == sum(quote.nightly) * quote.units,
        f"{quote.nights} night(s), {quote.units} unit(s) × up to {stay.max_guests} guests",
    )
    by_slot = {}
    for m in plan.meals:
        by_slot.setdefault(m.slot, []).append(m)
    meal_ok, meal_ev = True, []
    for slot in sched.meals:
        chosen = by_slot.get(slot.key, [])
        if slot.conditional == "arrival":
            expected = 1 if out.arrive_min <= ARRIVAL_DINNER_CUTOFF else 0
        elif slot.conditional == "departure":
            expected = 1 if ret.depart_min >= DEPARTURE_BREAKFAST_CUTOFF else 0
        else:
            expected = 1
        if len(chosen) != expected:
            options_exist = any(
                slot.meal in r.meals and r.closed_weekday != slot.date.weekday() for r in catalog.restaurants
            )
            if expected == 1 and not options_exist and slot.conditional:
                continue
            meal_ok = False
            meal_ev.append(f"{slot.key}: {len(chosen)} planned, {expected} expected")
        for m in chosen:
            r = m.restaurant
            if (
                slot.meal not in r.meals
                or r.closed_weekday == slot.date.weekday()
                or r.city != intent.destination
            ):
                meal_ok = False
                meal_ev.append(f"{r.name} can't serve {slot.meal} on {WEEKDAYS[slot.date.weekday()]}")
    v.add(
        "s_meals",
        "every meal slot filled by an open restaurant that serves it",
        "structure",
        meal_ok,
        "; ".join(meal_ev) or f"{len(plan.meals)} meal(s), all open and serving",
    )
    act_ok, act_ev = True, []
    for a in plan.activities:
        at = a.attraction
        if at.closed_weekday == a.date.weekday() or at.city != intent.destination:
            act_ok = False
            act_ev.append(f"{at.name} is closed on {WEEKDAYS[a.date.weekday()]}")
        if a.day == 1 and out.arrive_min > ARRIVAL_AFTERNOON_CUTOFF:
            act_ok = False
            act_ev.append(f"{at.name} planned on arrival day but landing is {fmt_minutes(out.arrive_min)}")
        if a.date != intent.start_date + timedelta(days=a.day - 1):
            act_ok = False
            act_ev.append(f"{at.name} has an inconsistent date")
    if sum(1 for a in plan.activities if a.day == 1) > 1:
        act_ok = False
        act_ev.append("more than one activity on arrival afternoon")
    v.add(
        "s_activities",
        "every activity is open on the day it's planned",
        "structure",
        act_ok,
        "; ".join(act_ev) or f"{len(plan.activities)} activit(ies), all open",
    )

    # --- commonsense
    ids = [m.restaurant.id for m in plan.meals]
    v.add(
        "k_diverse_dining",
        "no restaurant repeated",
        "commonsense",
        len(ids) == len(set(ids)),
        f"{len(set(ids))} distinct of {len(ids)}",
    )
    aids = [a.attraction.id for a in plan.activities]
    v.add(
        "k_diverse_sights",
        "no activity repeated",
        "commonsense",
        len(aids) == len(set(aids)),
        f"{len(set(aids))} distinct of {len(aids)}",
    )
    worst_day, ok_time = "", True
    for ad in sched.activity_days:
        used = sum(a.attraction.minutes for a in plan.activities if a.day == ad.day)
        if used > ad.minutes:
            ok_time, worst_day = False, f"day {ad.day} needs {used} min of {ad.minutes}"
    v.add(
        "k_day_time",
        "each day's activities fit in the time available",
        "commonsense",
        ok_time,
        worst_day or "all days within their time budget",
    )
    v.add(
        "k_seats",
        "enough seats left for every traveler",
        "commonsense",
        out.seats_left >= P and ret.seats_left >= P,
        f"{out.seats_left} and {ret.seats_left} seats left for {P}",
    )
    v.add(
        "k_car_return",
        "a rental car is driven back, not left at the destination",
        "commonsense",
        (out.mode == "drive") == (ret.mode == "drive"),
        f"{out.mode} out, {ret.mode} back",
    )
    v.add(
        "k_min_nights",
        "lodging's minimum-stay rule fits the trip length",
        "commonsense",
        stay.min_nights <= intent.nights,
        f"minimum {stay.min_nights}, staying {intent.nights}",
    )

    # --- arithmetic
    def tcost(t: Any) -> int:
        return t.price * P if t.price_basis == "per_person" else t.price

    recomputed = {
        "transport": tcost(out) + tcost(ret),
        "lodging": quote.total_price,
        "dining": sum(m.restaurant.avg_cost for m in plan.meals) * P,
        "activities": sum(a.attraction.price for a in plan.activities) * P,
    }
    recomputed["total"] = sum(recomputed.values())
    v.add(
        "a_total",
        "price breakdown adds up",
        "arithmetic",
        recomputed == plan.breakdown,
        f"{fmt_money(recomputed['transport'])} + {fmt_money(recomputed['lodging'])} + "
        f"{fmt_money(recomputed['dining'])} + {fmt_money(recomputed['activities'])} = {fmt_money(recomputed['total'])}",
    )

    # --- every rule from the request, profile and defaults
    total = recomputed["total"]
    bad = catalog.bad_weather_days()
    for c in intent.constraints:
        ok, ev = _check_rule(c, plan, total, P, bad)
        v.add(c.id, c.describe(), c.origin, ok, ev, getattr(c, "because", None))
    return v


def _check_rule(c: Any, plan: Plan, total: int, P: int, bad: set[str]) -> tuple[bool, str]:
    out, ret, stay = plan.outbound, plan.inbound, plan.stay.stay
    legs = {"outbound": out, "return": ret}
    k = c.kind
    if k == "budget_max":
        limit = c.amount * (P if c.per_person else 1)
        return total <= limit, f"total {fmt_money(total)} vs limit {fmt_money(limit)}"
    if k == "depart_after":
        t = legs[c.leg]
        return t.depart_min >= c.minutes, f"{t.code} departs {fmt_minutes(t.depart_min)}"
    if k == "arrive_before":
        t = legs[c.leg]
        return t.arrive_min <= c.minutes, f"{t.code} arrives {fmt_minutes(t.arrive_min)}"
    if k == "no_red_eye":
        return not (
            out.red_eye or ret.red_eye
        ), f"departures {fmt_minutes(out.depart_min)} and {fmt_minutes(ret.depart_min)}"
    if k == "max_stops":
        worst = (
            max(t.stops for t in (out, ret) if t.mode == "flight")
            if any(t.mode == "flight" for t in (out, ret))
            else 0
        )
        return worst <= c.stops, f"most stops on a flight: {worst}"
    if k == "transport_modes":
        return out.mode in c.allowed and ret.mode in c.allowed, f"{out.mode} out, {ret.mode} back"
    if k == "stay_accessible":
        return stay.accessible, f"{stay.name}: {'step-free' if stay.accessible else 'not step-free'}"
    if k == "stay_min_rating":
        return stay.rating >= c.rating, f"{stay.name} rated {stay.rating}"
    if k == "stay_kinds":
        return stay.kind in c.kinds, f"{stay.name} is a {stay.kind.replace('_', ' ')}"
    if k == "stay_rule":
        return bool(
            getattr(stay, c.rule)
        ), f"{stay.name}: {c.rule.replace('_', ' ')} = {getattr(stay, c.rule)}"
    if k == "stay_amenity":
        has = c.amenity in stay.amenities
        return has == c.want, f"{stay.name} {'has' if has else 'does not have'} {c.amenity.replace('_', ' ')}"
    if k == "stay_max_nightly":
        worst = max(plan.stay.nightly)
        return worst <= c.amount, f"highest night {fmt_money(worst)}"
    if k == "cuisine_min":
        n = sum(1 for m in plan.meals if c.cuisine in m.restaurant.cuisines)
        return n >= c.count, f"{n} {c.cuisine} meal(s) planned"
    if k == "diet_all":
        bad_meals = [m.restaurant.name for m in plan.meals if c.diet not in m.restaurant.diets]
        return not bad_meals, f"{len(plan.meals) - len(bad_meals)}/{len(plan.meals)} meals {c.diet}-friendly"
    if k == "meal_max_price":
        worst = max((m.restaurant.avg_cost for m in plan.meals), default=0)
        return worst <= c.amount, f"priciest meal {fmt_money(worst)} per person"
    if k == "category_min":
        n = sum(1 for a in plan.activities if a.attraction.category == c.category)
        return n >= c.count, f"{n} {c.category} visit(s) planned"
    if k == "attractions_accessible":
        bad_acts = [a.attraction.name for a in plan.activities if not a.attraction.accessible]
        return not bad_acts, "all activities step-free" if not bad_acts else "not accessible: " + ", ".join(
            bad_acts
        )
    if k == "activities_per_day":
        counts = {}
        for a in plan.activities:
            counts[a.day] = counts.get(a.day, 0) + 1
        full_days = range(2, plan.stay.nights + 1)
        ok = all(c.min <= counts.get(d, 0) <= c.max for d in full_days)
        return ok, "per full day: " + ", ".join(
            str(counts.get(d, 0)) for d in full_days
        ) if full_days else "no full days"
    if k == "dry_outdoor":
        wet = [
            a.attraction.name
            for a in plan.activities
            if not a.attraction.indoor and a.date.isoformat() in bad
        ]
        return not wet, "outdoor plans all on dry days" if not wet else "outdoor on a wet day: " + ", ".join(
            wet
        )
    if k == "include_item":
        if not c.item_id:
            return False, f"'{c.name}' not found in partner data"
        return (
            c.item_id in plan.item_ids(),
            f"{c.name} {'included' if c.item_id in plan.item_ids() else 'missing'}",
        )
    if k == "exclude_item":
        if not c.item_id:
            return True, f"'{c.name}' not offered, so it is avoided"
        return (
            c.item_id not in plan.item_ids(),
            f"{c.name} {'avoided' if c.item_id not in plan.item_ids() else 'present'}",
        )
    return False, f"unknown rule kind {k}"
