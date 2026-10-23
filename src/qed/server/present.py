"""Shape engine results for Alexa+ hosts: structured data first, short speech second.

Alexa+ composes its own spoken reply from structuredContent, so everything a
host could need (itinerary, prices, rules and their provenance, the proof) is
here as data. On-screen and spoken content are generated from the same signed
certificate, which is how they are guaranteed to match.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from qed.domain.world import city_name
from qed.engine import CompileResult, Engine
from qed.util import WEEKDAYS, fmt_minutes, fmt_money

MEAL_TIME = {"breakfast": "8:30 am", "lunch": "12:30 pm", "dinner": "7:00 pm"}
ACT_TIME = ["10:00 am", "2:30 pm", "4:30 pm"]


def plan_data(res: CompileResult, engine: Engine) -> dict[str, Any]:
    sp = res.stored
    it = res.intent
    data: dict[str, Any] = {
        "notes": res.notes,
        "diagnostics": [d.model_dump(mode="json") for d in (it.diagnostics if it else [])],
    }
    if it is not None:
        data["trip"] = None
    if sp is None or it is None:
        return data
    cert = sp.certificate
    entry = sp.entry
    data["proof"] = {
        "checks": len(cert.get("checks", [])),
        "passed": sum(1 for c in cert.get("checks", []) if c["passed"]),
        "ledgerIndex": entry.index if entry else None,
        "certHash": entry.cert_hash if entry else None,
        "signature": entry.signature if entry else None,
        "keyId": engine.signer.key_id,
        "solver": cert["solver"]["engine"],
        "solverStatus": cert["solver"]["status"],
        "z3": f"{cert['crossCheck']['engine']} ({cert['crossCheck']['result']})",
        "parser": it.parser,
        "coverage": it.coverage,
    }
    data["untrusted"] = cert["untrustedText"]["findings"]
    data["highlights"] = sp.catalog.highlights or None
    data["degraded"] = cert["data"]["degraded"]
    data["partnerCalls"] = res.partner_calls
    data["weatherAdvice"] = res.weather_advice
    budget = next((c for c in it.constraints if c.kind == "budget_max"), None)
    data["budget"] = (budget.amount * (it.travelers if budget.per_person else 1)) if budget else None
    assert it.start_date and it.nights and it.origin and it.destination
    data["trip"] = {
        "origin": it.origin,
        "originName": city_name(it.origin),
        "destination": it.destination,
        "destName": city_name(it.destination),
        "startDate": it.start_date.isoformat(),
        "endDate": (it.start_date + timedelta(days=it.nights)).isoformat(),
        "nights": it.nights,
        "travelers": it.travelers,
        "travelerNames": it.traveler_names,
        "objective": it.objective,
    }
    if sp.conflict is not None:
        data["trip"] = data["trip"] | {"impossible": True}
        data["conflict"] = cert["conflict"]
        return data
    plan = sp.plan
    assert plan is not None
    checks = {c["id"]: c for c in cert.get("checks", [])}
    data["rules"] = [
        {
            "id": c.id,
            "text": c.describe(),
            "origin": c.origin,
            "words": c.span.text if c.span else None,
            "because": c.because,
            "passed": checks.get(c.id, {}).get("passed", False),
            "evidence": checks.get(c.id, {}).get("evidence"),
        }
        for c in it.constraints
    ]
    data["rules"] += [
        {
            "id": c["id"],
            "text": c["label"],
            "origin": c["origin"],
            "words": None,
            "because": None,
            "passed": c["passed"],
            "evidence": c["evidence"],
        }
        for c in cert.get("checks", [])
        if c["origin"] in ("commonsense", "structure", "integrity", "arithmetic")
    ]
    data["breakdown"] = plan.breakdown
    weather = {w.date: w for w in sp.catalog.weather}
    icons = {"sunny": "☀️", "cloudy": "☁️", "rain": "🌧️", "storm": "⛈️", "snow": "❄️"}
    days = []
    for d in range(1, it.nights + 2):
        on = it.start_date + timedelta(days=d - 1)
        items: list[dict[str, Any]] = []
        if d == 1:
            t = plan.outbound
            items.append(
                {
                    "time": fmt_minutes(t.depart_min),
                    "kind": t.mode,
                    "title": f"{t.carrier} {t.code} · {t.origin} → {t.destination}",
                    "detail": f"{'nonstop' if t.stops == 0 else f'{t.stops} stop(s)'} · arrives {fmt_minutes(t.arrive_min)}",
                    "price": plan.transport_cost(t),
                    "id": t.id,
                }
            )
            q = plan.stay
            items.append(
                {
                    "time": "check-in",
                    "kind": "stay",
                    "title": q.stay.name,
                    "detail": f"{q.nights} nights · {q.stay.rating}★ · {q.stay.kind.replace('_', ' ')}"
                    f"{' · step-free' if q.stay.accessible else ''} · {'AC' if q.stay.air_conditioned else 'no AC'}"
                    f" · {q.stay.neighborhood}",
                    "price": q.total_price,
                    "id": q.stay.id,
                }
            )
        acts = [a for a in plan.activities if a.day == d]
        for i, a in enumerate(acts):
            items.append(
                {
                    "time": "3:00 pm" if d == 1 else ACT_TIME[min(i, 2)],
                    "kind": "activity",
                    "title": a.attraction.name,
                    "detail": f"{a.attraction.category} · {a.attraction.minutes // 60}h{a.attraction.minutes % 60:02d}"
                    f" · {'indoor' if a.attraction.indoor else 'outdoor'}"
                    f"{' · step-free' if a.attraction.accessible else ''}",
                    "price": a.attraction.price * plan.travelers,
                    "id": a.attraction.id,
                }
            )
        for m in [m for m in plan.meals if m.day == d]:
            items.append(
                {
                    "time": MEAL_TIME[m.meal],
                    "kind": "meal",
                    "title": m.restaurant.name,
                    "detail": f"{m.meal} · {', '.join(m.restaurant.cuisines)}"
                    f"{' · ' + ', '.join(x.replace('_', '-') for x in m.restaurant.diets) if m.restaurant.diets else ''}",
                    "price": m.restaurant.avg_cost * plan.travelers,
                    "id": m.restaurant.id,
                }
            )
        if d == it.nights + 1:
            t = plan.inbound
            items.append(
                {
                    "time": fmt_minutes(t.depart_min),
                    "kind": t.mode,
                    "title": f"{t.carrier} {t.code} · {t.origin} → {t.destination}",
                    "detail": f"{'nonstop' if t.stops == 0 else f'{t.stops} stop(s)'} · arrives {fmt_minutes(t.arrive_min)}",
                    "price": plan.transport_cost(t),
                    "id": t.id,
                }
            )
        items.sort(key=_time_key)
        w = weather.get(on)
        days.append(
            {
                "day": d,
                "date": on.isoformat(),
                "weekday": WEEKDAYS[on.weekday()],
                "weather": None
                if w is None
                else {
                    "condition": w.condition,
                    "icon": icons[w.condition],
                    "highC": w.high_c,
                    "lowC": w.low_c,
                    "source": w.source,
                },
                "items": items,
            }
        )
    data["itinerary"] = days
    return data


def _time_key(item: dict[str, Any]) -> int:
    t = item.get("time", "")
    if t == "check-in":
        return 15 * 60 + 30
    try:
        clock, ampm = t.split(" ")[:2]
        h, m = (int(x) for x in clock.split(":"))
        h = h % 12 + (12 if ampm.startswith("pm") else 0)
        return h * 60 + m + (1440 if "(+" in t else 0)
    except ValueError:
        return 0


def voice_options(res: CompileResult) -> list[str]:
    """At most three short options a voice-only device can read out (Alexa+ guidance: <=5)."""
    sp = res.stored
    if res.status == "infeasible" and sp and sp.conflict:
        return [f.message[0].upper() + f.message[1:] for f in sp.conflict.fixits[:3]]
    if res.status == "planned":
        return ["Book it", "Make it cheaper", "Why this hotel?"]
    return []


def money(n: int) -> str:
    return fmt_money(n)
