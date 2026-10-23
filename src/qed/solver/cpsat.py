"""CP-SAT encoding of a TripIntent over a Catalog.

Every user, profile, default and commonsense rule is guarded by its own
Boolean *assumption literal*. Solving with all literals assumed true gives the
plan; when that is infeasible, CP-SAT reports which assumptions it needed to
prove infeasibility, which `diagnose.py` shrinks into a minimal conflict and
turns into verified fix-its.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from qed.domain.models import TransportOption
from qed.ir import TripIntent
from qed.solver.catalog import Catalog
from qed.solver.plan import ActivityChoice, MealChoice, Plan
from qed.solver.schedule import (
    ARRIVAL_AFTERNOON_CUTOFF,
    ARRIVAL_DINNER_CUTOFF,
    DEPARTURE_BREAKFAST_CUTOFF,
    Schedule,
    build_schedule,
)

COMMONSENSE = {
    "k_min_nights": "lodging's minimum-stay rule fits the trip length",
    "k_diverse_dining": "no restaurant repeated",
    "k_diverse_sights": "no activity repeated",
    "k_day_time": "each day's activities fit in the time available",
    "k_seats": "enough seats left for every traveler",
    "k_car_return": "a rental car is driven back, not left at the destination",
}


@dataclass
class Guard:
    id: str
    lit: Any
    origin: str
    text: str
    kind: str


@dataclass
class SolveResult:
    status: str
    plan: Plan | None
    wall_ms: float
    objective: float | None = None
    core: list[str] = field(default_factory=list)
    values: dict[str, int] = field(default_factory=dict)


def transport_inconvenience(t: TransportOption) -> int:
    """Dollar-equivalent cost of a leg's time and hassle, per traveler."""
    duration = t.arrive_min - t.depart_min
    return (
        t.stops * 30
        + max(0, duration - 90) // 4
        + (40 if t.red_eye else 0)
        + (15 if t.depart_min < 420 else 0)
        + (50 if t.mode == "drive" else 0)
    )


class TripModel:
    def __init__(self, intent: TripIntent, catalog: Catalog, *, force: str | None = None) -> None:
        assert intent.start_date and intent.nights, "intent must be complete before solving"
        self.intent = intent
        self.cat = catalog
        self.sched: Schedule = build_schedule(intent.start_date, intent.nights)
        self.P = intent.travelers
        self.m = cp_model.CpModel()
        self.guards: dict[str, Guard] = {}
        self._build(force)

    # ------------------------------------------------------------------ build

    def _guard(self, gid: str, origin: str, text: str, kind: str) -> Any:
        lit = self.m.NewBoolVar(f"g_{gid}")
        self.guards[gid] = Guard(gid, lit, origin, text, kind)
        return lit

    def _zero(self, vars_: Iterable[Any], lit: Any) -> None:
        vs = list(vars_)
        if vs:
            self.m.Add(sum(vs) == 0).OnlyEnforceIf(lit)

    def _at_least(self, vars_: Iterable[Any], n: int, lit: Any) -> None:
        vs = list(vars_)
        if vs:
            self.m.Add(sum(vs) >= n).OnlyEnforceIf(lit)
        elif n > 0:
            self.m.AddBoolOr([lit.Not()])

    def _build(self, force: str | None) -> None:
        m, cat, P, N = self.m, self.cat, self.P, self.intent.nights or 1
        self.x_out = [m.NewBoolVar(f"out_{i}") for i in range(len(cat.outbound))]
        self.x_ret = [m.NewBoolVar(f"ret_{i}") for i in range(len(cat.inbound))]
        self.x_stay = [m.NewBoolVar(f"stay_{k}") for k in range(len(cat.stays))]
        for group in (self.x_out, self.x_ret, self.x_stay):
            if group:
                m.AddExactlyOne(group)
            else:
                m.AddBoolOr([])  # nothing to choose from: structurally infeasible

        arr_dinner = sum(
            x for x, t in zip(self.x_out, cat.outbound, strict=True) if t.arrive_min <= ARRIVAL_DINNER_CUTOFF
        )
        arr_afternoon = sum(
            x
            for x, t in zip(self.x_out, cat.outbound, strict=True)
            if t.arrive_min <= ARRIVAL_AFTERNOON_CUTOFF
        )
        dep_breakfast = sum(
            x
            for x, t in zip(self.x_ret, cat.inbound, strict=True)
            if t.depart_min >= DEPARTURE_BREAKFAST_CUTOFF
        )

        # Meals
        self.meal: dict[tuple[str, str], Any] = {}
        self.slot_vars: dict[str, list[tuple[Any, Any]]] = {}
        for slot in self.sched.meals:
            options = []
            for r in cat.restaurants:
                if slot.meal in r.meals and r.closed_weekday != slot.date.weekday():
                    v = m.NewBoolVar(f"meal_{slot.key}_{r.id}")
                    self.meal[(slot.key, r.id)] = v
                    options.append((v, r))
            self.slot_vars[slot.key] = options
            vs = [v for v, _ in options]
            if slot.conditional == "arrival":
                if vs:
                    m.Add(sum(vs) == arr_dinner)
            elif slot.conditional == "departure":
                if vs:
                    m.Add(sum(vs) == dep_breakfast)
            elif vs:
                m.AddExactlyOne(vs)
            else:
                m.AddBoolOr([])

        # Activities
        self.act: dict[tuple[int, str], Any] = {}
        self.day_vars: dict[int, list[tuple[Any, Any]]] = {}
        for ad in self.sched.activity_days:
            opts = []
            for a in cat.attractions:
                if a.closed_weekday != ad.date.weekday():
                    v = m.NewBoolVar(f"act_d{ad.day}_{a.id}")
                    self.act[(ad.day, a.id)] = v
                    opts.append((v, a))
            self.day_vars[ad.day] = opts
            if not ad.full and opts:
                m.Add(sum(v for v, _ in opts) <= arr_afternoon)

        # Commonsense rules, each guarded so a conflict can name them.
        g = self._guard("k_min_nights", "commonsense", COMMONSENSE["k_min_nights"], "min_nights")
        self._zero((x for x, s in zip(self.x_stay, cat.stays, strict=True) if s.stay.min_nights > N), g)
        g = self._guard("k_diverse_dining", "commonsense", COMMONSENSE["k_diverse_dining"], "diverse_dining")
        for r in cat.restaurants:
            vs = [v for (sk, rid), v in self.meal.items() if rid == r.id]
            if len(vs) > 1:
                m.Add(sum(vs) <= 1).OnlyEnforceIf(g)
        g = self._guard("k_diverse_sights", "commonsense", COMMONSENSE["k_diverse_sights"], "diverse_sights")
        for a in cat.attractions:
            vs = [v for (d, aid), v in self.act.items() if aid == a.id]
            if len(vs) > 1:
                m.Add(sum(vs) <= 1).OnlyEnforceIf(g)
        g = self._guard("k_day_time", "commonsense", COMMONSENSE["k_day_time"], "day_time")
        for ad in self.sched.activity_days:
            opts = self.day_vars[ad.day]
            if opts:
                m.Add(sum(v * a.minutes for v, a in opts) <= ad.minutes).OnlyEnforceIf(g)

        g = self._guard("k_seats", "commonsense", COMMONSENSE["k_seats"], "seats")
        self._zero(
            (
                x
                for x, t in zip(self.x_out + self.x_ret, cat.outbound + cat.inbound, strict=True)
                if t.seats_left < P
            ),
            g,
        )
        g = self._guard("k_car_return", "commonsense", COMMONSENSE["k_car_return"], "car_return")
        out_drive = [x for x, t in zip(self.x_out, cat.outbound, strict=True) if t.mode == "drive"]
        ret_drive = [x for x, t in zip(self.x_ret, cat.inbound, strict=True) if t.mode == "drive"]
        m.Add(sum(out_drive) == sum(ret_drive)).OnlyEnforceIf(g)

        # Expressions used by the objective, constraints and fix-it searches.
        def tcost(t: TransportOption) -> int:
            return t.price * P if t.price_basis == "per_person" else t.price

        self.e_transport = sum(x * tcost(t) for x, t in zip(self.x_out, cat.outbound, strict=True)) + sum(
            x * tcost(t) for x, t in zip(self.x_ret, cat.inbound, strict=True)
        )
        self.e_lodging = sum(x * s.total_price for x, s in zip(self.x_stay, cat.stays, strict=True))
        self.e_dining = sum(
            v * r.avg_cost * P for (sk, rid), v in self.meal.items() for r in [self._rest(rid)]
        )
        self.e_activities = sum(
            v * a.price * P for (d, aid), v in self.act.items() for a in [self._attr(aid)]
        )
        self.e_total = self.e_transport + self.e_lodging + self.e_dining + self.e_activities

        bad_days = cat.bad_weather_days()
        self.e_weather = sum(
            v * 60
            for (d, aid), v in self.act.items()
            if not self._attr(aid).indoor and self._date(d) in bad_days
        )
        self.e_inconvenience = sum(
            x * transport_inconvenience(t) * P for x, t in zip(self.x_out, cat.outbound, strict=True)
        ) + sum(x * transport_inconvenience(t) * P for x, t in zip(self.x_ret, cat.inbound, strict=True))
        aff = cat.affinity
        self.e_quality = (
            sum(x * int(s.stay.rating * 10) * 4 * N for x, s in zip(self.x_stay, cat.stays, strict=True))
            + sum(
                v * (int(r.rating * 10) + sum(aff.get(c, 0) for c in r.cuisines))
                for (sk, rid), v in self.meal.items()
                for r in [self._rest(rid)]
            )
            + sum(
                v * (int(a.rating * 10) * 2 + aff.get(a.category, 0))
                for (d, aid), v in self.act.items()
                for a in [self._attr(aid)]
            )
        )

        for c in self.intent.constraints:
            self._encode(c)
        if force:
            self._encode_force(force)
        self.set_objective()

    def _rest(self, rid: str) -> Any:
        return self._rest_idx[rid]

    def _attr(self, aid: str) -> Any:
        return self._attr_idx[aid]

    def _date(self, day: int) -> str:
        return self.sched.dates[day - 1].isoformat()

    @property
    def _rest_idx(self) -> dict[str, Any]:
        if not hasattr(self, "_ri"):
            self._ri = {r.id: r for r in self.cat.restaurants}
        return self._ri

    @property
    def _attr_idx(self) -> dict[str, Any]:
        if not hasattr(self, "_ai"):
            self._ai = {a.id: a for a in self.cat.attractions}
        return self._ai

    def _legs(self, leg: str) -> list[tuple[Any, TransportOption]]:
        if leg == "outbound":
            return list(zip(self.x_out, self.cat.outbound, strict=True))
        return list(zip(self.x_ret, self.cat.inbound, strict=True))

    def _encode(self, c: Any) -> None:
        lit = self._guard(c.id, c.origin, c.describe(), c.kind)
        cat, P = self.cat, self.P
        k = c.kind
        if k == "budget_max":
            limit = c.amount * (P if c.per_person else 1)
            self.m.Add(self.e_total <= limit).OnlyEnforceIf(lit)
        elif k == "depart_after":
            self._zero((x for x, t in self._legs(c.leg) if t.depart_min < c.minutes), lit)
        elif k == "arrive_before":
            self._zero((x for x, t in self._legs(c.leg) if t.arrive_min > c.minutes), lit)
        elif k == "no_red_eye":
            self._zero((x for leg in ("outbound", "return") for x, t in self._legs(leg) if t.red_eye), lit)
        elif k == "max_stops":
            self._zero(
                (
                    x
                    for leg in ("outbound", "return")
                    for x, t in self._legs(leg)
                    if t.mode == "flight" and t.stops > c.stops
                ),
                lit,
            )
        elif k == "transport_modes":
            self._zero(
                (x for leg in ("outbound", "return") for x, t in self._legs(leg) if t.mode not in c.allowed),
                lit,
            )
        elif k == "stay_accessible":
            self._zero((x for x, s in zip(self.x_stay, cat.stays, strict=True) if not s.stay.accessible), lit)
        elif k == "stay_min_rating":
            self._zero(
                (x for x, s in zip(self.x_stay, cat.stays, strict=True) if s.stay.rating < c.rating), lit
            )
        elif k == "stay_kinds":
            self._zero(
                (x for x, s in zip(self.x_stay, cat.stays, strict=True) if s.stay.kind not in c.kinds), lit
            )
        elif k == "stay_rule":
            self._zero(
                (x for x, s in zip(self.x_stay, cat.stays, strict=True) if not getattr(s.stay, c.rule)), lit
            )
        elif k == "stay_amenity":
            self._zero(
                (
                    x
                    for x, s in zip(self.x_stay, cat.stays, strict=True)
                    if (c.amenity in s.stay.amenities) != c.want
                ),
                lit,
            )
        elif k == "stay_max_nightly":
            self._zero(
                (x for x, s in zip(self.x_stay, cat.stays, strict=True) if max(s.nightly) > c.amount), lit
            )
        elif k == "cuisine_min":
            self._at_least(
                (v for (sk, rid), v in self.meal.items() if c.cuisine in self._rest(rid).cuisines),
                c.count,
                lit,
            )
        elif k == "diet_all":
            self._zero((v for (sk, rid), v in self.meal.items() if c.diet not in self._rest(rid).diets), lit)
        elif k == "meal_max_price":
            self._zero((v for (sk, rid), v in self.meal.items() if self._rest(rid).avg_cost > c.amount), lit)
        elif k == "category_min":
            self._at_least(
                (v for (d, aid), v in self.act.items() if self._attr(aid).category == c.category),
                c.count,
                lit,
            )
        elif k == "attractions_accessible":
            self._zero((v for (d, aid), v in self.act.items() if not self._attr(aid).accessible), lit)
        elif k == "activities_per_day":
            for ad in self.sched.activity_days:
                if not ad.full:
                    continue
                vs = [v for v, _ in self.day_vars[ad.day]]
                if vs:
                    self.m.Add(sum(vs) >= c.min).OnlyEnforceIf(lit)
                    self.m.Add(sum(vs) <= c.max).OnlyEnforceIf(lit)
                elif c.min > 0:
                    self.m.AddBoolOr([lit.Not()])
        elif k == "dry_outdoor":
            bad = self.cat.bad_weather_days()
            self._zero(
                (
                    v
                    for (d, aid), v in self.act.items()
                    if not self._attr(aid).indoor and self._date(d) in bad
                ),
                lit,
            )
        elif k in ("include_item", "exclude_item"):
            if not c.item_id:
                return
            vs = self.item_vars(c.item_id)
            if k == "include_item":
                self._at_least(vs, 1, lit)
            else:
                self._zero(vs, lit)

    def item_vars(self, item_id: str) -> list[Any]:
        vs = [x for x, t in zip(self.x_out, self.cat.outbound, strict=True) if t.id == item_id]
        vs += [x for x, t in zip(self.x_ret, self.cat.inbound, strict=True) if t.id == item_id]
        vs += [x for x, s in zip(self.x_stay, self.cat.stays, strict=True) if s.stay.id == item_id]
        vs += [v for (sk, rid), v in self.meal.items() if rid == item_id]
        vs += [v for (d, aid), v in self.act.items() if aid == item_id]
        return vs

    def _encode_force(self, item_id: str) -> None:
        lit = self._guard("force", "user", f"choose {item_id}", "force")
        self._at_least(self.item_vars(item_id), 1, lit)

    # ------------------------------------------------------------------ solve

    def set_objective(self, *, minimize: Any = None, maximize: Any = None) -> None:
        if hasattr(self.m, "ClearObjective"):
            self.m.ClearObjective()
        if minimize is not None:
            self.m.Minimize(minimize)
            return
        if maximize is not None:
            self.m.Maximize(maximize)
            return
        q, extra = self.e_quality, self.e_inconvenience + self.e_weather
        objective = {
            "cheapest": 8 * self.e_total + extra - q,
            "best": self.e_total + extra - 6 * q,
            "balanced": self.e_total + extra - q,
        }[self.intent.objective]
        self.m.Minimize(objective)

    def solve(
        self,
        assume: Iterable[str] | None = None,
        *,
        time_limit: float = 2.0,
        workers: int = 8,
        want_core: bool = False,
    ) -> SolveResult:
        ids = list(self.guards) if assume is None else list(assume)
        self.m.ClearAssumptions()
        self.m.AddAssumptions([self.guards[i].lit for i in ids])
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.num_workers = 1 if want_core else max(1, workers)
        solver.parameters.random_seed = 7
        # Full LP linearization tightens the bound enough to *prove* optimality in tens of
        # milliseconds on week-long trips (vs. timing out at the default level).
        solver.parameters.linearization_level = 2
        t0 = time.perf_counter()
        status = solver.Solve(self.m)
        wall = round((time.perf_counter() - t0) * 1000, 2)
        name = solver.StatusName(status)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            plan = self._extract(solver, name)
            obj, bound = solver.ObjectiveValue(), solver.BestObjectiveBound()
            gap = 0.0 if status == cp_model.OPTIMAL else round(abs(obj - bound) / max(1.0, abs(obj)) * 100, 2)
            return SolveResult(
                name, plan, wall, obj, values={"total": int(solver.Value(self.e_total)), "gapPct": gap}
            )
        core: list[str] = []
        if status == cp_model.INFEASIBLE:
            lit_to_id = {self.guards[i].lit.Index(): i for i in ids}
            core = [lit_to_id[i] for i in solver.SufficientAssumptionsForInfeasibility() if i in lit_to_id]
        return SolveResult(name, None, wall, core=core)

    def optimize_value(
        self, expr: Any, assume: Iterable[str], *, maximize: bool, time_limit: float = 1.0
    ) -> int | None:
        """Best achievable value of `expr` subject to the assumed guards (for fix-it hints)."""
        self.set_objective(maximize=expr) if maximize else self.set_objective(minimize=expr)
        try:
            self.m.ClearAssumptions()
            self.m.AddAssumptions([self.guards[i].lit for i in assume])
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = time_limit
            solver.parameters.num_workers = 8
            solver.parameters.linearization_level = 2
            status = solver.Solve(self.m)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                return round(solver.Value(expr))
            return None
        finally:
            self.set_objective()

    def _extract(self, solver: cp_model.CpSolver, status: str) -> Plan:
        cat = self.cat
        out = next(t for x, t in zip(self.x_out, cat.outbound, strict=True) if solver.Value(x))
        ret = next(t for x, t in zip(self.x_ret, cat.inbound, strict=True) if solver.Value(x))
        stay = next(s for x, s in zip(self.x_stay, cat.stays, strict=True) if solver.Value(x))
        meals = []
        for slot in self.sched.meals:
            for v, r in self.slot_vars[slot.key]:
                if solver.Value(v):
                    meals.append(
                        MealChoice(slot=slot.key, day=slot.day, date=slot.date, meal=slot.meal, restaurant=r)
                    )
        acts = []
        for ad in self.sched.activity_days:
            for v, a in self.day_vars[ad.day]:
                if solver.Value(v):
                    acts.append(ActivityChoice(day=ad.day, date=ad.date, attraction=a))
        plan = Plan(
            outbound=out,
            inbound=ret,
            stay=stay,
            meals=meals,
            activities=acts,
            travelers=self.P,
            status=status,
            objective_value=solver.ObjectiveValue(),
        )  # type: ignore[arg-type]
        plan.compute_breakdown()
        return plan
