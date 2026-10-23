"""Compile errors for real life.

When a request has no valid plan, Plexis:
1. takes the infeasibility core CP-SAT reports,
2. shrinks it to a *minimal* unsatisfiable subset (MUS): every rule in it is
   necessary for the conflict, so the explanation names nothing irrelevant,
3. computes fix-its: for each rule in the MUS, the smallest relaxation that
   restores feasibility (e.g. the exact budget needed), re-solved to prove it
   works with everything else you asked for.

`why_not` answers contrastive questions ("why not the Harbor Hotel?") by
forcing that choice and diagnosing what breaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qed.ir import Diagnostic, FixIt, TripIntent
from qed.solver.catalog import Catalog
from qed.solver.cpsat import TripModel
from qed.util import fmt_minutes, fmt_money


@dataclass
class Conflict:
    mus: list[str]
    texts: dict[str, str]
    origins: dict[str, str]
    fixits: list[FixIt] = field(default_factory=list)
    solve_calls: int = 0


def minimal_core(model: TripModel, core: list[str], *, time_limit: float = 0.5) -> tuple[list[str], int]:
    """Deletion-based MUS extraction over assumption literals."""
    calls = 0
    if not core:
        core = list(model.guards)
    mus = list(core)
    i = 0
    while i < len(mus):
        trial = mus[:i] + mus[i + 1 :]
        res = model.solve(trial, time_limit=time_limit, want_core=True)
        calls += 1
        if res.status == "INFEASIBLE":
            mus = [g for g in trial if g in set(res.core)] or trial
            i = 0 if len(mus) < len(trial) else i
            continue
        i += 1
    return mus, calls


def _relaxation(model: TripModel, gid: str, others: list[str]) -> tuple[str, dict[str, Any], str] | None:
    """For a numeric rule, the tightest value that works with every other rule."""
    c = model.intent.by_id(gid)
    if c is None:
        return None
    m, cat = model, model.cat
    if c.kind == "budget_max":
        need = m.optimize_value(m.e_total, others, maximize=False)
        if need is None:
            return None
        amount = -(-need // m.P) if c.per_person else need
        return (
            "relax",
            {"amount": amount},
            f"raise the budget to {fmt_money(amount)}{' per person' if c.per_person else ''} (+{fmt_money(amount - c.amount)})",
        )
    if c.kind == "depart_after":
        legs = m._legs(c.leg)
        expr = sum(x * t.depart_min for x, t in legs)
        best = m.optimize_value(expr, others, maximize=True)
        if best is None:
            return None
        return ("relax", {"minutes": best}, f"allow a {c.leg} departure at {fmt_minutes(best)}")
    if c.kind == "arrive_before":
        expr = sum(x * t.arrive_min for x, t in m._legs(c.leg))
        best = m.optimize_value(expr, others, maximize=False)
        if best is None:
            return None
        return ("relax", {"minutes": best}, f"accept a {c.leg} arrival at {fmt_minutes(best)}")
    if c.kind == "stay_min_rating":
        expr = sum(x * int(s.stay.rating * 10) for x, s in zip(m.x_stay, cat.stays, strict=True))
        best = m.optimize_value(expr, others, maximize=True)
        if best is None:
            return None
        return ("relax", {"rating": best / 10}, f"accept lodging rated {best / 10:.1f}")
    if c.kind == "stay_max_nightly":
        expr = sum(x * max(s.nightly) for x, s in zip(m.x_stay, cat.stays, strict=True))
        best = m.optimize_value(expr, others, maximize=False)
        if best is None:
            return None
        return ("relax", {"amount": best}, f"allow lodging up to {fmt_money(best)} a night")
    if c.kind in ("cuisine_min", "category_min"):
        if c.kind == "cuisine_min":
            vs = [v for (sk, rid), v in m.meal.items() if c.cuisine in m._rest(rid).cuisines]
            label = f"{c.cuisine} meal"
        else:
            vs = [v for (d, aid), v in m.act.items() if m._attr(aid).category == c.category]
            label = f"{c.category} visit"
        if not vs:
            return None
        best = m.optimize_value(sum(vs), others, maximize=True)
        if not best:
            return None
        return (
            "relax",
            {"count": best},
            f"plan {best} {label}{'s' if best != 1 else ''} instead of {c.count}",
        )
    if c.kind == "max_stops":
        expr = sum(
            x * t.stops for leg in ("outbound", "return") for x, t in m._legs(leg) if t.mode == "flight"
        )
        best = m.optimize_value(expr, others, maximize=False)
        if best is None:
            return None
        return ("relax", {"stops": max(1, c.stops + 1)}, f"allow up to {max(1, c.stops + 1)} stop(s)")
    return None


def _patched(intent: TripIntent, gid: str, patch: dict[str, Any] | None) -> TripIntent:
    new = intent.model_copy(deep=True)
    if patch is None:
        new.constraints = [c for c in new.constraints if c.id != gid]
    else:
        for c in new.constraints:
            if c.id == gid:
                for k, v in patch.items():
                    setattr(c, k, v)
    return new


def explain_conflict(
    model: TripModel, core: list[str], *, max_fixits: int = 4, mus: list[str] | None = None
) -> Conflict:
    if mus is None:
        mus, calls = minimal_core(model, core)
    else:
        calls = 0
    conflict = Conflict(
        mus=mus,
        texts={g: model.guards[g].text for g in mus},
        origins={g: model.guards[g].origin for g in mus},
        solve_calls=calls,
    )
    candidates: list[tuple[float, FixIt]] = []
    for gid in mus:
        guard = model.guards[gid]
        if guard.origin == "commonsense" or gid == "force":
            continue
        others = [g for g in model.guards if g != gid]
        relax = _relaxation(model, gid, others)
        c = model.intent.by_id(gid)
        if relax is not None:
            action, patch, msg = relax
            candidate = _patched(model.intent, gid, patch)
        else:
            action, patch, msg = "drop", None, _drop_message(c, guard.text)
            candidate = _patched(model.intent, gid, None)
        # Prove the fix: re-solve the whole request with only this change.
        check = TripModel(candidate, model.cat).solve(time_limit=1.0)
        calls += 1
        if check.plan is None:
            continue
        disruption = _disruption(c, patch) if c is not None else 1.0
        disruption += {"default": 0.0, "profile": 0.15, "user": 0.3}.get(guard.origin, 0.3)
        candidates.append(
            (
                disruption,
                FixIt(
                    id=f"fx-{gid}",
                    constraint_id=gid,
                    action=action,
                    message=msg,
                    patch=patch or {},
                    resulting_cost=check.plan.total,
                    verified=True,
                ),
            )
        )
    conflict.fixits = [f for _, f in sorted(candidates, key=lambda x: (x[0], x[1].resulting_cost or 0))][
        :max_fixits
    ]
    conflict.solve_calls = calls
    return conflict


def _drop_message(c: Any, text: str) -> str:
    """Say what dropping a rule allows, the way a person would."""
    from qed.domain.amenities import LABELS

    if c is None:
        return f"skip “{text}”"
    if c.kind == "stay_amenity":
        return (
            f"allow a place without {LABELS[c.amenity].removeprefix('a ').removeprefix('an ')}"
            if c.want
            else f"allow a place with {LABELS[c.amenity].removeprefix('a ').removeprefix('an ')}"
        )
    if c.kind == "stay_rule" and c.rule in ("air_conditioned", "no_air_conditioning"):
        return "allow a room without AC" if c.rule == "air_conditioned" else "allow a room with AC"
    if c.kind == "stay_kinds":
        return "allow any room type"
    return f"skip “{text}”"


def _disruption(c: Any, patch: dict[str, Any] | None) -> float:
    if patch is None:
        return 1.0
    if "amount" in patch and getattr(c, "amount", 0):
        return abs(patch["amount"] - c.amount) / max(1, c.amount)
    if "minutes" in patch:
        return abs(patch["minutes"] - c.minutes) / 240
    if "rating" in patch:
        return abs(patch["rating"] - c.rating) / 2
    if "count" in patch:
        return abs(patch["count"] - c.count) / max(1, c.count)
    return 0.5


def conflict_diagnostic(conflict: Conflict) -> Diagnostic:
    parts = [conflict.texts[g] for g in conflict.mus]
    if len(parts) == 1:
        message = f"No plan can satisfy: {parts[0]}."
    else:
        message = "These requirements cannot all hold together: " + "; ".join(parts) + "."
    return Diagnostic(
        severity="error",
        code="QED-E100",
        message=message,
        constraint_ids=conflict.mus,
        fixits=conflict.fixits,
    )


@dataclass
class WhyNot:
    item_id: str
    feasible: bool
    blocking: list[str]  # guard texts that rule the item out
    cost_delta: int | None
    quality_note: str
    message: str


def why_not(intent: TripIntent, catalog: Catalog, item_id: str, chosen_total: int, item_name: str) -> WhyNot:
    model = TripModel(intent, catalog, force=item_id)
    if not model.item_vars(item_id):
        return WhyNot(
            item_id,
            False,
            ["it isn't available on your dates"],
            None,
            "",
            f"{item_name} isn't available for your dates, so it couldn't be chosen.",
        )
    res = model.solve(time_limit=1.5)
    if res.plan is not None:
        delta = res.plan.total - chosen_total
        if delta > 0:
            msg = f"{item_name} fits all your requirements, but the best plan with it costs {fmt_money(delta)} more."
        else:
            msg = (
                f"{item_name} also fits, and costs about the same ({fmt_money(abs(delta))} "
                f"{'less' if delta < 0 else 'difference'}); the chosen plan scored better overall on ratings, "
                "travel time and weather."
            )
        return WhyNot(item_id, True, [], delta, "", msg)
    core = model.solve(time_limit=1.5, want_core=True).core
    mus, _ = minimal_core(model, core)
    blocking = [model.guards[g].text for g in mus if g != "force"]
    msg = f"{item_name} can't be chosen because it would break: " + "; ".join(blocking) + "."
    return WhyNot(item_id, False, blocking, None, "", msg)
