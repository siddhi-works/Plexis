"""The concrete itinerary a solver returns."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from qed.domain.models import Attraction, Restaurant, StayQuote, TransportOption


class MealChoice(BaseModel):
    slot: str
    day: int
    date: date
    meal: str
    restaurant: Restaurant


class ActivityChoice(BaseModel):
    day: int
    date: date
    attraction: Attraction


class Plan(BaseModel):
    outbound: TransportOption
    inbound: TransportOption
    stay: StayQuote
    meals: list[MealChoice]
    activities: list[ActivityChoice]
    travelers: int
    status: Literal["OPTIMAL", "FEASIBLE"]
    objective_value: float = 0.0
    breakdown: dict[str, int] = Field(default_factory=dict)

    def transport_cost(self, t: TransportOption) -> int:
        return t.price * self.travelers if t.price_basis == "per_person" else t.price

    def compute_breakdown(self) -> dict[str, int]:
        b = {
            "transport": self.transport_cost(self.outbound) + self.transport_cost(self.inbound),
            "lodging": self.stay.total_price,
            "dining": sum(m.restaurant.avg_cost for m in self.meals) * self.travelers,
            "activities": sum(a.attraction.price for a in self.activities) * self.travelers,
        }
        b["total"] = sum(b.values())
        self.breakdown = b
        return b

    @property
    def total(self) -> int:
        return self.breakdown.get("total") or self.compute_breakdown()["total"]

    def item_ids(self) -> set[str]:
        ids = {self.outbound.id, self.inbound.id, self.stay.stay.id}
        ids |= {m.restaurant.id for m in self.meals}
        ids |= {a.attraction.id for a in self.activities}
        return ids
