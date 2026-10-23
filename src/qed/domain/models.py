"""Typed records exchanged between partner servers and the compiler.

These are the *only* shapes the solver ever reads. Free-text fields such as
`description` are carried for display but are never consulted by any decision
logic, which is what keeps instructions hidden in listings from steering a plan.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["flight", "train", "drive"]
StayKind = Literal["hotel_room", "entire_home", "private_room", "shared_room"]
Meal = Literal["breakfast", "lunch", "dinner"]
Diet = Literal["vegetarian", "vegan", "gluten_free"]
StayRule = Literal[
    "pets_allowed",
    "children_allowed",
    "smoke_free",
    "parties_allowed",
    "air_conditioned",
    "no_air_conditioning",
]

CUISINES = (
    "american",
    "italian",
    "mexican",
    "thai",
    "japanese",
    "indian",
    "chinese",
    "mediterranean",
    "french",
    "korean",
    "vietnamese",
    "seafood",
    "cafe",
)
CATEGORIES = (
    "museum",
    "park",
    "landmark",
    "gallery",
    "market",
    "aquarium",
    "garden",
    "tour",
    "theater",
    "viewpoint",
)


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class City(Record):
    code: str
    name: str
    lat: float
    lon: float
    cost_index: float


class TransportOption(Record):
    id: str
    mode: Mode
    carrier: str
    code: str
    origin: str
    destination: str
    date: date
    depart_min: int = Field(description="Minutes after local midnight on `date`.")
    arrive_min: int = Field(description="Minutes after local midnight on `date`; may exceed 1440.")
    stops: int
    price: int = Field(description="USD, see price_basis.")
    price_basis: Literal["per_person", "per_group"]
    seats_left: int
    red_eye: bool


class Stay(Record):
    id: str
    city: str
    name: str
    kind: StayKind
    rating: float
    accessible: bool
    pets_allowed: bool
    children_allowed: bool
    smoke_free: bool
    parties_allowed: bool
    max_guests: int
    min_nights: int
    neighborhood: str
    nightly_base: int
    air_conditioned: bool = True
    amenities: list[str] = Field(default_factory=list, description="See qed.domain.amenities.")
    description: str = Field(description="UNTRUSTED partner text. Display only.")

    @property
    def no_air_conditioning(self) -> bool:
        """Some travelers ask for rooms without AC (allergies, cost, preference)."""
        return not self.air_conditioned


class StayQuote(Record):
    stay: Stay
    check_in: date
    nights: int
    units: int
    nightly: list[int]
    total_price: int = Field(description="All units, all nights, USD.")


class Restaurant(Record):
    id: str
    city: str
    name: str
    cuisines: list[str]
    avg_cost: int = Field(description="USD per person per meal.")
    rating: float
    diets: list[Diet]
    meals: list[Meal]
    closed_weekday: int | None = Field(default=None, description="0=Monday ... 6=Sunday")
    accessible: bool
    description: str = Field(description="UNTRUSTED partner text. Display only.")


class Attraction(Record):
    id: str
    city: str
    name: str
    category: str
    price: int = Field(description="USD per person.")
    minutes: int
    rating: float
    accessible: bool
    indoor: bool
    closed_weekday: int | None = None
    description: str = Field(description="UNTRUSTED partner text. Display only.")


Condition = Literal["sunny", "cloudy", "rain", "storm", "snow"]


class WeatherDay(Record):
    city: str
    date: date
    condition: Condition
    high_c: int
    low_c: int
    precip_pct: int
    wind_kph: int
    source: Literal["forecast", "climatology"] = Field(
        description="forecast within 10 days of today, otherwise seasonal climatology (lower confidence)."
    )


class TransportStatus(Record):
    option_id: str
    status: Literal["scheduled", "delayed", "cancelled"]
    delay_min: int = 0
    updated_at: str


class CityAdvisory(Record):
    """Comfort and preparedness notes for a destination. General information only."""

    city: str
    elevation_m: int
    notes: list[str]
    sources: list[str]


class PropertyFacts(Record):
    """What a front desk would tell a caller. Served only through the simulated concierge line."""

    stay_id: str
    early_checkin: str
    late_checkout: str
    parking: str
    breakfast: str
    accessibility_detail: str
    cribs: str
    shuttle: str
    quiet_hours: str
