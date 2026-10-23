"""Destination research: real places worth seeing, from Wikivoyage.

The planner's bookable catalog is simulated, but "what's worth seeing in San
Diego?" deserves a real answer. This module reads the "See" section of the
city's Wikivoyage article (CC BY-SA 4.0), extracts named listings with a
one-line description, and caches them on disk. With no network it falls back
to a small curated list of landmarks. Results are labelled as unverified
research: they inform suggestions, never the proof.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://en.wikivoyage.org/w/api.php"
# Wikimedia asks automated clients to identify themselves (https://w.wiki/4wJS).
USER_AGENT = "QEDTripAgent/0.1 (https://github.com/qed-trips/qed; Alexa+ hackathon demo) httpx"
PAGES = {
    "SEA": "Seattle",
    "SFO": "San Francisco",
    "LAX": "Los Angeles",
    "SAN": "San Diego",
    "PDX": "Portland (Oregon)",
    "DEN": "Denver",
    "ORD": "Chicago",
    "AUS": "Austin",
    "JFK": "New York City",
    "BOS": "Boston",
    "MIA": "Miami",
    "BNA": "Nashville",
    "LAS": "Las Vegas",
    "PHX": "Phoenix",
}
TAGLINES = {
    "SEA": "Pike Place Market, the waterfront and mountain views",
    "SFO": "the Golden Gate, cable cars and Alcatraz",
    "LAX": "beaches, Hollywood and the Getty",
    "SAN": "beaches, Balboa Park and the zoo",
    "PDX": "food carts, gardens and day trips to the Gorge",
    "DEN": "the Rockies, Red Rocks and craft breweries",
    "ORD": "architecture, the lakefront and great museums",
    "AUS": "live music, Tex-Mex and swimming holes",
    "JFK": "Broadway, Central Park and world-class museums",
    "BOS": "the Freedom Trail, harbor and history",
    "MIA": "beaches, Art Deco and Little Havana",
    "BNA": "country music, honky-tonks and hot chicken",
    "LAS": "shows, the Strip and the Hoover Dam nearby",
    "PHX": "desert hikes, sunshine and Taliesin West",
}
CURATED = {
    "SEA": [
        ("Pike Place Market", "Historic public market on the waterfront."),
        ("Space Needle", "Observation tower with city and mountain views."),
        ("Chihuly Garden and Glass", "Glass art museum at Seattle Center."),
    ],
    "SFO": [
        ("Golden Gate Bridge", "The city's landmark suspension bridge."),
        ("Alcatraz Island", "Former federal prison reached by ferry."),
        ("Golden Gate Park", "Large park with museums and gardens."),
    ],
    "LAX": [
        ("Griffith Observatory", "Free observatory with views of the Hollywood sign."),
        ("Getty Center", "Art museum and gardens on a hilltop."),
        ("Santa Monica Pier", "Beachfront pier with an amusement park."),
    ],
    "SAN": [
        ("Balboa Park", "Park with museums, gardens and the San Diego Zoo."),
        ("La Jolla Cove", "Rocky cove with sea lions and snorkeling."),
        ("USS Midway Museum", "Aircraft carrier museum on the bay."),
    ],
    "PDX": [
        ("Powell's City of Books", "One of the largest independent bookstores."),
        ("International Rose Test Garden", "Rose garden with city views."),
        ("Portland Japanese Garden", "Traditional gardens in Washington Park."),
    ],
    "DEN": [
        ("Red Rocks Park and Amphitheatre", "Concert venue set in red sandstone."),
        ("Denver Art Museum", "Large art museum downtown."),
        ("Larimer Square", "Historic block of shops and restaurants."),
    ],
    "ORD": [
        ("Millennium Park", "Park with Cloud Gate and public art."),
        ("Art Institute of Chicago", "Major art museum."),
        ("Chicago Architecture River Cruise", "Boat tour of the city's architecture."),
    ],
    "AUS": [
        ("Texas State Capitol", "Granite capitol building with free tours."),
        ("Barton Springs Pool", "Spring-fed swimming pool."),
        ("Congress Avenue Bridge bats", "Summer evening bat flights."),
    ],
    "JFK": [
        ("Central Park", "Manhattan's great urban park."),
        ("The Metropolitan Museum of Art", "One of the world's largest art museums."),
        ("Statue of Liberty", "Monument reached by ferry."),
    ],
    "BOS": [
        ("Freedom Trail", "Walking route past Revolutionary War sites."),
        ("Museum of Fine Arts", "Encyclopedic art museum."),
        ("Fenway Park", "Historic baseball park."),
    ],
    "MIA": [
        ("South Beach", "Beach and the Art Deco Historic District."),
        ("Little Havana", "Cuban culture along Calle Ocho."),
        ("Vizcaya Museum and Gardens", "Villa and formal gardens on the bay."),
    ],
    "BNA": [
        ("Country Music Hall of Fame", "Museum of country music history."),
        ("Ryman Auditorium", "Historic concert hall."),
        ("Broadway honky-tonks", "Live music bars downtown."),
    ],
    "LAS": [
        ("The Strip", "Casinos, shows and lights."),
        ("Hoover Dam", "Dam on the Colorado River, a short drive away."),
        ("Red Rock Canyon", "Scenic drive and hiking west of the city."),
    ],
    "PHX": [
        ("Desert Botanical Garden", "Garden of desert plants."),
        ("Camelback Mountain", "Popular hike with city views."),
        ("Heard Museum", "Museum of Native American art."),
    ],
}
SKIP = re.compile(r"\b(?:card|pass|citypass|go city|ticket)\b", re.IGNORECASE)


def _clean(text: str) -> str:
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # [[a|b]] -> b
    text = re.sub(r"\[https?://\S+ ([^\]]+)\]", r"\1", text)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"<[^>]+>", "", text).replace("'''", "").replace("''", "")
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.{20,180}?[.!?])(\s|$)", text)
    return (m.group(1) if m else text[:180]).strip()


def parse_see(wikitext: str, limit: int = 6) -> list[dict[str, str]]:
    found: list[tuple[int, str, str]] = []
    for m in re.finditer(r"\{\{\s*(?:see|do)\b(.*?)\n?\}\}", wikitext, re.IGNORECASE | re.DOTALL):
        body = m.group(1)
        name = re.search(r"\|\s*name\s*=\s*([^|\n]+)", body)
        content = re.search(r"\|\s*content\s*=\s*(.+)", body, re.DOTALL)
        if name and name.group(1).strip():
            found.append((m.start(), _clean(name.group(1)), _clean(content.group(1)) if content else ""))
    out, seen = [], set()
    for _, name, blurb in sorted(found):
        key = name.lower()
        # Listings without a description are usually district links, not sights.
        if not name or key in seen or SKIP.search(name) or len(name) > 60 or len(blurb) < 25:
            continue
        seen.add(key)
        out.append({"name": name, "why": blurb})
        if len(out) == limit:
            break
    return out


class Research:
    def __init__(self, cache_dir: Path | None = None, *, online: bool = True, timeout_s: float = 1.2) -> None:
        self.dir = cache_dir
        self.online = online
        self.timeout = timeout_s
        self._mem: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)

    def highlights(self, city: str) -> dict[str, Any]:
        with self._lock:
            if city in self._mem:
                return self._mem[city]
        path = self.dir / f"{city}.json" if self.dir else None
        if path and path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            live = json.loads(path.read_text(encoding="utf-8"))
        else:
            live = self._fetch(city) if self.online else None
            if path and self.online:  # cache empty answers too, so a slow source isn't retried on every plan
                path.write_text(json.dumps(live or {"places": []}), encoding="utf-8")
        # Well-known landmarks first, then extra listings from live research.
        places = [{"name": n, "why": w, "source": "curated"} for n, w in CURATED.get(city, [])]
        names = {p["name"].lower() for p in places}
        for p in (live or {}).get("places", []):
            if p["name"].lower() not in names and len(places) < 6:
                places.append({**p, "source": "Wikivoyage"})
                names.add(p["name"].lower())
        data = {
            "city": city,
            "places": places,
            "tagline": TAGLINES.get(city, ""),
            "source": "curated landmarks + Wikivoyage (CC BY-SA 4.0)"
            if any(p["source"] == "Wikivoyage" for p in places)
            else "curated landmarks",
            "url": (live or {}).get("url"),
        }
        with self._lock:
            self._mem[city] = data
        return data

    def _fetch(self, city: str) -> dict[str, Any] | None:
        page = PAGES.get(city)
        if not page:
            return None
        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=self.timeout) as client:
                secs = client.get(
                    API,
                    params={
                        "action": "parse",
                        "page": page,
                        "prop": "sections",
                        "format": "json",
                        "redirects": 1,
                    },
                ).json()["parse"]["sections"]
                see = next((s for s in secs if s["line"].strip().lower() == "see"), None)
                if see is None:
                    return None
                text = client.get(
                    API,
                    params={
                        "action": "parse",
                        "page": page,
                        "prop": "wikitext",
                        "section": see["index"],
                        "format": "json",
                        "redirects": 1,
                    },
                ).json()["parse"]["wikitext"]["*"]
        except (httpx.HTTPError, KeyError, ValueError):
            return None
        places = parse_see(text)
        if not places:
            return None
        return {
            "city": city,
            "places": places,
            "source": "Wikivoyage",
            "license": "CC BY-SA 4.0",
            "url": f"https://en.wikivoyage.org/wiki/{page.replace(' ', '_')}",
        }
