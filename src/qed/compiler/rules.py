"""Deterministic front end: utterance -> TripIntent in about a millisecond.

Each rule records the exact character span it consumed. Words no rule consumed
are reported as warnings ("this phrase was not compiled"), which is the honest
alternative to silently dropping part of what someone asked for. When an LLM
is configured, low coverage escalates the leftover phrases to it (see llm.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta

from qed.domain.amenities import NEGATE, SYNONYMS
from qed.domain.world import CITY_ALIASES, today
from qed.ir import (
    ActivitiesPerDay,
    ArriveBefore,
    AttractionsAccessible,
    BudgetMax,
    CategoryMin,
    CuisineMin,
    DepartAfter,
    Diagnostic,
    DietAll,
    DryOutdoor,
    ExcludeItem,
    IncludeItem,
    MaxStops,
    MealMaxPrice,
    NoRedEye,
    Span,
    StayAccessible,
    StayAmenity,
    StayKinds,
    StayMaxNightly,
    StayMinRating,
    StayRuleC,
    TransportModes,
    TripIntent,
)

NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "couple of": 2,
    "a couple of": 2,
    "a few": 3,
    "few": 3,
    "some": 2,
}
NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a couple of|couple of|a few|few|some|a|an)"
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
WEEKDAY_RE = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
WEEKDAY_IDX = {
    d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])
}
CUISINE_RE = r"(thai|italian|mexican|japanese|sushi|ramen|indian|chinese|dim sum|mediterranean|greek|french|korean|vietnamese|pho|seafood|american|bbq|barbecue)"
CUISINE_MAP = {
    "sushi": "japanese",
    "ramen": "japanese",
    "dim sum": "chinese",
    "greek": "mediterranean",
    "pho": "vietnamese",
    "bbq": "american",
    "barbecue": "american",
}
CATEGORY_RE = r"(museums?|parks?|galler(?:y|ies)|markets?|aquariums?|gardens?|tours?|cruises?|shows?|theat(?:er|re)s?|plays?|viewpoints?|landmarks?|hikes?|hiking)"
CATEGORY_MAP = {
    "museum": "museum",
    "park": "park",
    "gallery": "gallery",
    "galleries": "gallery",
    "market": "market",
    "aquarium": "aquarium",
    "garden": "garden",
    "tour": "tour",
    "cruise": "tour",
    "show": "theater",
    "theater": "theater",
    "theatre": "theater",
    "play": "theater",
    "viewpoint": "viewpoint",
    "landmark": "landmark",
    "hike": "park",
    "hiking": "park",
}
STOPWORDS = set(
    [
        "a",
        "an",
        "the",
        "to",
        "from",
        "for",
        "me",
        "my",
        "us",
        "our",
        "we",
        "i",
        "you",
        "it",
        "its",
        "please",
        "can",
        "could",
        "would",
        "like",
        "want",
        "wanna",
        "need",
        "needs",
        "must",
        "should",
        "book",
        "plan",
        "find",
        "get",
        "make",
        "take",
        "go",
        "going",
        "let's",
        "lets",
        "and",
        "or",
        "of",
        "on",
        "in",
        "at",
        "be",
        "with",
        "by",
        "some",
        "also",
        "alexa",
        "hey",
        "trip",
        "vacation",
        "getaway",
        "holiday",
        "visit",
        "travel",
        "traveling",
        "travelling",
        "stay",
        "staying",
        "place",
        "places",
        "hotel",
        "hotels",
        "lodging",
        "room",
        "rooms",
        "food",
        "eat",
        "eating",
        "restaurants",
        "restaurant",
        "things",
        "thing",
        "do",
        "see",
        "something",
        "good",
        "nice",
        "really",
        "just",
        "maybe",
        "around",
        "people",
        "person",
        "persons",
        "adults",
        "adult",
        "guests",
        "total",
        "max",
        "per",
        "night",
        "nights",
        "day",
        "days",
        "week",
        "weekend",
        "that",
        "this",
        "there",
        "is",
        "are",
        "was",
        "will",
        "fly",
        "flight",
        "flights",
        "flying",
        "leave",
        "leaving",
        "back",
        "home",
        "return",
        "returning",
        "time",
        "times",
        "am",
        "pm",
        "so",
        "too",
        "very",
        "only",
        "any",
        "all",
        "everyone",
        "everything",
        "family",
        "kids",
        "children",
        "wife",
        "husband",
        "partner",
        "friend",
        "friends",
        "mom",
        "dad",
        "grandma",
        "grandpa",
        "please",
        "budget",
        "dollars",
        "usd",
        "bucks",
        "cost",
        "costs",
        "costing",
        "spend",
        "spending",
        "under",
        "below",
        "up",
        "within",
        "less",
        "than",
        "more",
        "no",
        "not",
        "possible",
        "option",
        "options",
        "thanks",
        "thank",
        "great",
        "awesome",
        "somewhere",
        "needed",
        "kindly",
        "pls",
        "plz",
        "required",
        "baby",
        "toddler",
        "fine",
        "ok",
        "okay",
    ]
)

_Rule = Callable[[str, "ParseState"], None]


class ParseState:
    def __init__(self, text: str, known_people: list[str]) -> None:
        self.text = text
        self.low = text.lower()
        self.consumed = [False] * len(text)
        self.intent = TripIntent(utterance=text)
        self.known_people = known_people

    def take(self, m: re.Match[str], group: int = 0) -> Span:
        s, e = m.span(group)
        for i in range(s, e):
            self.consumed[i] = True
        return Span(start=s, end=e, text=self.text[s:e])

    def free(self, m: re.Match[str]) -> bool:
        s, e = m.span()
        return not any(self.consumed[s:e])

    def add(self, constraint, m: re.Match[str]) -> None:  # type: ignore[no-untyped-def]
        constraint.span = self.take(m)
        self.intent.constraints.append(constraint)


def _num(token: str) -> int:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return NUM_WORDS.get(token, 1)


def _time(h: str, m: str | None, suffix: str | None, *, prefer_pm: bool) -> int | None:
    hour = int(h)
    minute = int(m) if m else 0
    if hour > 23 or minute > 59:
        return None
    suffix = (suffix or "").replace(".", "").strip()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and prefer_pm and 1 <= hour <= 7:
        hour += 12
    return hour * 60 + minute


TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"


# ---------------------------------------------------------------------- rules


def rule_cities(text: str, st: ParseState) -> None:
    names = sorted(CITY_ALIASES, key=len, reverse=True)
    alt = "|".join(re.escape(n) for n in names)
    for m in re.finditer(rf"\bfrom\s+({alt})\b", st.low):
        st.intent.origin = CITY_ALIASES[m.group(1)]
        st.intent.provenance["origin"] = st.take(m)
    for m in re.finditer(rf"\b(?:to|in|visit(?:ing)?|around|for)\s+({alt})\b", st.low):
        if not st.free(m) or CITY_ALIASES[m.group(1)] == st.intent.origin:
            continue
        st.intent.destination = CITY_ALIASES[m.group(1)]
        st.intent.provenance["destination"] = st.take(m)
        break
    m = re.search(rf"\b({alt})\s+(?:to|->|→)\s+({alt})\b", st.low)
    if m and st.free(m):
        st.intent.origin, st.intent.destination = CITY_ALIASES[m.group(1)], CITY_ALIASES[m.group(2)]
        span = st.take(m)
        st.intent.provenance["origin"] = st.intent.provenance["destination"] = span
    if not st.intent.destination:
        for m in re.finditer(rf"\b({alt})\b", st.low):
            code = CITY_ALIASES[m.group(1)]
            if st.free(m) and code != st.intent.origin:
                st.intent.destination = code
                st.intent.provenance["destination"] = st.take(m)
                break


def _resolve_day(day: int, month: int, ref: date) -> date:
    year = ref.year
    candidate = date(year, month, min(day, 28) if month == 2 and day > 28 else day)
    if candidate < ref:
        candidate = candidate.replace(year=year + 1)
    return candidate


def rule_dates(text: str, st: ParseState) -> None:
    ref = today()
    low = st.low
    # Ranges: "nov 14 to nov 17", "nov 14-17", "from november 14 through 17"
    m = re.search(
        rf"\b{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:-|–|to|through|until|till)\s*(?:{MONTH_RE}\.?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b",
        low,
    )
    if m:
        start = _resolve_day(int(m.group(2)), MONTHS[m.group(1)[:3]], ref)
        end_month = MONTHS[m.group(3)[:3]] if m.group(3) else start.month
        end = date(start.year + (1 if end_month < start.month else 0), end_month, int(m.group(4)))
        if end > start:
            st.intent.start_date, st.intent.nights = start, (end - start).days
            st.intent.provenance["start_date"] = st.intent.provenance["nights"] = st.take(m)
            return
    m = re.search(
        rf"\b(?:on\s+|starting\s+|from\s+)?{MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low
    ) or re.search(rf"\b(?:on\s+|starting\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH_RE}\b", low)
    if m:
        g = m.groups()
        month, day = (g[0], g[1]) if not g[0].isdigit() else (g[1], g[0])
        st.intent.start_date = _resolve_day(int(day), MONTHS[month[:3]], ref)
        st.intent.provenance["start_date"] = st.take(m)
    m = re.search(r"\b(?:this|next)?\s*(long\s+)?weekend\b", low)
    if m and not st.intent.start_date:
        friday = ref + timedelta(days=(4 - ref.weekday()) % 7 or 7)
        if "next" in m.group(0):
            friday += timedelta(days=7)
        st.intent.start_date = friday
        st.intent.nights = st.intent.nights or (3 if m.group(1) else 2)
        st.intent.provenance["start_date"] = st.intent.provenance["nights"] = st.take(m)
    elif m and not st.intent.nights:
        st.intent.nights = 3 if m.group(1) else 2
        st.intent.provenance["nights"] = st.take(m)
    m = re.search(rf"\b(?:on\s+|leaving\s+|starting\s+)?(this|next)\s+{WEEKDAY_RE}\b", low) or re.search(
        rf"\b(?:on|leaving|starting)\s+{WEEKDAY_RE}\b", low
    )
    if m and not st.intent.start_date:
        groups = [g for g in m.groups() if g]
        wd = WEEKDAY_IDX[groups[-1]]
        delta = (wd - ref.weekday()) % 7 or 7
        if groups[0] == "next" and delta < 7:
            delta += 7
        st.intent.start_date = ref + timedelta(days=delta)
        st.intent.provenance["start_date"] = st.take(m)
    m = re.search(r"\b(tomorrow|in\s+(\d+|two|three|four)\s+weeks?)\b", low)
    if m and not st.intent.start_date:
        if m.group(1) == "tomorrow":
            st.intent.start_date = ref + timedelta(days=1)
        else:
            st.intent.start_date = ref + timedelta(weeks=_num(m.group(2)))
        st.intent.provenance["start_date"] = st.take(m)
    # Duration
    m = re.search(rf"\b{NUM}[\s-]+nights?\b", low)
    if m and st.free(m):
        st.intent.nights = _num(m.group(1))
        st.intent.provenance["nights"] = st.take(m)
    else:
        m = re.search(rf"\b{NUM}[\s-]+days?\b", low)
        if m and st.free(m):
            st.intent.nights = max(1, _num(m.group(1)) - 1)
            st.intent.provenance["nights"] = st.take(m)
        else:
            m = re.search(r"\b(?:a|one|for a)\s+week\b", low)
            if m and st.free(m):
                st.intent.nights = 7
                st.intent.provenance["nights"] = st.take(m)


def rule_travelers(text: str, st: ParseState) -> None:
    low = st.low
    count = None
    # "2 adults and 2 kids": everyone needs a seat and a bed.
    kids = r"(?:kids?|children|child|teens?|teenagers?|toddlers?|bab(?:y|ies)|infants?|sons?|daughters?)"
    m = re.search(rf"\b{NUM}\s+adults?\s*(?:,|and|\+|&|with|plus)\s*{NUM}\s+{kids}\b", low)
    if m and st.free(m):
        count = _num(m.group(1)) + _num(m.group(2))
        st.intent.provenance["travelers"] = st.take(m)  # rule_stay adds "children welcome"
    m = (
        None
        if count
        else (
            re.search(rf"\bfor\s+{NUM}\s+(?:people|persons|adults|travell?ers|guests|of us)\b", low)
            or re.search(rf"\b{NUM}\s+(?:people|persons|adults|travell?ers|guests)\b", low)
            or re.search(rf"\b(?:family|group)\s+of\s+{NUM}\b", low)
            or re.search(r"\bfor\s+(\d+|two|three|four|five|six)\b(?!\s*(?:nights?|days?|am|pm|:|\$))", low)
        )
    )
    if m and st.free(m):
        count = _num(m.group(1))
        st.intent.provenance["travelers"] = st.take(m)
    m = re.search(r"\b(?:just me|solo|by myself|alone)\b", low)
    if m and count is None:
        count = 1
        st.intent.provenance["travelers"] = st.take(m)
    companions = 0
    for m in re.finditer(
        r"\b(?:me and|with)\s+my\s+(wife|husband|partner|girlfriend|boyfriend|friend|mom|dad|mother|father)\b",
        low,
    ):
        companions += 1
        st.take(m)
    m = re.search(rf"\b(?:and|with)\s+(?:my|our)\s+{NUM}\s+(kids|children|sons|daughters)\b", low)
    if m:
        companions += _num(m.group(1))
        st.take(m)
    for person in st.known_people:
        for m in re.finditer(rf"\b{re.escape(person.lower())}\b", low):
            if person not in st.intent.traveler_names:
                st.intent.traveler_names.append(person)
                companions += 1
            st.take(m)
    if count is None and companions:
        count = 1 + companions
    if count:
        st.intent.travelers = max(1, min(count, 12))


def rule_budget(text: str, st: ParseState) -> None:
    pat = (
        r"(?:under|below|less than|at most|max(?:imum)?(?: of)?|budget(?: of| is| around)?|up to|no more than|"
        r"within|cap(?:ped)? at|keep it (?:under|below))\s*\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k|grand|thousand)?\b"
        r"(?:\s*(?:dollars|usd|bucks))?(?:\s*(per person|each|pp|a head))?(?:\s*(?:total|all in|in total))?"
    )
    for m in re.finditer(pat, st.low):
        if not st.free(m):
            continue
        tail = st.low[m.end() : m.end() + 14]
        if re.match(r"\s*(?:a|per|/)\s*night", tail):
            amount = _money(m.group(1), m.group(2))
            st.add(StayMaxNightly(amount=amount), _extend(m, st.low, r"\s*(?:a|per|/)\s*night"))
            continue
        if re.match(r"\s*(?:a|per|/)\s*(?:meal|plate)", tail):
            st.add(
                MealMaxPrice(amount=_money(m.group(1), m.group(2))),
                _extend(m, st.low, r"\s*(?:a|per|/)\s*(?:meal|plate)"),
            )
            continue
        st.add(BudgetMax(amount=_money(m.group(1), m.group(2)), per_person=bool(m.group(3))), m)
        return
    m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)\s*(k)?\s*(?:budget|total|all in|max)\b", st.low)
    if m and st.free(m):
        st.add(BudgetMax(amount=_money(m.group(1), m.group(2))), m)


def _money(num: str, mult: str | None) -> int:
    value = float(num.replace(",", ""))
    if mult:
        value *= 1000
    return round(value)


class _Ext:
    """A tiny match-like object spanning a match plus a trailing pattern."""

    def __init__(self, m: re.Match[str], end: int) -> None:
        self._s, self._e = m.start(), end

    def span(self, group: int = 0) -> tuple[int, int]:
        return self._s, self._e


def _extend(m: re.Match[str], low: str, tail: str) -> re.Match[str]:
    t = re.match(tail, low[m.end() :])
    return _Ext(m, m.end() + (t.end() if t else 0))  # type: ignore[return-value]


def rule_transport(text: str, st: ParseState) -> None:
    low = st.low
    for m in re.finditer(
        rf"\b(?:no|not|don'?t want|avoid)\s+(?:any\s+)?(?:flights?|flying|departures?|leaving)\s+(?:before|earlier than)\s+{TIME_RE}",
        low,
    ):
        t = _time(m.group(1), m.group(2), m.group(3), prefer_pm=False)
        if t is not None:
            st.add(DepartAfter(leg="outbound", minutes=t), m)
    for m in re.finditer(
        rf"\b(?:leave|leaving|depart(?:ing)?|fly(?:ing)?\s+out|take\s+off)\s+(?:after|no earlier than|later than)\s+{TIME_RE}",
        low,
    ):
        if st.free(m) and (t := _time(m.group(1), m.group(2), m.group(3), prefer_pm=False)) is not None:
            st.add(DepartAfter(leg="outbound", minutes=t), m)
    for m in re.finditer(
        rf"\b(?:arrive|arriving|land|landing|get there|be there)\s+(?:by|before|no later than)\s+{TIME_RE}",
        low,
    ):
        if st.free(m) and (t := _time(m.group(1), m.group(2), m.group(3), prefer_pm=True)) is not None:
            st.add(ArriveBefore(leg="outbound", minutes=t), m)
    for m in re.finditer(
        rf"\b(?:back|home|return(?:ing)?(?:\s+home)?)\s+(?:by|before|no later than)\s+{TIME_RE}", low
    ):
        if st.free(m) and (t := _time(m.group(1), m.group(2), m.group(3), prefer_pm=True)) is not None:
            st.add(ArriveBefore(leg="return", minutes=t), m)
    for m in re.finditer(
        rf"\b(?:return(?:ing)?|fly(?:ing)?\s+back|head(?:ing)?\s+(?:back|home))\s+(?:after|no earlier than)\s+{TIME_RE}",
        low,
    ):
        if st.free(m) and (t := _time(m.group(1), m.group(2), m.group(3), prefer_pm=True)) is not None:
            st.add(DepartAfter(leg="return", minutes=t), m)
    m = re.search(r"\b(?:no|avoid|not on a|without)\s+red[\s-]?eyes?\b|\bno overnight flights?\b", low)
    if m:
        st.add(NoRedEye(), m)
    m = re.search(r"\b(?:non[\s-]?stop|direct)(?:\s+flights?)?(?:\s+only)?\b", low)
    if m:
        st.add(MaxStops(stops=0), m)
    m = re.search(r"\bat most (one|1) (?:stop|layover)\b|\bno more than (one|1) (?:stop|layover)\b", low)
    if m and st.free(m):
        st.add(MaxStops(stops=1), m)
    m = re.search(
        r"\b(?:no flying|no flights|no planes?|(?:don'?t|do not|hate to|rather not) (?:want to )?fly)\b"
        r"(?!\s+(?:before|after|earlier|later|that|which|leaving|departing))",
        low,
    )
    if m and st.free(m):
        st.add(TransportModes(allowed=["train", "drive"]), m)
    else:
        m = re.search(r"\b(?:by|take the|on the)\s+train\b|\btrain only\b", low)
        if m:
            st.add(TransportModes(allowed=["train"]), m)
        else:
            m = re.search(r"\b(?:road\s*trip|drive there|let'?s drive|by car|driving)\b", low)
            if m:
                st.add(TransportModes(allowed=["drive"]), m)


def rule_stay(text: str, st: ParseState) -> None:
    low = st.low
    m = re.search(
        r"\b(?:wheelchair[\s-]?(?:accessible|friendly|user)?|step[\s-]free|accessible|mobility (?:issues|needs)|uses a wheelchair)\b"
        r"(?:\s+(?:hotel|room|place|stay|lodging|everything|attractions|activities))?",
        low,
    )
    if m:
        scope = m.group(0)
        span_m = m
        if any(w in scope for w in ("hotel", "room", "place", "stay", "lodging")):
            st.add(StayAccessible(), span_m)
        elif any(w in scope for w in ("attractions", "activities")):
            st.add(AttractionsAccessible(), span_m)
        else:
            st.add(StayAccessible(), span_m)
            st.intent.constraints.append(AttractionsAccessible(span=st.intent.constraints[-1].span))
    m = re.search(
        r"\b(?:rated|rating(?: of)?|at least)\s*(\d(?:\.\d)?)\s*(?:\+|stars?|or (?:better|higher|above))?|\b(\d(?:\.\d)?)\s*[\s-]?(?:\+\s*)?stars?\b",
        low,
    )
    if m and st.free(m):
        val = float(m.group(1) or m.group(2))
        if 1 <= val <= 5:
            st.add(StayMinRating(rating=val), m)
    m = re.search(
        r"\b(?:highly[\s-]rated|top[\s-]rated|well[\s-]reviewed|really nice hotel|luxury hotel)\b", low
    )
    if m and st.free(m):
        st.add(StayMinRating(rating=4.5), m)
    m = re.search(
        r"\b(?:entire (?:home|place|house|apartment)|whole (?:house|place|apartment)|(?:an |a )?(?:apartment|airbnb|house|condo|vacation rental))\b",
        low,
    )
    if m:
        st.add(StayKinds(kinds=["entire_home"]), m)
    # Negated shared lodging ("I don't want to stay in a hostel") means private, never the opposite.
    m = re.search(
        r"\b(?:private room|(?:my|our) own room|not (?:a )?shared(?: room)?|no shared rooms?|no dorms?|"
        r"(?:no|not|never|avoid|without|don'?t want(?: to stay)?(?: in| at)?|hate)\s+(?:an?\s+|any\s+)?"
        r"(?:hostels?|dorms?|shared rooms?))\b",
        low,
    )
    if m and st.free(m):
        st.add(StayKinds(kinds=["hotel_room", "entire_home", "private_room"]), m)
    m = re.search(r"\b(?:hostel|shared rooms?|dorms?|sharing (?:a )?room|room ?sharing)\b", low)
    if m and st.free(m):
        st.add(StayKinds(kinds=["shared_room"]), m)
    m = re.search(
        r"\b(?:pet[\s-]friendly|pets? allowed|(?:bring(?:ing)?|with)\s+(?:my|our|the)\s+(?:dog|cat|pup|puppy))\b",
        low,
    )
    if m:
        st.add(StayRuleC(rule="pets_allowed"), m)
    m = re.search(r"\b(?:non[\s-]?smoking|smoke[\s-]free)\b", low)
    if m:
        st.add(StayRuleC(rule="smoke_free"), m)
    ac = r"(?:a\.?c\.?|air[\s-]?con(?:ditioning|ditioned|ditioner)?)"
    m = re.search(
        rf"\b(?:no|without|non|not|don'?t want|avoid)[\s-]+(?:an?\s+)?{ac}(?=\W|$)(?:\s+rooms?)?", low
    )
    if m:
        st.add(StayRuleC(rule="no_air_conditioning"), m)
    else:
        m = re.search(
            rf"\b(?:with|need|needs|must have|want)\s+(?:an?\s+)?{ac}(?=\W|$)|\bair[\s-]?conditioned\b", low
        )
        if m and st.free(m):
            st.add(StayRuleC(rule="air_conditioned"), m)
    m = re.search(
        r"\b(?:kid[\s-]friendly|family[\s-]friendly|with (?:the |our |my )?(?:kids|children|baby|toddler))\b",
        low,
    )
    if m and st.free(m):
        st.add(StayRuleC(rule="children_allowed"), m)
    elif re.search(r"\b(?:kids|children)\b", low) and st.intent.travelers > 1:
        st.intent.constraints.append(
            StayRuleC(rule="children_allowed", origin="commonsense", because="Children are travelling.")
        )


def rule_food(text: str, st: ParseState) -> None:
    low = st.low
    for diet, pat in (
        ("vegan", r"\bvegan\b"),
        ("vegetarian", r"\b(?:vegetarian|veggie|meat[\s-]free)\b"),
        ("gluten_free", r"\b(?:gluten[\s-]free|celiac|coeliac)\b"),
    ):
        m = re.search(pat, low)
        if m:
            st.add(DietAll(diet=diet), m)  # type: ignore[arg-type]
    for m in re.finditer(
        rf"\b(?:at least\s+)?(?:{NUM}\s+)?{CUISINE_RE}(?:\s+(?:food|meals?|dinners?|lunch(?:es)?|restaurants?|places?|nights?|spots?))?\b",
        low,
    ):
        if not st.free(m):
            continue
        count = _num(m.group(1)) if m.group(1) else 1
        cuisine = CUISINE_MAP.get(m.group(2), m.group(2))
        st.add(CuisineMin(cuisine=cuisine, count=max(1, count)), m)


def rule_activities(text: str, st: ParseState) -> None:
    low = st.low
    for m in re.finditer(rf"\b(?:at least\s+)?(?:{NUM}\s+)?{CATEGORY_RE}\b", low):
        if not st.free(m):
            continue
        raw = m.group(2)
        key = raw.rstrip("s") if raw not in ("galleries",) else "galleries"
        if raw.endswith("ies"):
            key = "galleries"
        category = CATEGORY_MAP.get(key, CATEGORY_MAP.get(raw))
        if category is None:
            continue
        count = _num(m.group(1)) if m.group(1) else (2 if raw.endswith("s") and raw not in ("hiking",) else 1)
        st.add(CategoryMin(category=category, count=max(1, count)), m)
    m = re.search(r"\b(?:relaxed|slow|easy|chill|laid[\s-]back)\s*(?:pace|trip|schedule|itinerary)?\b", low)
    if m and st.free(m):
        st.add(_activities(0, 1), m)
    m = re.search(r"\b(?:packed|busy|see (?:as much as possible|everything))\b", low)
    if m and st.free(m):
        st.add(_activities(2, 3), m)
    m = re.search(
        rf"\b(?:no more than|at most|max(?:imum)?)\s+{NUM}\s+(?:activities|things|stops|sights)\s+(?:a|per)\s+day\b",
        low,
    )
    if m and st.free(m):
        st.add(_activities(0, _num(m.group(1))), m)
    m = re.search(r"\b(?:avoid (?:the )?rain|dry days?|good weather|when it'?s not raining)\b", low)
    if m:
        st.add(DryOutdoor(), m)


def _activities(lo: int, hi: int) -> ActivitiesPerDay:
    return ActivitiesPerDay(min=lo, max=hi)


def rule_named(text: str, st: ParseState) -> None:
    for m in re.finditer(
        r"\b(?:must|have to|want to|wanna|need to)\s+(?:see|visit|eat at|stay at|go to)\s+(?:the\s+)?([a-z0-9&' ]{3,40}?)(?=[,.;!?]|\s+and\b|\s+on\b|$)",
        st.low,
    ):
        if st.free(m):
            st.add(IncludeItem(name=text[m.start(1) : m.end(1)].strip()), m)
    for m in re.finditer(
        r"\b(?:avoid|skip|not)\s+(?:the\s+)?([A-Z][A-Za-z0-9&' ]{2,40}?)(?=[,.;!?]|\s+and\b|$)", text
    ):
        if st.free(m):
            st.add(ExcludeItem(name=m.group(1).strip()), m)


def rule_objective(text: str, st: ParseState) -> None:
    low = st.low
    m = re.search(
        r"\b(?:cheapest|as cheap as possible|budget[\s-]friendly|save money|lowest price|on a budget|affordable)\b",
        low,
    )
    if m:
        st.intent.objective = "cheapest"
        st.intent.provenance["objective"] = st.take(m)
        return
    m = re.search(
        r"\b(?:best|nicest|luxur(?:y|ious)|splurge|treat (?:myself|ourselves|us)|special occasion|anniversary)\b",
        low,
    )
    if m:
        st.intent.objective = "best"
        st.intent.provenance["objective"] = st.take(m)


def rule_amenities(text: str, st: ParseState) -> None:
    for amenity, syn in SYNONYMS.items():
        m = re.search(rf"\b{NEGATE}\s+(?:an?\s+|the\s+|any\s+)?(?:{syn})(?=\W|$)", st.low)
        if m and st.free(m):
            st.add(StayAmenity(amenity=amenity, want=False), m)  # type: ignore[arg-type]
            continue
        m = re.search(rf"\b(?:{syn})(?=\W|$)", st.low)
        if m and st.free(m):
            st.add(StayAmenity(amenity=amenity, want=True), m)  # type: ignore[arg-type]


RULES: list[_Rule] = [
    rule_cities,
    rule_dates,
    rule_budget,
    rule_named,
    rule_travelers,
    rule_transport,
    rule_stay,
    rule_amenities,
    rule_food,
    rule_activities,
    rule_objective,
]


def parse(text: str, known_people: list[str] | None = None) -> TripIntent:
    st = ParseState(text, known_people or [])
    for rule in RULES:
        rule(text, st)
    intent = st.intent
    leftovers = _leftovers(st)
    content = [w for w in re.finditer(r"[a-z0-9']+", st.low) if w.group(0) not in STOPWORDS]
    used = [w for w in content if all(st.consumed[w.start() : w.end()])]
    intent.coverage = round(len(used) / len(content), 3) if content else 1.0
    for span in leftovers:
        intent.diagnostics.append(
            Diagnostic(
                severity="warning",
                code="QED-W001",
                message=f"'{span.text}' was not understood and is not part of the plan.",
                span=span,
            )
        )
    return intent.renumber()


def _leftovers(st: ParseState) -> list[Span]:
    spans: list[Span] = []
    for m in re.finditer(r"[a-z0-9'$.,]+(?:\s+[a-z0-9'$.,]+)*", st.low):
        words = list(re.finditer(r"[a-z0-9']+", m.group(0)))
        run_start = run_end = None
        for w in words:
            s, e = m.start() + w.start(), m.start() + w.end()
            unknown = w.group(0) not in STOPWORDS and not all(st.consumed[s:e]) and not w.group(0).isdigit()
            if unknown:
                run_start = s if run_start is None else run_start
                run_end = e
            elif run_start is not None and (w.group(0) not in STOPWORDS or all(st.consumed[s:e])):
                spans.append(Span(start=run_start, end=run_end or s, text=st.text[run_start : run_end or s]))
                run_start = run_end = None
        if run_start is not None:
            spans.append(Span(start=run_start, end=run_end or run_start, text=st.text[run_start:run_end]))
    return spans
