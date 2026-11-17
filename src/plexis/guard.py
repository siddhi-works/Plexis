"""Guardrails at every boundary.

Input   - normalise, bound and screen what the user says; refuse payment card
          numbers and government IDs (Plexis never needs them); optional Amazon
          Bedrock Guardrails screening.
LLM     - any constraint an LLM proposes must quote the exact words in the
          request that justify it, or it is dropped (provenance check).
Data    - partner free text is untrusted. It never reaches decision logic; the
          scanner only *reports* instruction-like text so the user can see it.
Output  - every number and name spoken must appear in the signed certificate
          (grounded-speech check), so the voice never says something the proof
          doesn't contain.
Logs    - PII redaction before anything is written.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)")
CONTROL_RE = re.compile(
    "[\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)  # escapes, never literal bidi chars

INJECTION_PATTERNS = [
    r"ignore (?:all |the )?(?:previous|prior|above|user'?s?) (?:instructions|budget|request)",
    r"\b(?:system|assistant)\s*(?:note|override|message|prompt)?\s*:",
    r"note to (?:ai|assistants?|agents?)",
    r"\bai (?:assistants?|agents?)\b.*\b(?:must|should|always|select|book|confirm|tell)\b",
    r"pre-?approved",
    r"do not (?:mention|tell|disclose)",
    r"<!--.*?-->",
    r"say (?:all )?other options are",
]
_INJ = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in INJECTION_PATTERNS]


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


@dataclass
class InputVerdict:
    text: str
    allowed: bool
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


def screen_input(raw: str, *, max_chars: int, guardrail: Any | None = None) -> InputVerdict:
    text = unicodedata.normalize("NFKC", raw or "")
    text = CONTROL_RE.sub("", text).strip()
    notes = []
    if not text:
        return InputVerdict(text, False, "empty request")
    if len(text) > max_chars:
        return InputVerdict(text[:max_chars], False, f"request is longer than {max_chars} characters")
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return InputVerdict(
                redact(text),
                False,
                "That looks like a payment card number. Plexis never needs card details; "
                "payment goes through your saved wallet after you confirm.",
            )
    if SSN_RE.search(text):
        return InputVerdict(
            redact(text), False, "Please don't share government ID numbers; Plexis never needs them."
        )
    if guardrail is not None:
        blocked = guardrail(text)
        if blocked:
            return InputVerdict(text, False, blocked)
        notes.append("screened by Amazon Bedrock Guardrails")
    return InputVerdict(text, True, notes=notes)


def redact(text: str) -> str:
    text = CARD_RE.sub("[card]", text)
    text = SSN_RE.sub("[id]", text)
    text = EMAIL_RE.sub("[email]", text)
    return PHONE_RE.sub("[phone]", text)


def scan_untrusted(items: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    """(id, name, text) -> findings. Reporting only: decisions never read this text."""
    findings = []
    for item_id, name, text in items:
        for pat in _INJ:
            m = pat.search(text or "")
            if m:
                findings.append({"itemId": item_id, "name": name, "excerpt": m.group(0)[:90]})
                break
    return findings


def check_llm_provenance(
    utterance: str, proposals: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only proposals whose `quote` appears verbatim (case-insensitive) in the utterance."""
    low = utterance.lower()
    kept, dropped = [], []
    for p in proposals:
        quote = (p.get("quote") or "").strip().lower()
        if quote and len(quote) >= 3 and quote in low:
            kept.append(p)
        else:
            dropped.append(f"{p.get('kind', '?')} (no supporting words: {quote!r})")
    return kept, dropped


NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")


def grounded(speech: str, certificate_text: str) -> tuple[bool, list[str]]:
    """Every number in the speech must appear in the certificate text."""
    cert = certificate_text.replace(",", "")
    missing = []
    for m in NUMBER_RE.finditer(speech):
        token = m.group(0).replace(",", "").lstrip("$").rstrip(".")
        if token and token not in cert:
            missing.append(m.group(0))
    return not missing, missing
