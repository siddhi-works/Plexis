"""An independent second opinion from the Z3 SMT solver.

This module re-encodes the trip rules from scratch with pseudo-Boolean
constraints instead of reusing the CP-SAT model. When CP-SAT reports that a set
of rules conflicts, Z3 must independently agree the same set is unsatisfiable
before Plexis says "impossible" out loud: two different solvers, two different
encodings, one claim.
"""

from __future__ import annotations

from typing import Any

import z3

from qed.ir import TripIntent
from qed.solver.catalog import Catalog
from qed.solver.plan import Plan
from qed.solver.schedule import (
    ARRIVAL_AFTERNOON_CUTOFF,
    ARRIVAL_DINNER_CUTOFF,
    DEPARTURE_BREAKFAST_CUTOFF,
    build_schedule,
)

# Independent infeasibility confirmation runs in two tiers: an arithmetic cost floor
# (instant, hand-checkable) for budget conflicts, then a full Z3 SMT proof.


class _Enc:
    def __init__(self, intent: TripIntent, cat: Catalog) -> None:
        assert intent.start_date and intent.nights
        self.i, self.c = intent, cat
        self.s = build_schedule(intent.start_date, intent.nights)
        self.P = intent.travelers
        self.out = {t.id: z3.Bool(f"o:{t.id}") for t in cat.outbound}
        self.ret = {t.id: z3.Bool(f"r:{t.id}") for t in cat.inbound}
        self.stay = {q.stay.id: z3.Bool(f"s:{q.stay.id}") for q in cat.stays}
        self.meal: dict[tuple[str, str], Any] = {}
        self.act: dict[tuple[int, str], Any] = {}
        self.f: list[Any] = []

    def _one(self, bs: list[Any]) -> Any:
        return z3.PbEq([(b, 1) for b in bs], 1) if bs else z3.BoolVal(False)

    def _none(self, bs: list[Any]) -> Any:
        return z3.And([z3.Not(b) for b in bs]) if bs else z3.BoolVal(True)

    def structure(self) -> None:
        c, s = self.c, self.s
        self.f += [
            self._one(list(self.out.values())),
            self._one(list(self.ret.values())),
            self._one(list(self.stay.values())),
        ]
        arr_dinner = z3.Or(
            [self.out[t.id] for t in c.outbound if t.arrive_min <= ARRIVAL_DINNER_CUTOFF]
            or [z3.BoolVal(False)]
        )
        arr_aft = z3.Or(
            [self.out[t.id] for t in c.outbound if t.arrive_min <= ARRIVAL_AFTERNOON_CUTOFF]
            or [z3.BoolVal(False)]
        )
        dep_bk = z3.Or(
            [self.ret[t.id] for t in c.inbound if t.depart_min >= DEPARTURE_BREAKFAST_CUTOFF]
            or [z3.BoolVal(False)]
        )
        for slot in s.meals:
            bs = []
            for r in c.restaurants:
                if slot.meal in r.meals and r.closed_weekday != slot.date.weekday():
                    b = z3.Bool(f"m:{slot.key}:{r.id}")
                    self.meal[(slot.key, r.id)] = b
                    bs.append(b)
            if not bs:
                if slot.conditional is None:
                    self.f.append(z3.BoolVal(False))
                continue
            if slot.conditional == "arrival":
                self.f.append(z3.If(arr_dinner, self._one(bs), self._none(bs)))
            elif slot.conditional == "departure":
                self.f.append(z3.If(dep_bk, self._one(bs), self._none(bs)))
            else:
                self.f.append(self._one(bs))
        for ad in s.activity_days:
            bs = []
            for a in c.attractions:
                if a.closed_weekday != ad.date.weekday():
                    b = z3.Bool(f"a:{ad.day}:{a.id}")
                    self.act[(ad.day, a.id)] = b
                    bs.append(b)
            if not ad.full and bs:
                self.f.append(z3.Implies(z3.Not(arr_aft), self._none(bs)))
                self.f.append(z3.PbLe([(b, 1) for b in bs], 1))

    def total_terms(self) -> list[tuple[Any, int]]:
        P, c = self.P, self.c

        def tc(t: Any) -> int:
            return t.price * P if t.price_basis == "per_person" else t.price

        terms = [(self.out[t.id], tc(t)) for t in c.outbound] + [(self.ret[t.id], tc(t)) for t in c.inbound]
        terms += [(self.stay[q.stay.id], q.total_price) for q in c.stays]
        rest = {r.id: r for r in c.restaurants}
        attr = {a.id: a for a in c.attractions}
        terms += [(b, rest[rid].avg_cost * P) for (sk, rid), b in self.meal.items()]
        terms += [(b, attr[aid].price * P) for (d, aid), b in self.act.items()]
        return [(b, w) for b, w in terms if w]

    def rule(self, gid: str) -> Any:
        c, s = self.c, self.s
        rest = {r.id: r for r in c.restaurants}
        attr = {a.id: a for a in c.attractions}
        legs = {"outbound": (self.out, c.outbound), "return": (self.ret, c.inbound)}
        N = self.i.nights or 1
        if gid == "k_seats":
            return self._none(
                [self.out[t.id] for t in c.outbound if t.seats_left < self.P]
                + [self.ret[t.id] for t in c.inbound if t.seats_left < self.P]
            )
        if gid == "k_car_return":
            out_drive = z3.Or(
                [self.out[t.id] for t in c.outbound if t.mode == "drive"] or [z3.BoolVal(False)]
            )
            ret_drive = z3.Or([self.ret[t.id] for t in c.inbound if t.mode == "drive"] or [z3.BoolVal(False)])
            return out_drive == ret_drive
        if gid == "k_min_nights":
            return self._none([self.stay[q.stay.id] for q in c.stays if q.stay.min_nights > N])
        if gid == "k_diverse_dining":
            parts = []
            for r in c.restaurants:
                bs = [b for (sk, rid), b in self.meal.items() if rid == r.id]
                if len(bs) > 1:
                    parts.append(z3.PbLe([(b, 1) for b in bs], 1))
            return z3.And(parts) if parts else z3.BoolVal(True)
        if gid == "k_diverse_sights":
            parts = []
            for a in c.attractions:
                bs = [b for (d, aid), b in self.act.items() if aid == a.id]
                if len(bs) > 1:
                    parts.append(z3.PbLe([(b, 1) for b in bs], 1))
            return z3.And(parts) if parts else z3.BoolVal(True)
        if gid == "k_day_time":
            parts = []
            for ad in s.activity_days:
                terms = [(b, attr[aid].minutes) for (d, aid), b in self.act.items() if d == ad.day]
                if terms:
                    parts.append(z3.PbLe(terms, ad.minutes))
            return z3.And(parts) if parts else z3.BoolVal(True)
        k = self.i.by_id(gid)
        if k is None:
            return z3.BoolVal(True)
        kind = k.kind
        if kind == "budget_max":
            return z3.PbLe(self.total_terms(), k.amount * (self.P if k.per_person else 1))
        if kind == "depart_after":
            vs, opts = legs[k.leg]
            return self._none([vs[t.id] for t in opts if t.depart_min < k.minutes])
        if kind == "arrive_before":
            vs, opts = legs[k.leg]
            return self._none([vs[t.id] for t in opts if t.arrive_min > k.minutes])
        if kind == "no_red_eye":
            return self._none([vs[t.id] for vs, opts in legs.values() for t in opts if t.red_eye])
        if kind == "max_stops":
            return self._none(
                [
                    vs[t.id]
                    for vs, opts in legs.values()
                    for t in opts
                    if t.mode == "flight" and t.stops > k.stops
                ]
            )
        if kind == "transport_modes":
            return self._none(
                [vs[t.id] for vs, opts in legs.values() for t in opts if t.mode not in k.allowed]
            )
        stay_attr = {
            "stay_accessible": lambda q: q.stay.accessible,
            "stay_min_rating": lambda q: q.stay.rating >= k.rating if hasattr(k, "rating") else True,
            "stay_kinds": lambda q: q.stay.kind in getattr(k, "kinds", []),
            "stay_rule": lambda q: bool(getattr(q.stay, getattr(k, "rule", "accessible"))),
            "stay_max_nightly": lambda q: max(q.nightly) <= getattr(k, "amount", 0),
        }
        if kind == "stay_amenity":
            return self._none(
                [self.stay[q.stay.id] for q in c.stays if (k.amenity in q.stay.amenities) != k.want]
            )
        if kind in stay_attr:
            return self._none([self.stay[q.stay.id] for q in c.stays if not stay_attr[kind](q)])
        if kind == "cuisine_min":
            terms = [(b, 1) for (sk, rid), b in self.meal.items() if k.cuisine in rest[rid].cuisines]
            return z3.PbGe(terms, k.count) if terms else z3.BoolVal(k.count <= 0)
        if kind == "diet_all":
            return self._none([b for (sk, rid), b in self.meal.items() if k.diet not in rest[rid].diets])
        if kind == "meal_max_price":
            return self._none([b for (sk, rid), b in self.meal.items() if rest[rid].avg_cost > k.amount])
        if kind == "category_min":
            terms = [(b, 1) for (d, aid), b in self.act.items() if attr[aid].category == k.category]
            return z3.PbGe(terms, k.count) if terms else z3.BoolVal(k.count <= 0)
        if kind == "attractions_accessible":
            return self._none([b for (d, aid), b in self.act.items() if not attr[aid].accessible])
        if kind == "activities_per_day":
            parts = []
            for ad in s.activity_days:
                if not ad.full:
                    continue
                terms = [(b, 1) for (d, aid), b in self.act.items() if d == ad.day]
                if terms:
                    parts += [z3.PbGe(terms, k.min), z3.PbLe(terms, k.max)]
                elif k.min > 0:
                    parts.append(z3.BoolVal(False))
            return z3.And(parts) if parts else z3.BoolVal(True)
        if kind == "dry_outdoor":
            bad = c.bad_weather_days()
            dates = s.dates
            return self._none(
                [
                    b
                    for (d, aid), b in self.act.items()
                    if not attr[aid].indoor and dates[d - 1].isoformat() in bad
                ]
            )
        if kind in ("include_item", "exclude_item") and k.item_id:
            bs = self._item(k.item_id)
            return (
                z3.Or(bs)
                if kind == "include_item" and bs
                else (self._none(bs) if bs else z3.BoolVal(kind == "exclude_item"))
            )
        return z3.BoolVal(True)

    def _item(self, item_id: str) -> list[Any]:
        bs = [b for key, b in self.out.items() if key == item_id] + [
            b for key, b in self.ret.items() if key == item_id
        ]
        bs += [b for key, b in self.stay.items() if key == item_id]
        bs += [b for (sk, rid), b in self.meal.items() if rid == item_id]
        bs += [b for (d, aid), b in self.act.items() if aid == item_id]
        return bs


def cost_lower_bound(intent: TripIntent, catalog: Catalog, guard_ids: list[str]) -> int | None:
    """A hand-checkable infeasibility certificate for budget conflicts.

    Sums the cheapest option that satisfies the conflict's other per-item rules
    for every slot a trip must fill (relaxing everything else). If that floor
    exceeds the budget, no plan can exist: a proof anyone can redo with a
    calculator. Returns the floor, or None if the conflict has no budget rule.
    """
    rules = [intent.by_id(g) for g in guard_ids if intent.by_id(g) is not None]
    budget = next((c for c in rules if c.kind == "budget_max"), None)
    if budget is None or not intent.start_date or not intent.nights:
        return None
    P = intent.travelers

    def tx_ok(t: Any, leg: str) -> bool:
        if "k_seats" in guard_ids and t.seats_left < P:
            return False
        for c in rules:
            k = c.kind
            if (
                (k == "depart_after" and c.leg == leg and t.depart_min < c.minutes)
                or (k == "arrive_before" and c.leg == leg and t.arrive_min > c.minutes)
                or (k == "no_red_eye" and t.red_eye)
                or (k == "max_stops" and t.mode == "flight" and t.stops > c.stops)
                or (k == "transport_modes" and t.mode not in c.allowed)
            ):
                return False
        return True

    def stay_ok(q: Any) -> bool:
        s = q.stay
        for c in rules:
            k = c.kind
            if (
                (k == "stay_accessible" and not s.accessible)
                or (k == "stay_min_rating" and s.rating < c.rating)
                or (k == "stay_kinds" and s.kind not in c.kinds)
                or (k == "stay_rule" and not getattr(s, c.rule))
                or (k == "stay_max_nightly" and max(q.nightly) > c.amount)
            ):
                return False
        return not ("k_min_nights" in guard_ids and s.min_nights > (intent.nights or 1))

    def rest_ok(r: Any) -> bool:
        return all(
            not (
                (c.kind == "diet_all" and c.diet not in r.diets)
                or (c.kind == "meal_max_price" and r.avg_cost > c.amount)
            )
            for c in rules
        )

    def tcost(t: Any) -> int:
        return t.price * P if t.price_basis == "per_person" else t.price

    outs = [tcost(t) for t in catalog.outbound if tx_ok(t, "outbound")]
    ins = [tcost(t) for t in catalog.inbound if tx_ok(t, "return")]
    stays = [q.total_price for q in catalog.stays if stay_ok(q)]
    if not outs or not ins or not stays:
        return None
    floor = min(outs) + min(ins) + min(stays)
    sched = build_schedule(intent.start_date, intent.nights)
    # Conditional slots may legitimately be skipped, so they contribute nothing to the floor.
    slots = [s for s in sched.meals if not s.conditional]
    options = [
        [
            r
            for r in catalog.restaurants
            if s.meal in r.meals and r.closed_weekday != s.date.weekday() and rest_ok(r)
        ]
        for s in slots
    ]
    if "k_diverse_dining" in guard_ids:
        dining = _distinct_assignment_cost(slots, options, catalog.restaurants)
        if dining is None:
            return None
        floor += dining * P
    else:
        floor += sum(min(r.avg_cost for r in opts) for opts in options if opts) * P
    per_day = next((c for c in rules if c.kind == "activities_per_day"), None)
    if per_day and per_day.min > 0:
        for ad in sched.activity_days:
            if not ad.full:
                continue
            prices = sorted(
                a.price
                for a in catalog.attractions
                if a.closed_weekday != ad.date.weekday()
                and not (any(c.kind == "attractions_accessible" for c in rules) and not a.accessible)
            )
            floor += sum(prices[: per_day.min]) * P
    return floor


def _distinct_assignment_cost(
    slots: list[Any], options: list[list[Any]], restaurants: list[Any]
) -> int | None:
    """Cheapest way to fill every slot with a *different* restaurant (min-cost bipartite matching)."""
    from ortools.graph.python import linear_sum_assignment

    if len(slots) > len(restaurants):
        return None
    index = {r.id: j for j, r in enumerate(restaurants)}
    lsa = linear_sum_assignment.SimpleLinearSumAssignment()
    for i, opts in enumerate(options):
        for r in opts:
            lsa.add_arc_with_cost(i, index[r.id], r.avg_cost)
    if not slots:
        return 0
    # The solver needs a perfect matching, so free "unused" rows absorb the spare restaurants at no cost.
    for i in range(len(slots), len(restaurants)):
        for j in range(len(restaurants)):
            lsa.add_arc_with_cost(i, j, 0)
    if lsa.solve() != lsa.OPTIMAL:
        return None
    return int(lsa.optimal_cost())


def check_conflict(
    intent: TripIntent, catalog: Catalog, guard_ids: list[str], *, timeout_ms: int = 3000
) -> str:
    """'unsat' confirms that the named rules really conflict."""
    floor = cost_lower_bound(intent, catalog, guard_ids)
    budget = next(
        (
            intent.by_id(g)
            for g in guard_ids
            if intent.by_id(g) is not None and intent.by_id(g).kind == "budget_max"
        ),
        None,
    )
    if floor is not None and budget is not None:
        limit = budget.amount * (intent.travelers if budget.per_person else 1)
        if floor > limit:
            return f"unsat (cost floor ${floor:,} > ${limit:,})"
    enc = _Enc(intent, catalog)
    enc.structure()
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.add(*enc.f)
    for gid in guard_ids:
        if gid == "force":
            continue
        solver.add(enc.rule(gid))
    return str(solver.check())


def check_plan(intent: TripIntent, catalog: Catalog, plan: Plan, *, timeout_ms: int = 3000) -> str:
    """'sat' confirms the concrete plan satisfies every rule under the Z3 encoding."""
    enc = _Enc(intent, catalog)
    enc.structure()
    solver = z3.Solver()
    solver.set("timeout", timeout_ms)
    solver.add(*enc.f)
    for gid in [
        "k_min_nights",
        "k_diverse_dining",
        "k_diverse_sights",
        "k_day_time",
        "k_seats",
        "k_car_return",
    ] + [c.id for c in intent.constraints]:
        solver.add(enc.rule(gid))
    chosen = plan.item_ids()
    for key, b in list(enc.out.items()) + list(enc.ret.items()) + list(enc.stay.items()):
        solver.add(b == (key in chosen))
    picked_meals = {(m.slot, m.restaurant.id) for m in plan.meals}
    for key, b in enc.meal.items():
        solver.add(b == (key in picked_meals))
    picked_acts = {(a.day, a.attraction.id) for a in plan.activities}
    for key, b in enc.act.items():
        solver.add(b == (key in picked_acts))
    return str(solver.check())
