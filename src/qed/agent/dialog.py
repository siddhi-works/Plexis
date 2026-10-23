"""The conversational booking agent.

People don't say a whole trip in one sentence, and they forget things that
matter (a shared room is fine, no AC, Grandma needs step-free access). The
dialog keeps a trip *draft* between turns, merges each answer into it, and
asks for one missing thing at a time, always with up to three concrete
suggestions so a voice user can just say "the first one".

Order of questions: where -> when -> how long -> who -> from where ->
budget (priced from real quotes) -> room needs. Required details are asked
until answered; optional ones are asked once. While the customer is still
answering, partner data for the trip is fetched in the background, so the
final plan comes back fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field

from qed.agent.learning import CORRECTION
from qed.compiler import rules
from qed.domain import world
from qed.domain.amenities import COASTAL, HOT
from qed.domain.research import TAGLINES
from qed.engine import CompileResult, Engine, apply_refinement
from qed.graph import Graph
from qed.guard import screen_input
from qed.ir import ActivitiesPerDay, TripIntent
from qed.partners.client import CallLog
from qed.solver.cpsat import TripModel
from qed.util import fmt_money

REGIONS: dict[str, list[str]] = {
    "california": ["SFO", "LAX", "SAN"],
    "socal": ["LAX", "SAN"],
    "southern california": ["LAX", "SAN"],
    "west coast": ["SEA", "SFO", "LAX"],
    "pacific northwest": ["SEA", "PDX"],
    "the northwest": ["SEA", "PDX"],
    "east coast": ["JFK", "BOS", "MIA"],
    "new england": ["BOS"],
    "florida": ["MIA"],
    "texas": ["AUS"],
    "tennessee": ["BNA"],
    "arizona": ["PHX"],
    "nevada": ["LAS"],
    "colorado": ["DEN"],
    "oregon": ["PDX"],
    "washington state": ["SEA"],
    "illinois": ["ORD"],
    "massachusetts": ["BOS"],
    "the mountains": ["DEN", "SEA", "PHX"],
    "mountains": ["DEN", "SEA", "PHX"],
    "hiking": ["DEN", "PHX", "PDX"],
    "the beach": ["MIA", "SAN", "LAX"],
    "beach": ["MIA", "SAN", "LAX"],
    "somewhere warm": ["MIA", "SAN", "PHX"],
    "warm": ["MIA", "SAN", "PHX"],
    "sunny": ["SAN", "PHX", "MIA"],
    "a big city": ["JFK", "ORD", "SFO"],
    "big city": ["JFK", "ORD", "SFO"],
    "nightlife": ["LAS", "MIA", "BNA"],
    "somewhere fun": ["LAS", "BNA", "MIA"],
    "live music": ["BNA", "AUS", "ORD"],
    "food": ["SFO", "JFK", "AUS"],
    "museums": ["JFK", "ORD", "BOS"],
}
SKIP = re.compile(
    r"^\s*(?:no(?:thing)?(?: (?:preference|special|particular|thanks))?|none|skip|doesn'?t matter|"
    r"you (?:choose|pick|decide)|anything(?: is fine)?|not really|whatever|no budget|any budget|"
    r"no fixed budget|i'?m flexible|flexible)\s*[.!]?\s*$",
    re.IGNORECASE,
)
YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|correct|right|sounds good|that'?s right|do it|go ahead)\b",
    re.IGNORECASE,
)
NO = re.compile(r"^\s*(?:no|nope|nah|not quite|wrong)\b", re.IGNORECASE)
# Ordinals only: a bare "3" answers "how many nights?", it doesn't pick the third suggestion.
ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}
FILLER = re.compile(
    r"^\s*(?:thanks?(?: you)?(?: (?:so|very) much)?|um+|uh+|hmm+|mm+|so|well|let me think|"
    r"one (?:sec|second|moment))\s*[.!,]?\s*$",
    re.IGNORECASE,
)
NEW_TRIP = re.compile(
    r"\b(?:plan|book|new|another|different)\b.*\b(?:trip|vacation|getaway|holiday|weekend)\b", re.IGNORECASE
)
PLACE_WORDS = re.compile(r"\b(?:to|in|visit|visiting|from|around)\s+([A-Z][\w'.-]+(?:\s+[A-Z][\w'.-]+){0,2})")
NOT_PLACES = {
    m.capitalize()
    for m in [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
}
# Room details people forget to mention. Step-free access is handled by household memory, not this question.
STAY_PREFS = {("stay_kinds", None), ("stay_rule", "air_conditioned"), ("stay_rule", "no_air_conditioning")}


class Suggestion(BaseModel):
    label: str = Field(description="What to show or read out.")
    say: str = Field(description="What Plexis treats it as when chosen.")


class Question(BaseModel):
    slot: str
    text: str
    suggestions: list[Suggestion] = Field(default_factory=list)


@dataclass
class Draft:
    id: str
    intent: TripIntent
    asked: set[str] = field(default_factory=set)
    origin_from_profile: bool = False
    use_profile: bool = True
    stage: str = "collecting"  # collecting | planned | infeasible
    plan_id: str | None = None
    question: Question | None = None
    notes: list[str] = field(default_factory=list)
    prefetch: asyncio.Task[Any] | None = None
    learned: list[str] = field(default_factory=list)  # preferences the customer may want remembered
    region: list[str] = field(default_factory=list)  # candidate cities for a vague place ("somewhere warm")

    def dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "intent": self.intent.model_dump(mode="json"),
            "asked": sorted(self.asked),
            "origin_from_profile": self.origin_from_profile,
            "use_profile": self.use_profile,
            "stage": self.stage,
            "plan_id": self.plan_id,
            "question": self.question.model_dump() if self.question else None,
            "notes": self.notes,
            "learned": self.learned,
            "region": self.region,
        }

    @classmethod
    def load(cls, d: dict[str, Any]) -> Draft:
        return cls(
            id=d["id"],
            intent=TripIntent.model_validate(d["intent"]),
            asked=set(d["asked"]),
            origin_from_profile=d["origin_from_profile"],
            use_profile=d.get("use_profile", True),
            stage=d["stage"],
            plan_id=d["plan_id"],
            question=Question.model_validate(d["question"]) if d["question"] else None,
            notes=d["notes"],
            learned=d.get("learned", []),
            region=d.get("region", []),
        )


@dataclass
class Turn:
    status: str  # needs_input | planned | infeasible | rejected | learned
    draft_id: str
    speech: str
    question: Question | None = None
    result: CompileResult | None = None
    suggestions: list[Suggestion] = field(default_factory=list)
    path: list[str] = field(default_factory=list)  # route through the turn graph


def _append(left: list[str], right: list[str]) -> list[str]:
    return [*left, *right]


class TurnState(TypedDict, total=False):
    text: str
    draft_id: str | None
    use_profile: bool
    progress: Any
    route: str
    taught: tuple[str, list[str]]
    draft: Any
    turn: Turn
    path: Annotated[list[str], _append]


def _next_fridays(n: int = 2) -> list[date]:
    today = world.today()
    first = today + timedelta(days=((4 - today.weekday()) % 7) or 7)
    return [first + timedelta(days=7 * i) for i in range(n)]


def _say_date(d: date) -> str:
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def _facts(it: TripIntent) -> dict[str, Any]:
    return {
        "destination": it.destination,
        "origin": it.origin,
        "start_date": it.start_date,
        "nights": it.nights,
        "travelers": it.travelers,
    }


def _is_pref(c: Any) -> bool:
    return (
        c.kind == "stay_amenity"
        or (c.kind, getattr(c, "rule", None) if c.kind == "stay_rule" else None) in STAY_PREFS
    )


def _has_pref(intent: TripIntent) -> bool:
    return any(_is_pref(c) for c in intent.constraints)


class Dialog:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.drafts: dict[str, Draft] = {}
        self._graph: Any = None
        self.dir: Path = engine.state / "drafts"
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ storage

    def _get(self, draft_id: str | None) -> Draft | None:
        if not draft_id or not re.fullmatch(r"[a-f0-9]{12}", draft_id):
            return None
        if draft_id in self.drafts:
            return self.drafts[draft_id]
        path = self.dir / f"{draft_id}.json"
        if path.exists():
            import orjson

            self.drafts[draft_id] = Draft.load(orjson.loads(path.read_bytes()))
            return self.drafts[draft_id]
        return None

    def _save(self, d: Draft) -> None:
        import orjson

        self.drafts[d.id] = d
        (self.dir / f"{d.id}.json").write_bytes(orjson.dumps(d.dump()))

    # ------------------------------------------------------------------ the turn

    async def turn(
        self, text: str, draft_id: str | None = None, *, use_profile: bool = True, progress: Any = None
    ) -> Turn:
        """One customer turn, routed through a LangGraph state graph (see `_turn_graph`)."""
        if self._graph is None:
            self._graph = self._turn_graph()
        state = await self._graph.ainvoke(
            {"text": text, "draft_id": draft_id, "use_profile": use_profile, "progress": progress, "path": []}
        )
        turn: Turn = state["turn"]
        turn.path = state["path"]
        return turn

    def _turn_graph(self) -> Any:
        """screen -> classify -> one of teach / start / answer / choose_fix / refine.

        Each turn is a small graph instead of nested ifs, so the route taken is explicit
        and shows up in the trace ("path"), and new behaviors are new nodes.
        """
        from langgraph.graph import END, START, StateGraph

        def visit(name: str) -> dict[str, Any]:
            return {"path": [name]}

        async def screen(s: TurnState) -> TurnState:
            verdict = screen_input(s["text"], max_chars=self.e.s.max_request_chars)
            if not verdict.allowed:
                return {
                    **visit("screen"),
                    "turn": Turn("rejected", s.get("draft_id") or "", verdict.reason or "I can't use that."),
                }
            return {**visit("screen"), "text": verdict.text}

        async def classify(s: TurnState) -> TurnState:
            draft = self._get(s.get("draft_id"))
            taught = self.e.learner.teach(s["text"])
            if taught is not None:
                return {**visit("classify"), "route": "teach", "taught": taught, "draft": draft}
            if draft is not None and draft.stage != "collecting" and self._is_new_trip(s["text"], draft):
                draft = None
            route = (
                "start"
                if draft is None
                else {"infeasible": "choose_fix", "planned": "refine"}.get(draft.stage, "answer")
            )
            return {**visit("classify"), "route": route, "draft": draft}

        async def teach(s: TurnState) -> TurnState:
            return {
                **visit("teach"),
                "turn": await self._taught(s.get("draft"), s["taught"], s.get("progress")),
            }

        async def start(s: TurnState) -> TurnState:
            return {
                **visit("start"),
                "turn": await self._start(
                    s["text"], use_profile=s.get("use_profile", True), progress=s.get("progress")
                ),
            }

        async def answer(s: TurnState) -> TurnState:
            return {**visit("answer"), "turn": await self._answer(s["draft"], s["text"], s.get("progress"))}

        async def choose_fix(s: TurnState) -> TurnState:
            return {
                **visit("choose_fix"),
                "turn": await self._choose_fix(s["draft"], s["text"], s.get("progress")),
            }

        async def refine(s: TurnState) -> TurnState:
            return {**visit("refine"), "turn": await self._refine(s["draft"], s["text"], s.get("progress"))}

        g = StateGraph(TurnState)
        for name, fn in (
            ("screen", screen),
            ("classify", classify),
            ("teach", teach),
            ("start", start),
            ("answer", answer),
            ("choose_fix", choose_fix),
            ("refine", refine),
        ):
            g.add_node(name, fn)
        g.add_edge(START, "screen")
        g.add_conditional_edges("screen", lambda s: END if "turn" in s else "classify", ["classify", END])
        g.add_conditional_edges(
            "classify", lambda s: s["route"], ["teach", "start", "answer", "choose_fix", "refine"]
        )
        for name in ("teach", "start", "answer", "choose_fix", "refine"):
            g.add_edge(name, END)
        return g.compile()

    async def _taught(self, draft: Draft | None, taught: tuple[str, list[str]], progress: Any) -> Turn:
        phrase, meaning = taught
        if not meaning:
            speech = (
                f"I couldn't turn that into something I can check for “{phrase}”. Try things like a pool, "
                "no AC, breakfast included or under $2,000."
            )
            return Turn(
                "needs_input",
                draft.id if draft else "",
                speech,
                question=draft.question if draft else None,
                suggestions=draft.question.suggestions if draft and draft.question else [],
            )
        lead = f"Got it. When you say “{phrase}”, I'll plan for {' and '.join(meaning)}."
        if draft is None or draft.stage != "collecting":
            return Turn("learned", draft.id if draft else "", lead + " Ask me for a trip any time.")
        draft.notes += self.e.learner.apply(draft.intent)  # it may already be in what they said
        return await self._ask(draft, progress, lead=lead)

    def _is_new_trip(self, text: str, draft: Draft) -> bool:
        if NEW_TRIP.search(text):
            return True
        parsed = rules.parse(text)
        return bool(
            parsed.destination
            and parsed.destination != draft.intent.destination
            and "destination" in parsed.provenance
            and parsed.start_date
        )

    async def _start(self, text: str, *, use_profile: bool, progress: Any) -> Turn:
        intent = rules.parse(text, known_people=self.e.profile.people)
        draft = Draft(id=uuid.uuid4().hex[:12], intent=intent, use_profile=use_profile)
        had_origin = bool(intent.origin)
        if use_profile:
            draft.notes = self.e.profile.apply(intent)
            draft.origin_from_profile = not had_origin and bool(intent.origin)
            draft.notes += self.e.learner.apply(intent)
            if self.e.learner.prefers_value and "objective" not in intent.provenance:
                intent.objective = "cheapest"
                draft.notes.append("you usually ask for cheaper options, so I started with the best value")
        unknown = self._unknown_places(text)
        if unknown and not intent.destination:
            draft.asked.add("unsupported")
            return await self._ask(draft, progress, lead=self._unsupported_lead(unknown))
        lead = self._region_lead(text, draft)
        # Offer teaching only once the trip itself was understood; otherwise just ask the next question.
        gap = self._gap_lead(intent) if intent.destination else ""
        return await self._ask(draft, progress, lead=" ".join(x for x in (gap, lead) if x))

    async def _answer(self, draft: Draft, text: str, progress: Any) -> Turn:
        q = draft.question
        if FILLER.match(text):  # "thank you", "um": not an answer; ask again, briefly
            return await self._ask(draft, progress, lead="")
        said = self._resolve(text, q)
        if q is not None:
            draft.asked.add(q.slot)
            if said is None:  # "no preference", "skip", "you choose"
                if q.slot in ("budget", "needs", "travelers"):
                    return await self._ask(draft, progress, lead="No problem.")
                if q.slot == "origin_confirm":
                    draft.origin_from_profile = False
                    return await self._ask(draft, progress, lead="Okay.")
                said = q.suggestions[0].say if q.suggestions else text  # required: take the first suggestion
            elif q.slot == "origin_confirm" and NO.match(text) and not rules.parse(text).origin:
                draft.intent.origin = None
                draft.origin_from_profile = False
                return await self._ask(draft, progress, lead="Okay.")
            said = self._contextualise(said, q.slot)
        unknown = self._unknown_places(said)
        if unknown and q is not None and q.slot == "destination" and not rules.parse(said).destination:
            return await self._ask(draft, progress, lead=self._unsupported_lead(unknown))
        region = self._region_lead(said, draft)
        before = draft.intent
        draft.intent = apply_refinement(before, said, self.e.profile.people)
        draft.intent.utterance = f"{before.utterance} | {text}"
        if draft.use_profile:  # new travelers may bring saved needs (Grandma's wheelchair, Maya's diet)
            draft.notes += [n for n in self.e.profile.apply(draft.intent) if n not in draft.notes]
            draft.notes += [n for n in self.e.learner.apply(draft.intent) if n not in draft.notes]
        if CORRECTION.match(text):  # "no, I meant 4 nights": fix it and remember the slip
            self.e.learner.note_correction(text, _facts(before), _facts(draft.intent))
        gap = (
            self._gap_lead(rules.parse(said)) if said == text and q is not None and q.slot == "needs" else ""
        )
        if q is not None and q.slot == "origin_confirm" and draft.intent.origin:
            draft.origin_from_profile = False
        if q is not None and q.slot == "needs" and _has_pref(draft.intent):
            draft.learned = [
                c.describe() for c in draft.intent.constraints if c.origin == "user" and _is_pref(c)
            ]
        return await self._ask(
            draft, progress, lead=" ".join(x for x in (gap, region or self._ack(before, draft.intent)) if x)
        )

    # ------------------------------------------------------------------ asking

    async def _ask(self, draft: Draft, progress: Any, *, lead: str = "") -> Turn:
        it = draft.intent
        if (
            it.destination
            and it.origin
            and it.start_date
            and it.nights
            and (draft.prefetch is None or draft.prefetch.done())
        ):
            draft.prefetch = asyncio.create_task(self.e.prefetch(it))  # warm partner data while we talk
        if it.destination:
            asyncio.get_running_loop().create_task(self._warm_research(it.destination))
        q = await self._next_question(draft)
        if q is None:
            return await self._compile(draft, progress, lead)
        draft.question = q
        self._save(draft)
        speech = f"{lead} {q.text}".strip()
        return Turn("needs_input", draft.id, speech, question=q, suggestions=q.suggestions)

    async def _warm_research(self, city: str) -> None:
        with contextlib.suppress(Exception):
            await self.e.highlights(city)

    async def _next_question(self, d: Draft) -> Question | None:
        it = d.intent
        today = world.today()
        if not it.destination:
            return self._destination_question(d)
        name = world.city_name(it.destination)
        if not it.start_date or it.start_date <= today:
            fr = _next_fridays(2)
            two_weeks = today + timedelta(days=14)
            if two_weeks in fr:
                two_weeks = fr[-1] + timedelta(days=7)
            opts = [Suggestion(label=_say_date(x), say=f"on {x.strftime('%B')} {x.day}") for x in fr]
            opts.append(
                Suggestion(label=_say_date(two_weeks), say=f"on {two_weeks.strftime('%B')} {two_weeks.day}")
            )
            text = (
                "That date has passed. When would you like to leave?"
                if it.start_date
                else f"When would you like to leave for {name}?"
            )
            return Question(slot="start_date", text=text, suggestions=opts)
        if not it.nights:
            return Question(
                slot="nights",
                text="How many nights?",
                suggestions=[
                    Suggestion(label="2 nights", say="2 nights"),
                    Suggestion(label="3 nights", say="3 nights"),
                    Suggestion(label="5 nights", say="5 nights"),
                ],
            )
        if "travelers" not in it.provenance and not it.traveler_names and "travelers" not in d.asked:
            people = self.e.profile.people
            opts = [
                Suggestion(label="Just me", say="just me"),
                Suggestion(label="2 people", say="for 2 people"),
            ]
            if people:
                opts.insert(
                    1, Suggestion(label="Me, " + " and ".join(people), say="with " + " and ".join(people))
                )
            else:
                opts.append(Suggestion(label="Family of 4", say="for 4 people"))
            return Question(slot="travelers", text="Who's going?", suggestions=opts[:3])
        if not it.origin:
            picks = [c for c in ("SEA", "JFK", "ORD", "LAX") if c != it.destination][:3]
            return Question(
                slot="origin",
                text="Where will you be leaving from?",
                suggestions=[
                    Suggestion(label=world.city_name(c), say=f"from {world.city_name(c)}") for c in picks
                ],
            )
        if d.origin_from_profile and "origin_confirm" not in d.asked:
            home = world.city_name(it.origin)
            others = [c for c in ("JFK", "ORD", "LAX", "SFO") if c not in (it.origin, it.destination)][:2]
            return Question(
                slot="origin_confirm",
                text=f"Leaving from {home}, right?",
                suggestions=[
                    Suggestion(label=f"Yes, from {home}", say=f"from {home}"),
                    *[
                        Suggestion(label=f"From {world.city_name(c)}", say=f"from {world.city_name(c)}")
                        for c in others
                    ],
                ],
            )
        if not any(c.kind == "budget_max" for c in it.constraints) and "budget" not in d.asked:
            return await self._budget_question(d)
        if not _has_pref(it) and "needs" not in d.asked:
            return self._needs_question(it)
        return None

    def _destination_question(self, d: Draft) -> Question:
        it = d.intent
        month = (it.start_date or world.today()).month
        picks = d.region or (["MIA", "SAN", "PHX"] if month in (11, 12, 1, 2, 3) else ["SFO", "JFK", "BNA"])
        text = "Which one sounds best?" if d.region else "Where would you like to go?"
        return Question(
            slot="destination",
            text=text,
            suggestions=[
                Suggestion(label=f"{world.city_name(c)}: {TAGLINES[c]}", say=f"to {world.city_name(c)}")
                for c in picks
            ],
        )

    async def _budget_question(self, d: Draft) -> Question | None:
        est = await self.estimate(d.intent)
        if est is None:  # nothing fits the other rules; the planner will explain why
            d.asked.add("budget")
            return await self._next_question(d)
        low, typical = est
        r = lambda x: int(-(-x // 50) * 50)  # noqa: E731 - round up to $50
        return Question(
            slot="budget",
            text=f"What's your budget? Trips like this start around {fmt_money(r(low))}.",
            suggestions=[
                Suggestion(
                    label=f"Around {fmt_money(r(low * 1.05))}, keep it cheap", say=f"budget ${r(low * 1.05)}"
                ),
                Suggestion(
                    label=f"Around {fmt_money(r(typical * 1.1))}, comfortable",
                    say=f"budget ${r(typical * 1.1)}",
                ),
                Suggestion(label="No fixed budget", say="no fixed budget"),
            ],
        )

    def _needs_question(self, it: TripIntent) -> Question:
        kept = [c.because for c in it.constraints if c.origin == "profile" and c.because]
        lead = ""
        if any(c.kind == "stay_accessible" for c in it.constraints):
            who = next(
                (n for n in it.traveler_names if any(n in (c.because or "") for c in it.constraints)), None
            )
            lead = f"I'll keep everything step-free{' for ' + who if who else ''}. " if kept else ""
        return Question(
            slot="needs",
            text=f"{lead}Anything the place must have? Like a pool, breakfast included, "
            "free parking, a private or shared room, AC or no AC. Or say no preference.",
            suggestions=self._needs_suggestions(it),
        )

    def _needs_suggestions(self, it: TripIntent) -> list[Suggestion]:
        """The three preferences most likely to matter for this particular trip."""
        dest, month = it.destination or "", (it.start_date or world.today()).month
        ranked: list[tuple[str, str]] = []
        if dest in HOT or month in (6, 7, 8):
            ranked.append(("A pool", "with a pool"))
        if it.travelers >= 3 or it.traveler_names:
            ranked.append(("A family room", "family room"))
        if dest in COASTAL:
            ranked.append(("Beachfront or a view", "with a view"))
        if it.origin and world.distance_km(it.origin, dest) < 700:
            ranked.append(("Free parking", "free parking"))
        if it.travelers == 1:
            ranked.append(("Shared room is fine", "shared room"))
        ranked += [
            ("Breakfast included", "breakfast included"),
            ("Private room with AC", "private room with AC"),
            ("A quiet room", "quiet room"),
        ]
        seen, out = set(), []
        for label, say in ranked:
            if label not in seen:
                seen.add(label)
                out.append(Suggestion(label=label, say=say))
        return out[:3]

    async def estimate(self, intent: TripIntent) -> tuple[int, int] | None:
        """Cheapest and typical total for this trip, from live quotes, solved in parallel."""
        probe = intent.model_copy(deep=True)
        probe.constraints = [c for c in probe.constraints if c.kind != "budget_max"]
        if not any(c.kind == "activities_per_day" for c in probe.constraints):
            probe.constraints.append(ActivitiesPerDay(origin="default", min=1, max=2))
        probe.renumber()
        try:
            catalog = await self.e._ground(probe, CallLog())
        except Exception:
            return None

        def total(objective: str) -> int | None:
            p = probe.model_copy(deep=True)
            p.objective = objective  # type: ignore[assignment]
            res = TripModel(p, catalog).solve(time_limit=1.0, workers=4)
            return res.plan.total if res.plan else None

        g = (
            Graph()
            .add("cheapest", lambda _: asyncio.to_thread(total, "cheapest"))
            .add("typical", lambda _: asyncio.to_thread(total, "balanced"))
        )
        out, _ = await g.run()
        if out["cheapest"] is None:
            return None
        return out["cheapest"], out["typical"] or out["cheapest"]

    # ------------------------------------------------------------------ planning

    async def _compile(self, d: Draft, progress: Any, lead: str) -> Turn:
        if d.prefetch is not None:
            with contextlib.suppress(Exception):
                await d.prefetch
        res = await self.e.compile(
            d.intent.utterance, intent=d.intent.model_copy(deep=True), use_profile=False, progress=progress
        )
        res.notes = d.notes + res.notes
        return self._after(d, res, lead)

    def _after(self, d: Draft, res: CompileResult, lead: str = "") -> Turn:
        if res.stored is not None:
            d.plan_id = res.stored.plan_id
            d.intent = res.stored.intent.model_copy(deep=True)
        d.stage = {"planned": "planned", "infeasible": "infeasible"}.get(res.status, d.stage)
        if res.status == "planned":
            self.e.learner.note_plan()
        suggestions: list[Suggestion] = []
        if res.status == "infeasible" and res.stored and res.stored.conflict:
            suggestions = [
                Suggestion(label=f.message[0].upper() + f.message[1:], say=f.id)
                for f in res.stored.conflict.fixits[:3]
            ]
            if not suggestions:  # no single change is enough: let them relax one rule at a time
                from qed.solver.diagnose import _drop_message

                for cid in res.stored.conflict.mus:
                    c = res.stored.intent.by_id(cid)
                    if c is not None and c.origin in ("user", "learned"):
                        msg = _drop_message(c, c.describe())
                        suggestions.append(Suggestion(label=msg[0].upper() + msg[1:], say=f"drop:{cid}"))
                suggestions = suggestions[:3]
        elif res.status == "planned":
            suggestions = [
                Suggestion(label="Book it", say="book it"),
                Suggestion(label="Make it cheaper", say="make it cheaper"),
            ]
            if d.learned:
                suggestions.append(
                    Suggestion(
                        label="Remember these preferences",
                        say="remember that we prefer " + " and ".join(d.learned),
                    )
                )
            else:
                suggestions.append(Suggestion(label="What should we pack?", say="what should we pack"))
        d.question = Question(
            slot="fix" if res.status == "infeasible" else "next", text=res.speech, suggestions=suggestions
        )
        self._save(d)
        speech = res.speech if not lead or lead in ("Okay.", "Got it.") else f"{lead} {res.speech}"
        if res.status == "planned" and res.stored is not None:
            places = (res.stored.catalog.highlights or {}).get("places") or []
            if places:  # one researched suggestion; the full list is on screen
                speech = speech.replace(
                    " Want me to book it?", f" Don't miss {places[0]['name']}. Want me to book it?"
                )
        return Turn(res.status, d.id, speech, question=d.question, result=res, suggestions=suggestions)

    async def _restore(self, d: Draft, progress: Any) -> None:
        """After a restart the plan is gone from memory: rebuild it from the saved conversation."""
        if d.plan_id and d.plan_id not in self.e.plans:
            res = await self.e.compile(
                d.intent.utterance,
                intent=d.intent.model_copy(deep=True),
                use_profile=False,
                progress=progress,
            )
            if res.stored is not None:
                d.plan_id = res.stored.plan_id

    async def _choose_fix(self, d: Draft, text: str, progress: Any) -> Turn:
        await self._restore(d, progress)
        sp = self.e.plans.get(d.plan_id or "")
        fixits = sp.conflict.fixits if sp is not None and sp.conflict is not None else []
        said = self._resolve(text, d.question)
        if said and said.startswith("drop:"):
            new = d.intent.model_copy(deep=True)
            new.constraints = [c for c in new.constraints if c.id != said[5:]]
            new.diagnostics = []
            res = await self.e.compile(
                f"{d.intent.utterance} | {text}", intent=new.renumber(), use_profile=False, progress=progress
            )
            return self._after(d, res)
        fix = next((f for f in fixits if f.id == said), None)
        if fix is None and YES.match(text) and fixits:
            fix = fixits[0]
        if fix is not None:
            return self._after(d, await self.e.apply_fix(d.plan_id, fix.id, progress=progress))  # type: ignore[arg-type]
        return await self._refine(d, text, progress)

    async def _refine(self, d: Draft, text: str, progress: Any) -> Turn:
        await self._restore(d, progress)
        said = self._resolve(text, d.question) or text
        self.e.learner.note_refinement(said)
        res = await self.e.refine(d.plan_id, said, use_profile=False, progress=progress)  # type: ignore[arg-type]
        return self._after(d, res)

    # ------------------------------------------------------------------ understanding answers

    def _resolve(self, text: str, q: Question | None) -> str | None:
        """Map a reply to a suggestion when it picks one; None means 'skip / no preference'."""
        if q is None or not q.suggestions:
            return None if SKIP.match(text) else text
        low = re.sub(r"[^\w\s$,]", " ", text.lower()).strip()
        for word, idx in ORDINALS.items():
            if (
                re.search(rf"\b(?:the\s+)?{re.escape(word)}(?:\s+one|\s+option)?\b", low)
                and len(low.split()) <= 4
            ):
                return q.suggestions[idx].say
        for s in q.suggestions:
            label = re.sub(r"[^\w\s$,]", " ", s.label.lower())
            # A reply that says more than the label ("no AC, shared room is fine") is kept whole.
            if low and (
                low == label
                or (len(low) > 3 and low in label)
                or (label in low and len(low) - len(label) <= 6)
            ):
                return s.say
        # Only near-identical replies (speech-recognition slips), never a longer sentence that contains extra wishes.
        close = difflib.get_close_matches(
            low, [re.sub(r"[^\w\s$,]", " ", s.label.lower()) for s in q.suggestions], n=1, cutoff=0.9
        )
        if close:
            return next(
                s.say for s in q.suggestions if re.sub(r"[^\w\s$,]", " ", s.label.lower()) == close[0]
            )
        if YES.match(text) and q.slot in ("origin_confirm", "fix"):
            return q.suggestions[0].say
        if SKIP.match(text):
            return None
        return text

    @staticmethod
    def _contextualise(said: str, slot: str) -> str:
        """Bare answers ("Miami", "4", "2000") mean something only given the question."""
        s = said.strip().rstrip(".!")
        bare_num = re.fullmatch(r"\$?\s*(\d[\d,]*)", s)
        if slot == "travelers" and bare_num:
            return f"for {bare_num.group(1)} people"
        if slot == "nights" and bare_num:
            return f"{bare_num.group(1)} nights"
        if slot == "budget" and bare_num:
            return f"budget ${bare_num.group(1)}"
        if slot == "budget" and re.fullmatch(r"\d+\s*k", s.lower()):
            return f"budget ${int(s.lower().rstrip('k ').strip()) * 1000}"
        if slot == "destination" and not re.search(r"\b(?:to|in|visit)\b", s.lower()):
            return f"to {s}"
        if slot in ("origin", "origin_confirm") and not re.search(r"\bfrom\b", s.lower()):
            return f"from {s}"
        if slot == "start_date" and not re.search(r"\b(?:on|in|this|next|tomorrow)\b", s.lower()):
            return f"on {s}"
        return said

    def _region_lead(self, text: str, d: Draft) -> str:
        if d.intent.destination:
            return ""
        low = text.lower()
        for region in sorted(REGIONS, key=len, reverse=True):
            if re.search(rf"\b{re.escape(region)}\b", low):
                cities = REGIONS[region]
                if len(cities) == 1:
                    d.intent.destination = cities[0]
                    return f"For {region.title()}, I'll plan {world.city_name(cities[0])}."
                d.region = cities
                return f"A few good options for {region}."
        return ""

    def _unknown_places(self, text: str) -> list[str]:
        out = []
        for m in PLACE_WORDS.finditer(text):
            place = m.group(1).strip()
            low = place.lower()
            if place.split()[0] in NOT_PLACES or low in world.CITY_ALIASES or low in REGIONS:
                continue
            if any(alias in low for alias in world.CITY_ALIASES if len(alias) > 3):
                continue
            out.append(place)
        return out

    def _gap_lead(self, intent: TripIntent) -> str:
        gaps = self.e.learner.note_gaps(intent)
        if not gaps:
            return ""
        g = gaps[0]
        return f"I didn't understand “{g}”. To teach me, say: when I say {g}, I mean, for example, a pool."

    @staticmethod
    def _unsupported_lead(places: list[str]) -> str:
        return (
            f"I can't plan trips involving {' or '.join(places)} yet. I book trips between "
            f"{len(world.CITIES)} US cities."
        )

    @staticmethod
    def _ack(before: TripIntent, after: TripIntent) -> str:
        if after.destination and after.destination != before.destination:
            return f"{world.city_name(after.destination)}, great."
        if after.travelers != before.travelers or after.traveler_names != before.traveler_names:
            return "Just you." if after.travelers == 1 else f"{after.travelers} travelers."
        if after.nights and after.nights != before.nights:
            return f"{after.nights} nights."
        if after.start_date and after.start_date != before.start_date:
            return f"{_say_date(after.start_date)}."
        if any(c.kind == "budget_max" for c in after.constraints) and not any(
            c.kind == "budget_max" for c in before.constraints
        ):
            b = next(c for c in after.constraints if c.kind == "budget_max")
            return f"Budget {fmt_money(b.amount)}."
        if len(after.constraints) > len(before.constraints):
            return "Got it."
        return "Okay."
