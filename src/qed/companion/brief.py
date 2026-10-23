"""The travel brief: what to wear, what to pack, what to eat, how to stay comfortable.

Built from the verified plan plus the weather partner's forecast and the
destination advisory. Health content is deliberately general preparedness
(altitude, heat, sun, flu season, bring your prescriptions) with pointers to
official sources: Plexis does not diagnose or recommend medication.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from qed.domain.models import CityAdvisory, WeatherDay
from qed.solver.plan import Plan
from qed.util import WEEKDAYS

ICONS = {"sunny": "☀️", "cloudy": "☁️", "rain": "🌧️", "storm": "⛈️", "snow": "❄️"}


def _outfit(w: WeatherDay | None, activities: list[str], dinner_style: str) -> str:
    if w is None:
        return "Comfortable layers"
    parts = []
    if w.high_c >= 26:
        parts.append("light breathable clothes and a sun hat")
    elif w.high_c >= 17:
        parts.append("a t-shirt with a light layer")
    elif w.high_c >= 8:
        parts.append("a sweater and a medium jacket")
    else:
        parts.append("a warm coat, hat and gloves")
    if w.condition in ("rain", "storm"):
        parts.append("a waterproof shell and shoes that handle puddles")
    if w.condition == "snow":
        parts.append("insulated waterproof boots")
    if any(a in ("park", "garden", "tour", "viewpoint", "market") for a in activities):
        parts.append("comfortable walking shoes")
    if "theater" in activities or dinner_style == "smart":
        parts.append("something smart-casual for the evening")
    return "; ".join(parts).capitalize()


def build_brief(
    plan: Plan, weather: list[WeatherDay], advisory: CityAdvisory, *, travelers: list[str], city_name: str
) -> dict[str, Any]:
    by_date = {w.date: w for w in weather}
    days = []
    for i, d in enumerate(
        sorted(
            {m.date for m in plan.meals}
            | {a.date for a in plan.activities}
            | {plan.outbound.date, plan.inbound.date}
        )
    ):
        w = by_date.get(d)
        acts = [a.attraction.category for a in plan.activities if a.date == d]
        dinner = next((m.restaurant for m in plan.meals if m.date == d and m.meal == "dinner"), None)
        style = "smart" if dinner and (dinner.avg_cost >= 45 or "french" in dinner.cuisines) else "casual"
        days.append(
            {
                "day": i + 1,
                "date": d.isoformat(),
                "weekday": WEEKDAYS[d.weekday()],
                "weather": None
                if w is None
                else {
                    "condition": w.condition,
                    "icon": ICONS[w.condition],
                    "highC": w.high_c,
                    "lowC": w.low_c,
                    "precipPct": w.precip_pct,
                    "source": w.source,
                },
                "outfit": _outfit(w, acts, style),
            }
        )

    packing: list[str] = [
        "Phone charger and a power bank",
        "ID and booking confirmations (Plexis receipt link)",
    ]
    conds = Counter(w.condition for w in weather)
    lows = [w.low_c for w in weather] or [15]
    highs = [w.high_c for w in weather] or [20]
    if conds["rain"] + conds["storm"]:
        packing.append(
            f"Compact umbrella and a waterproof layer ({conds['rain'] + conds['storm']} wet day(s) expected)"
        )
    if min(lows) <= 5:
        packing.append(f"Warm layers: lows down to {min(lows)}°C")
    if max(highs) >= 27:
        packing.append(f"Sunscreen, sunglasses and a refillable water bottle: highs up to {max(highs)}°C")
    cats = {a.attraction.category for a in plan.activities}
    if cats & {"park", "garden", "viewpoint", "tour"}:
        packing.append("Walking shoes and a small day pack")
    if "theater" in cats:
        packing.append("A smart-casual outfit for the show")
    if plan.stay.stay.pets_allowed and any(w in " ".join(travelers).lower() for w in ("dog", "pet")):
        packing.append("Pet food, leash and vaccination records")
    if len(travelers) > 1:
        packing.append(
            f"Shared items for {len(travelers)} travelers: a first-aid kit and snacks for travel days"
        )

    cuisines = Counter(c for m in plan.meals for c in m.restaurant.cuisines)
    food = {
        "planned": [
            {
                "meal": f"Day {m.day} {m.meal}",
                "restaurant": m.restaurant.name,
                "cuisines": m.restaurant.cuisines,
                "diets": m.restaurant.diets,
            }
            for m in plan.meals
        ],
        "highlights": [f"{c.title()} ({n} meal{'s' if n > 1 else ''})" for c, n in cuisines.most_common(4)],
    }
    dry_days = sum(1 for w in weather if w.condition in ("sunny", "cloudy"))
    return {
        "city": city_name,
        "outlook": {
            "dryDays": dry_days,
            "wetDays": len(weather) - dry_days,
            "confidence": "forecast" if all(w.source == "forecast" for w in weather) else "seasonal outlook",
            "summary": f"{dry_days} of {len(weather)} days look dry in {city_name}.",
        },
        "days": days,
        "packing": packing,
        "food": food,
        "comfort": {
            "notes": advisory.notes,
            "elevationM": advisory.elevation_m,
            "sources": advisory.sources,
            "disclaimer": "General travel-preparedness information, not medical advice. For anything specific to "
            "you, ask your pharmacist or doctor and check official travel notices.",
        },
    }
