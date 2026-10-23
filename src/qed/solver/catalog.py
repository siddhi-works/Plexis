"""What the partners told us for one request, frozen and fingerprinted."""

from __future__ import annotations

from dataclasses import dataclass, field

from qed.domain.models import Attraction, Restaurant, StayQuote, TransportOption, WeatherDay
from qed.util import digest


@dataclass
class Catalog:
    outbound: list[TransportOption]
    inbound: list[TransportOption]
    stays: list[StayQuote]
    restaurants: list[Restaurant]
    attractions: list[Attraction]
    weather: list[WeatherDay] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # source name -> content digest
    degraded: list[str] = field(default_factory=list)  # e.g. "stays: served from cache (partner unavailable)"
    affinity: dict[str, int] = field(default_factory=dict)  # learned preference bonus by category/cuisine
    highlights: dict[str, object] = field(
        default_factory=dict
    )  # research results; informational, never proven

    def fingerprint(self) -> str:
        return digest(self.sources)

    def bad_weather_days(self) -> set[str]:
        return {w.date.isoformat() for w in self.weather if w.condition in ("rain", "storm", "snow")}

    def snapshot(self) -> dict[str, object]:
        """Everything needed to re-verify a plan offline."""
        return {
            "outbound": [t.model_dump(mode="json") for t in self.outbound],
            "inbound": [t.model_dump(mode="json") for t in self.inbound],
            "stays": [s.model_dump(mode="json") for s in self.stays],
            "restaurants": [r.model_dump(mode="json") for r in self.restaurants],
            "attractions": [a.model_dump(mode="json") for a in self.attractions],
            "weather": [w.model_dump(mode="json") for w in self.weather],
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, list[dict[str, object]]]) -> Catalog:
        return cls(
            outbound=[TransportOption.model_validate(x) for x in snap["outbound"]],
            inbound=[TransportOption.model_validate(x) for x in snap["inbound"]],
            stays=[StayQuote.model_validate(x) for x in snap["stays"]],
            restaurants=[Restaurant.model_validate(x) for x in snap["restaurants"]],
            attractions=[Attraction.model_validate(x) for x in snap["attractions"]],
            weather=[WeatherDay.model_validate(x) for x in snap.get("weather", [])],
        )
