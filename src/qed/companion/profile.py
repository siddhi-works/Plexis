"""Household memory: the "family member" part of Plexis.

Facts are only stored after explicit consent, can be listed and forgotten at
any time, and every fact that influences a plan shows up in the certificate
as a constraint with origin "profile" and the sentence that created it. A
lightweight preference model (category and cuisine affinities) nudges the
objective toward what the household enjoyed before; it never overrides a rule.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, Field

from qed.compiler.rules import CATEGORY_MAP, CUISINE_MAP
from qed.domain.amenities import NEGATE, SYNONYMS
from qed.domain.world import CITY_ALIASES
from qed.ir import (
    AttractionsAccessible,
    DepartAfter,
    DietAll,
    MaxStops,
    StayAccessible,
    StayAmenity,
    StayKinds,
    StayRuleC,
    TripIntent,
)


class Fact(BaseModel):
    id: str
    person: str  # "me", a name, or "household"
    kind: (
        str  # mobility | diet | pet | home | early_flights | nonstop | room_ac | room_kind | likes | dislikes
    )
    value: str = ""
    text: str
    created_at: str


class Household(BaseModel):
    members: list[str] = Field(default_factory=lambda: ["me"])
    facts: list[Fact] = Field(default_factory=list)
    affinity: dict[str, int] = Field(default_factory=dict)
    trips_booked: int = 0


PERSON = r"(i|i'm|i am|me|my|we|we're|our|[a-z][a-z]+)"


def interpret(statement: str) -> list[dict[str, str]]:
    """Turn a sentence into candidate facts (not yet stored)."""
    s = statement.strip()
    low = s.lower()
    out: list[dict[str, str]] = []

    def who(token: str) -> str:
        token = token.lower()
        if token in ("i", "i'm", "i am", "me", "my"):
            return "me"
        if token in ("we", "we're", "our"):
            return "household"
        return token.capitalize()

    if m := re.search(
        rf"\b{PERSON}\s+(?:uses|is in|needs)\s+a\s+wheelchair|\b{PERSON}\s+(?:can'?t|cannot) (?:do|climb) stairs",
        low,
    ):
        out.append({"person": who(m.group(1) or m.group(2)), "kind": "mobility", "value": "wheelchair"})
    for diet, pat in (
        ("vegan", r"vegan"),
        ("vegetarian", r"vegetarian|veggie"),
        ("gluten_free", r"gluten[\s-]free|celiac"),
    ):
        if m := re.search(
            rf"\b(?:(i'm|i am|we're|we are)|{PERSON}\s+(?:is|are|eat|eats|only eats?))\s+(?:a\s+)?(?:{pat})\b",
            low,
        ):
            out.append({"person": who(m.group(1) or m.group(2)), "kind": "diet", "value": diet})
    if m := re.search(
        r"\b(?:we|i)\s+(?:always\s+)?(?:travel|go) with (?:our|my) (dog|cat|pet)|\b(?:we|i) have a (dog|cat)",
        low,
    ):
        out.append({"person": "household", "kind": "pet", "value": m.group(1) or m.group(2)})
    alias = "|".join(sorted((re.escape(a) for a in CITY_ALIASES), key=len, reverse=True))
    if m := re.search(
        rf"\b(?:i|we)\s+(?:live|are based|'re based) in ({alias})\b|\bhome is ({alias})\b", low
    ):
        out.append({"person": "household", "kind": "home", "value": CITY_ALIASES[m.group(1) or m.group(2)]})
    if re.search(r"\b(?:hate|don'?t like|avoid|no) (?:early|morning) flights\b", low):
        out.append({"person": "me", "kind": "early_flights", "value": "540"})
    if re.search(r"\b(?:prefer|only|always) (?:non[\s-]?stop|direct)\b", low):
        out.append({"person": "me", "kind": "nonstop", "value": "0"})
    ac = r"(?:a\.?c\.?|air[\s-]?con\w*)(?=\W|$)"
    if re.search(rf"without air[\s-]?condition|\bno[\s-]+{ac}|\bnon[\s-]?a\.?c\b", low):
        out.append({"person": "household", "kind": "room_ac", "value": "no"})
    elif re.search(
        rf"\bwith air[\s-]?condition|\b(?:need|want|must have)\s+{ac}|\bair[\s-]?conditioned\b", low
    ):
        out.append({"person": "household", "kind": "room_ac", "value": "yes"})
    if re.search(r"\bnot shared\b|\bno shared rooms?\b|\bprivate rooms?\b|\b(?:our|my) own room\b", low):
        out.append({"person": "household", "kind": "room_kind", "value": "private"})
    elif re.search(r"\bshared rooms?\b|\bdorms?\b|\bhostels?\b|\bsharing (?:a )?room\b", low):
        out.append({"person": "household", "kind": "room_kind", "value": "shared"})
    for amenity, syn in SYNONYMS.items():
        if re.search(rf"\b{NEGATE}\s+(?:an?\s+|the\s+|any\s+)?(?:{syn})(?=\W|$)", low):
            out.append({"person": "household", "kind": "amenity", "value": f"{amenity}:no"})
        elif re.search(rf"\b(?:{syn})(?=\W|$)", low) and re.search(
            r"\b(?:prefer|always|need|want|love|like|must|remember|usually)\b", low
        ):
            out.append({"person": "household", "kind": "amenity", "value": f"{amenity}:yes"})
    for verb, kind in (
        ("love|like|enjoy|adore", "likes"),
        ("hate|dislike|don'?t like|can'?t stand", "dislikes"),
    ):
        for m in re.finditer(rf"\b(?:we|i)\s+(?:really\s+)?(?:{verb})\s+([a-z ]+?)(?:[,.!]|$| and )", low):
            topic = m.group(1).strip().rstrip("s")
            key = (
                CATEGORY_MAP.get(topic)
                or CATEGORY_MAP.get(topic + "s")
                or CUISINE_MAP.get(topic)
                or (
                    topic
                    if topic
                    in {
                        "thai",
                        "italian",
                        "mexican",
                        "japanese",
                        "indian",
                        "chinese",
                        "mediterranean",
                        "french",
                        "korean",
                        "vietnamese",
                        "seafood",
                        "american",
                    }
                    else None
                )
            )
            if key:
                out.append({"person": "household", "kind": kind, "value": key})
    return out


class ProfileStore:
    def __init__(self, state_dir: Path, *, seed_demo: bool = False) -> None:
        self.path = state_dir / "household.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if self.path.exists():
            self.data = Household.model_validate(orjson.loads(self.path.read_bytes()))
        else:
            self.data = Household()
            if seed_demo:
                for sentence in (
                    "We live in Seattle",
                    "Grandma uses a wheelchair",
                    "Maya is vegetarian",
                    "We love museums",
                ):
                    for cand in interpret(sentence):
                        self.remember(cand, sentence)
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.write_bytes(orjson.dumps(self.data.model_dump(), option=orjson.OPT_INDENT_2))

    def remember(self, cand: dict[str, str], text: str) -> Fact:
        fact = Fact(
            id=f"f-{uuid.uuid4().hex[:6]}",
            text=text.strip(),
            created_at=datetime.now(UTC).date().isoformat(),
            **cand,
        )

        def same(f: Fact) -> bool:  # a newer statement replaces the older one about the same thing
            if f.person != fact.person or f.kind != fact.kind or f.kind in ("likes", "dislikes"):
                return False
            return f.kind != "amenity" or f.value.split(":")[0] == fact.value.split(":")[0]

        self.data.facts = [f for f in self.data.facts if not same(f)]
        self.data.facts.append(fact)
        if fact.person not in ("me", "household") and fact.person not in self.data.members:
            self.data.members.append(fact.person)
        if fact.kind in ("likes", "dislikes"):
            self.data.affinity[fact.value] = self.data.affinity.get(fact.value, 0) + (
                12 if fact.kind == "likes" else -20
            )
        self.save()
        return fact

    def forget(self, fact_id_or_text: str) -> list[Fact]:
        key = fact_id_or_text.lower().strip()
        gone = [f for f in self.data.facts if f.id == key or key in f.text.lower() or key == f.person.lower()]
        self.data.facts = [f for f in self.data.facts if f not in gone]
        for f in gone:
            if f.kind in ("likes", "dislikes"):
                self.data.affinity.pop(f.value, None)
        self.save()
        return gone

    def forget_all(self) -> int:
        n = len(self.data.facts)
        self.data = Household()
        self.save()
        return n

    @property
    def people(self) -> list[str]:
        return [m for m in self.data.members if m != "me"]

    def home(self) -> str | None:
        return next((f.value for f in self.data.facts if f.kind == "home"), None)

    def learn_from_booking(self, categories: list[str], cuisines: list[str]) -> None:
        for key in categories + cuisines:
            self.data.affinity[key] = min(30, self.data.affinity.get(key, 0) + 2)
        self.data.trips_booked += 1
        self.save()

    def apply(self, intent: TripIntent) -> list[str]:
        """Add constraints from facts about people on this trip. Returns notes for the response."""
        notes: list[str] = []
        travelling = {"me", "household", *intent.traveler_names}
        existing = {
            (c.kind, getattr(c, "diet", None), getattr(c, "rule", None), getattr(c, "amenity", None))
            for c in intent.constraints
        }
        for f in self.data.facts:
            if f.person not in travelling:
                continue
            added: list[Any] = []
            if f.kind == "mobility":
                added = [StayAccessible(), AttractionsAccessible()]
            elif f.kind == "diet":
                added = [DietAll(diet=f.value)]  # type: ignore[arg-type]
            elif f.kind == "pet" and re.search(r"\b(?:dog|cat|pet|pup)\b", intent.utterance.lower()):
                added = [StayRuleC(rule="pets_allowed")]
            elif f.kind == "early_flights":
                added = [DepartAfter(leg="outbound", minutes=int(f.value))]
            elif f.kind == "nonstop":
                added = [MaxStops(stops=0)]
            elif f.kind == "room_ac" and not any(
                getattr(c, "rule", "") in ("air_conditioned", "no_air_conditioning")
                for c in intent.constraints
            ):
                added = [StayRuleC(rule="no_air_conditioning" if f.value == "no" else "air_conditioned")]
            elif f.kind == "room_kind" and not any(c.kind == "stay_kinds" for c in intent.constraints):
                added = [
                    StayKinds(
                        kinds=["shared_room"]
                        if f.value == "shared"
                        else ["hotel_room", "entire_home", "private_room"]
                    )
                ]
            elif f.kind == "amenity":
                name, _, want = f.value.partition(":")
                if not any(getattr(c, "amenity", None) == name for c in intent.constraints):
                    added = [StayAmenity(amenity=name, want=want != "no")]  # type: ignore[arg-type]
            for c in added:
                sig = (
                    c.kind,
                    getattr(c, "diet", None),
                    getattr(c, "rule", None),
                    getattr(c, "amenity", None),
                )
                if sig in existing:
                    continue
                c.origin, c.because = "profile", f"{f.text} (saved {f.created_at})"
                intent.constraints.append(c)
                existing.add(sig)
                notes.append(f"{c.describe()} because {f.text.rstrip('.')}")
        if not intent.origin and (home := self.home()):
            intent.origin = home
            notes.append(f"leaving from {home} (your saved home city)")
        return notes

    def public(self) -> dict[str, Any]:
        return {
            "members": self.data.members,
            "facts": [f.model_dump() for f in self.data.facts],
            "affinity": self.data.affinity,
            "tripsBooked": self.data.trips_booked,
        }
