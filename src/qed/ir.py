"""The intent IR: what a spoken request compiles to.

A `TripIntent` is a small typed program. Every constraint records *where it
came from* (the user's words, their saved profile, a commonsense rule, or a
safety policy) and, for user constraints, the exact span of the utterance that
justifies it. That provenance is what lets Plexis reject a hallucinated
constraint and explain any decision back to the words that caused it.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from qed.domain.amenities import LABELS, Amenity
from qed.domain.models import Diet, Mode, StayKind, StayRule
from qed.util import fmt_minutes, fmt_money

Origin = Literal["user", "profile", "learned", "commonsense", "default", "policy"]
Leg = Literal["outbound", "return"]
Objective = Literal["cheapest", "best", "balanced"]


class Span(BaseModel):
    model_config = ConfigDict(frozen=True)
    start: int
    end: int
    text: str


class _C(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    origin: Origin = "user"
    span: Span | None = None
    because: str | None = Field(
        default=None, description="For profile/policy constraints: the saved fact or rule."
    )

    def describe(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class BudgetMax(_C):
    kind: Literal["budget_max"] = "budget_max"
    amount: int
    per_person: bool = False

    def describe(self) -> str:
        return f"total cost at most {fmt_money(self.amount)}" + (" per person" if self.per_person else "")


class DepartAfter(_C):
    kind: Literal["depart_after"] = "depart_after"
    leg: Leg = "outbound"
    minutes: int

    def describe(self) -> str:
        return f"{self.leg} departs at or after {fmt_minutes(self.minutes)}"


class ArriveBefore(_C):
    kind: Literal["arrive_before"] = "arrive_before"
    leg: Leg = "outbound"
    minutes: int

    def describe(self) -> str:
        return f"{self.leg} arrives by {fmt_minutes(self.minutes)}"


class NoRedEye(_C):
    kind: Literal["no_red_eye"] = "no_red_eye"

    def describe(self) -> str:
        return "no overnight (red-eye) departures"


class MaxStops(_C):
    kind: Literal["max_stops"] = "max_stops"
    stops: int

    def describe(self) -> str:
        return "nonstop only" if self.stops == 0 else f"at most {self.stops} stop(s)"


class TransportModes(_C):
    kind: Literal["transport_modes"] = "transport_modes"
    allowed: list[Mode]

    def describe(self) -> str:
        return "travel by " + " or ".join(self.allowed)


class StayAccessible(_C):
    kind: Literal["stay_accessible"] = "stay_accessible"

    def describe(self) -> str:
        return "step-free, wheelchair-accessible lodging"


class StayMinRating(_C):
    kind: Literal["stay_min_rating"] = "stay_min_rating"
    rating: float

    def describe(self) -> str:
        return f"lodging rated {self.rating:.1f}+"


class StayKinds(_C):
    kind: Literal["stay_kinds"] = "stay_kinds"
    kinds: list[StayKind]

    def describe(self) -> str:
        if set(self.kinds) == {"hotel_room", "entire_home", "private_room"}:
            return "a private room (not shared)"
        if self.kinds == ["shared_room"]:
            return "a shared room or dorm"
        return "lodging type: " + " or ".join(k.replace("_", " ") for k in self.kinds)


class StayRuleC(_C):
    kind: Literal["stay_rule"] = "stay_rule"
    rule: StayRule

    def describe(self) -> str:
        return {
            "pets_allowed": "pets allowed",
            "children_allowed": "children welcome",
            "smoke_free": "smoke-free",
            "parties_allowed": "events allowed",
            "air_conditioned": "room with air conditioning",
            "no_air_conditioning": "room without air conditioning",
        }[self.rule]


class StayAmenity(_C):
    kind: Literal["stay_amenity"] = "stay_amenity"
    amenity: Amenity
    want: bool = True

    def describe(self) -> str:
        label = LABELS[self.amenity]
        if self.want:
            return label
        return "no " + label.removeprefix("a ").removeprefix("an ")


class StayMaxNightly(_C):
    kind: Literal["stay_max_nightly"] = "stay_max_nightly"
    amount: int

    def describe(self) -> str:
        return f"lodging at most {fmt_money(self.amount)} per night"


class CuisineMin(_C):
    kind: Literal["cuisine_min"] = "cuisine_min"
    cuisine: str
    count: int = 1

    def describe(self) -> str:
        return f"at least {self.count} {self.cuisine} meal(s)"


class DietAll(_C):
    kind: Literal["diet_all"] = "diet_all"
    diet: Diet

    def describe(self) -> str:
        return f"every meal {self.diet.replace('_', '-')} friendly"


class MealMaxPrice(_C):
    kind: Literal["meal_max_price"] = "meal_max_price"
    amount: int

    def describe(self) -> str:
        return f"meals at most {fmt_money(self.amount)} per person"


class CategoryMin(_C):
    kind: Literal["category_min"] = "category_min"
    category: str
    count: int = 1

    def describe(self) -> str:
        return f"at least {self.count} {self.category} visit(s)"


class AttractionsAccessible(_C):
    kind: Literal["attractions_accessible"] = "attractions_accessible"

    def describe(self) -> str:
        return "wheelchair-accessible activities"


class ActivitiesPerDay(_C):
    kind: Literal["activities_per_day"] = "activities_per_day"
    min: int = 1
    max: int = 2

    def describe(self) -> str:
        return f"{self.min}-{self.max} activities per full day"


class IncludeItem(_C):
    kind: Literal["include_item"] = "include_item"
    name: str
    item_id: str | None = None

    def describe(self) -> str:
        return f"include {self.name}"


class ExcludeItem(_C):
    kind: Literal["exclude_item"] = "exclude_item"
    name: str
    item_id: str | None = None

    def describe(self) -> str:
        return f"avoid {self.name}"


class DryOutdoor(_C):
    """Outdoor activities only on days without rain or storms in the forecast."""

    kind: Literal["dry_outdoor"] = "dry_outdoor"

    def describe(self) -> str:
        return "outdoor activities only on dry days"


Constraint = Annotated[
    BudgetMax
    | DepartAfter
    | ArriveBefore
    | NoRedEye
    | MaxStops
    | TransportModes
    | StayAccessible
    | StayMinRating
    | StayKinds
    | StayRuleC
    | StayAmenity
    | StayMaxNightly
    | CuisineMin
    | DietAll
    | MealMaxPrice
    | CategoryMin
    | AttractionsAccessible
    | ActivitiesPerDay
    | IncludeItem
    | ExcludeItem
    | DryOutdoor,
    Field(discriminator="kind"),
]

CONSTRAINT_KINDS: dict[str, type[_C]] = {
    cls.model_fields["kind"].default: cls
    for cls in (
        BudgetMax,
        DepartAfter,
        ArriveBefore,
        NoRedEye,
        MaxStops,
        TransportModes,
        StayAccessible,
        StayMinRating,
        StayKinds,
        StayRuleC,
        StayAmenity,
        StayMaxNightly,
        CuisineMin,
        DietAll,
        MealMaxPrice,
        CategoryMin,
        AttractionsAccessible,
        ActivitiesPerDay,
        IncludeItem,
        ExcludeItem,
        DryOutdoor,
    )
}


class FixIt(BaseModel):
    """A verified edit that makes an infeasible request solvable (like a compiler fix-it hint)."""

    id: str
    constraint_id: str
    action: Literal["relax", "drop", "extend"]
    message: str
    patch: dict[str, Any] = Field(
        default_factory=dict, description="Field updates applied to the constraint."
    )
    resulting_cost: int | None = None
    verified: bool = False


class Diagnostic(BaseModel):
    severity: Literal["error", "warning", "note"]
    code: str
    message: str
    span: Span | None = None
    constraint_ids: list[str] = Field(default_factory=list)
    fixits: list[FixIt] = Field(default_factory=list)


class TripIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utterance: str
    origin: str | None = None
    destination: str | None = None
    start_date: date | None = None
    nights: int | None = None
    travelers: int = 1
    objective: Objective = "balanced"
    constraints: list[Constraint] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    provenance: dict[str, Span] = Field(default_factory=dict, description="Field name -> utterance span.")
    parser: str = "rules"
    coverage: float = 1.0
    traveler_names: list[str] = Field(default_factory=list)

    def missing(self) -> list[str]:
        out = []
        if not self.origin:
            out.append("origin")
        if not self.destination:
            out.append("destination")
        if not self.start_date:
            out.append("start_date")
        if not self.nights:
            out.append("nights")
        return out

    def renumber(self) -> TripIntent:
        """Give every constraint a stable id (c1, c2, ...) in declaration order."""
        for i, c in enumerate(self.constraints, start=1):
            c.id = f"c{i}"
        return self

    def by_id(self, cid: str) -> Any:
        return next((c for c in self.constraints if c.id == cid), None)
