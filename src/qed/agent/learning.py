"""How Plexis gets better the more it's used, without retraining a model.

1. A personal phrasebook. "When I say beach vibes, I mean beachfront with a
   pool" teaches a phrase; Bedrock's verified interpretations of unknown
   phrases are saved the same way, so next time no model call is needed.
2. Gaps. Words the compiler didn't understand are counted and the customer
   is invited to teach them.
3. Mistakes. "No, I meant ..." corrections are logged with what was heard and
   what was meant, so recurring misreadings show up in the learning report.
4. Habits. Repeated "make it cheaper" requests make value the default
   objective for new trips; bookings already tune activity and cuisine taste.

Everything learned is visible, labelled in plans (origin "learned"), and can
be forgotten.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from qed.compiler import rules
from qed.ir import CONSTRAINT_KINDS, Span, TripIntent

TEACH = re.compile(
    r"^\s*(?:remember that\s+)?(?:when|if)\s+i\s+say\s+[\"'“]?(.+?)[\"'”]?\s*,?\s+i\s+mean\s+(.+?)\s*[.!]?\s*$"
    r"|^\s*[\"'“](.+?)[\"'”]\s+means\s+(.+?)\s*[.!]?\s*$",
    re.IGNORECASE,
)
CORRECTION = re.compile(
    r"^\s*(?:no|nope|not that|that'?s (?:wrong|not right)|i (?:said|meant)|actually)\b[,.]?\s*", re.IGNORECASE
)
CHEAPER = re.compile(
    r"\b(?:cheaper|less expensive|save (?:some )?money|lower (?:the )?cost|too expensive)\b", re.IGNORECASE
)


class Learner:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "learning.json"
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "lexicon": {},
            "mistakes": [],
            "gaps": {},
            "habits": {"cheaper": 0, "plans": 0},
        }
        if self.path.exists():
            self.data.update(orjson.loads(self.path.read_bytes()))

    def _save(self) -> None:
        with self._lock:
            self.path.write_bytes(orjson.dumps(self.data, option=orjson.OPT_INDENT_2))

    # ------------------------------------------------------------- phrasebook

    def teach(self, text: str) -> tuple[str, list[str]] | None:
        """Returns (phrase, rule descriptions) when the text teaches a phrase."""
        m = TEACH.match(text)
        if not m:
            return None
        phrase = (m.group(1) or m.group(3)).strip().lower()
        meaning = (m.group(2) or m.group(4)).strip()
        parsed = rules.parse(meaning)
        cons = [c for c in parsed.constraints if c.kind != "activities_per_day"]
        if not cons or len(phrase) < 3:
            return phrase, []
        self.data["lexicon"][phrase] = {
            "constraints": [c.model_dump(mode="json", exclude={"span", "id"}) for c in cons],
            "meaning": meaning,
            "source": "taught",
            "uses": 0,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self.data["gaps"].pop(phrase, None)
        self._save()
        return phrase, [c.describe() for c in cons]

    def learn_from_model(self, quote: str, constraint: Any) -> None:
        """Keep a Bedrock interpretation (already checked against the customer's own words)."""
        phrase = quote.strip().lower()
        if len(phrase) < 3 or phrase in self.data["lexicon"]:
            return
        self.data["lexicon"][phrase] = {
            "constraints": [constraint.model_dump(mode="json", exclude={"span", "id"})],
            "meaning": constraint.describe(),
            "source": "Amazon Bedrock",
            "uses": 0,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._save()

    def apply(self, intent: TripIntent) -> list[str]:
        """Add constraints for phrases the customer taught. Returns notes for the response."""
        notes: list[str] = []
        low = intent.utterance.lower()
        have = {
            (c.kind, getattr(c, "amenity", None), getattr(c, "rule", None), getattr(c, "diet", None))
            for c in intent.constraints
        }
        for phrase, entry in self.data["lexicon"].items():
            m = re.search(rf"\b{re.escape(phrase)}\b", low)
            if not m:
                continue
            added = False
            for raw in entry["constraints"]:
                cls = CONSTRAINT_KINDS.get(raw.get("kind", ""))
                if cls is None:
                    continue
                c = cls(**{k: v for k, v in raw.items() if k not in ("origin", "because")})
                sig = (
                    c.kind,
                    getattr(c, "amenity", None),
                    getattr(c, "rule", None),
                    getattr(c, "diet", None),
                )
                if sig in have:
                    continue
                c.origin = "learned"  # type: ignore[assignment]
                c.because = f"you taught me “{phrase}” means {entry['meaning']}"
                c.span = Span(start=m.start(), end=m.end(), text=intent.utterance[m.start() : m.end()])
                intent.constraints.append(c)
                have.add(sig)
                added = True
            if added:
                entry["uses"] = entry.get("uses", 0) + 1
                notes.append(f"“{phrase}” means {entry['meaning']} (learned)")
                intent.diagnostics = [
                    d
                    for d in intent.diagnostics
                    if not (d.code == "QED-W001" and d.span and d.span.text.lower() in phrase)
                ]
        if notes:
            self._save()
        return notes

    # ------------------------------------------------------------- gaps, mistakes, habits

    def note_gaps(self, intent: TripIntent) -> list[str]:
        """Phrases the compiler couldn't use. Counted, so frequent ones can be taught or built in."""
        gaps = [
            d.span.text
            for d in intent.diagnostics
            if d.code == "QED-W001" and d.span and d.span.text.lower() not in self.data["lexicon"]
        ]
        for g in gaps:
            self.data["gaps"][g.lower()] = self.data["gaps"].get(g.lower(), 0) + 1
        if gaps:
            self._save()
        return gaps

    def note_correction(self, text: str, heard: dict[str, Any], meant: dict[str, Any]) -> None:
        changed = {k: (heard.get(k), v) for k, v in meant.items() if heard.get(k) != v}
        if not changed:
            return
        self.data["mistakes"].append(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "said": text[:160],
                "changes": {k: {"heard": str(a), "meant": str(b)} for k, (a, b) in changed.items()},
            }
        )
        self.data["mistakes"] = self.data["mistakes"][-200:]
        self._save()

    def note_refinement(self, text: str) -> None:
        if CHEAPER.search(text):
            self.data["habits"]["cheaper"] = self.data["habits"].get("cheaper", 0) + 1
            self._save()

    def note_plan(self) -> None:
        self.data["habits"]["plans"] = self.data["habits"].get("plans", 0) + 1
        self._save()

    @property
    def prefers_value(self) -> bool:
        h = self.data["habits"]
        return h.get("cheaper", 0) >= 2 and h.get("cheaper", 0) * 3 >= max(1, h.get("plans", 0))

    def forget(self, phrase: str | None = None) -> int:
        if phrase:
            return 1 if self.data["lexicon"].pop(phrase.lower().strip(" \"'"), None) is not None else 0
        n = len(self.data["lexicon"])
        self.data = {"lexicon": {}, "mistakes": [], "gaps": {}, "habits": {"cheaper": 0, "plans": 0}}
        self._save()
        return n

    def report(self) -> dict[str, Any]:
        lex = self.data["lexicon"]
        top_gaps = sorted(self.data["gaps"].items(), key=lambda kv: -kv[1])[:5]
        return {
            "phrases": [
                {"phrase": p, "meaning": e["meaning"], "source": e["source"], "uses": e.get("uses", 0)}
                for p, e in lex.items()
            ],
            "mistakesLogged": len(self.data["mistakes"]),
            "recentMistakes": self.data["mistakes"][-3:],
            "unknownPhrases": [{"phrase": p, "times": n} for p, n in top_gaps],
            "prefersValue": self.prefers_value,
            "habits": self.data["habits"],
        }

    def summary(self) -> str:
        r = self.report()
        parts = []
        if r["phrases"]:
            parts.append(
                "I know your phrases: "
                + "; ".join(f"“{p['phrase']}” means {p['meaning']}" for p in r["phrases"][:3])
            )
        if r["prefersValue"]:
            parts.append("you usually ask for cheaper options, so I start with the best value")
        if r["mistakesLogged"]:
            parts.append(f"I've logged {r['mistakesLogged']} correction(s) to avoid repeating them")
        if r["unknownPhrases"]:
            parts.append(
                "I still don't understand "
                + ", ".join(f"“{g['phrase']}”" for g in r["unknownPhrases"][:2])
                + "; teach me with “when I say …, I mean …”"
            )
        return (". ".join(parts) + ".") if parts else "I haven't learned anything new yet."
