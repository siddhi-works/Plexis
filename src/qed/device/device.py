"""A voice-only Alexa+-style device for the Plexis add-on. No screen, no browser.

It behaves like the Alexa+ side of an add-on: it authenticates with the
two-tier OAuth flow, speaks MCP 2025-11-25 over Streamable HTTP, routes each
utterance to the right tool, answers the add-on's confirmation questions by
voice (elicitation), and reads proactive alerts (a cancelled flight) aloud.

    qed voice                 # microphone + spoken replies
    qed voice --text          # type instead of talking (testing)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from qed.device.audio import Ears, Voice

CLIENT = ("alexa-plus-simulator", "dev-secret-change-me")
STOP = re.compile(r"^\s*(?:stop|goodbye|good bye|bye|exit|quit|that'?s all|nothing else)\b", re.IGNORECASE)
YES = re.compile(
    r"\b(?:yes|yeah|yep|sure|ok(?:ay)?|go ahead|do it|confirm|book it|please do)\b", re.IGNORECASE
)
NO = re.compile(r"\b(?:no|nope|nah|cancel|don'?t|never mind)\b", re.IGNORECASE)
ORD = {"first": 0, "second": 1, "third": 2, "last": -1}
BOOK_IT = re.compile(
    r"^\s*(?:yes,?\s*|ok(?:ay)?,?\s*)?(?:please\s+)?(?:book|reserve|confirm)(?:\s+(?:it|this|that|the trip))?"
    r"\s*[.!]?\s*$",
    re.IGNORECASE,
)


@dataclass
class Context:
    plan_id: str | None = None
    draft_id: str | None = None
    trip_id: str | None = None
    receipt: int | None = None
    status: str | None = None
    alerts: list[str] = field(default_factory=list)


def route(text: str, c: Context) -> tuple[str, dict[str, Any]]:
    """Which add-on tool serves this utterance (the Alexa+ orchestration step)."""
    low = text.lower()
    talking = c.draft_id and c.status in ("needs_input", "infeasible")
    if re.match(r"^\s*(?:please\s+)?(?:remember|don'?t forget)\b", low):
        return "household_memory", {
            "action": "remember",
            "statement": re.sub(r"^.*?(?:remember|forget)\s+(?:that\s+)?", "", text),
        }
    if re.search(r"\bwhat do you (?:know|remember)\b", low):
        return "household_memory", {"action": "list"}
    if talking:
        return "plan_trip", {"request": text, "draft_id": c.draft_id}
    if re.search(r"\b(?:rebook|switch (?:me|us) to)\b", low) or (c.alerts and YES.search(low)):
        return "trip_status", {"accept_rebooking": True, **({"trip_id": c.trip_id} if c.trip_id else {})}
    if re.search(r"\b(?:trip status|any updates|flight status|is my flight)\b", low):
        return "trip_status", {"trip_id": c.trip_id} if c.trip_id else {}
    if re.search(r"\b(?:verify|prove)\b", low) and c.receipt is not None:
        return "verify_receipt", {"receipt_index": c.receipt}
    if c.plan_id:
        if BOOK_IT.match(text):
            return "book_trip", {"plan_id": c.plan_id}
        if re.search(r"\bwhy\b", low):
            return "explain_choice", {"plan_id": c.plan_id, "question": text}
        if re.search(r"\b(?:weather|better dates|rain)\b", low):
            return "check_better_dates", {"plan_id": c.plan_id}
        if re.search(r"\b(?:pack|wear|bring|outfit)\b", low):
            return "travel_brief", {"plan_id": c.plan_id}
        if re.search(r"\b(?:call|phone)\b.*\b(?:hotel|front desk|property)\b", low):
            qs = [
                q.strip()
                for q in re.split(
                    r"\band\b|,|\?",
                    re.sub(
                        r"^.*?\b(?:ask|call)\b(?: the)?(?: hotel| front desk)?(?: and ask)?(?: if| whether)?",
                        "",
                        text,
                    ),
                )
                if len(q.strip()) > 3
            ]
            return "call_front_desk", {"plan_id": c.plan_id, "questions": qs or ["Can we check in early?"]}
    return "plan_trip", {"request": text, **({"draft_id": c.draft_id} if c.draft_id else {})}


def user_token(base: str) -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = httpx.get(
        f"{base}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": CLIENT[0],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "format": "json",
        },
    ).json()["code"]
    r = httpx.post(
        f"{base}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": CLIENT[0],
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


class Device:
    def __init__(self, base: str, ears: Ears, voice: Voice) -> None:
        self.base = base.rstrip("/")
        self.ears, self.voice = ears, voice
        self.ctx = Context()

    async def hear(self) -> str:
        return await asyncio.to_thread(self.ears.listen)

    async def say(self, text: str) -> None:
        if text:
            await asyncio.to_thread(self.voice.say, text)

    async def on_elicit(self, _ctx: Any, params: Any) -> Any:
        """The add-on asks a question mid-tool (payment confirmation, consent): answer it by voice."""
        from mcp_types import ElicitResult

        props = (params.requested_schema or {}).get("properties") or {}
        key = next(iter(props), None)
        await self.say(params.message)
        for _ in range(2):
            heard = await self.hear()
            print(f"  heard: {heard!r}")
            if key and props[key].get("type") == "boolean":
                if YES.search(heard):
                    return ElicitResult(action="accept", content={key: True})
                if NO.search(heard):
                    return ElicitResult(action="decline")
                await self.say("Please say yes or no.")
                continue
            if key and "enum" in props[key]:
                opts = props[key]["enum"]
                low = heard.lower()
                pick = next((opts[i] for w, i in ORD.items() if re.search(rf"\b{w}\b", low)), None) or next(
                    (o for o in opts if o.lower() in low or low in o.lower()), None
                )
                if pick:
                    return ElicitResult(action="accept", content={key: pick})
                await self.say("Which one? " + ", or ".join(opts[:3]) + ".")
                continue
            return ElicitResult(action="decline")
        return ElicitResult(action="cancel")

    async def on_log(self, params: Any) -> None:
        data = getattr(params, "data", None)
        if isinstance(data, dict) and data.get("type") == "qed/disruption":
            self.ctx.alerts.append(data["message"])
            await self.say("Trip alert. " + data["message"])

    async def run(self) -> None:
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        token = await asyncio.to_thread(user_token, self.base)
        client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(90))
        async with (
            streamable_http_client(f"{self.base}/mcp", http_client=client) as st,
            ClientSession(
                st[0], st[1], elicitation_callback=self.on_elicit, logging_callback=self.on_log
            ) as s,
        ):
            init = await s.initialize()
            print(f"  connected to add-on '{init.server_info.name}' over MCP {init.protocol_version}")
            await self.say(
                "Hi! I'm Plexis, your trip agent. Tell me where you'd like to go, or just say, book a trip."
            )
            misses = 0
            while True:
                heard = await self.hear()
                if not heard:
                    misses += 1
                    if misses >= 3:
                        await self.say("I'll be here when you need me. Goodbye!")
                        return
                    await self.say("Sorry, I didn't catch that.")
                    continue
                misses = 0
                print(f"  heard: {heard!r}")
                if re.fullmatch(
                    r"\s*(?:thanks?(?: you)?(?: (?:so|very) much)?|um+|uh+|hmm+)\s*[.!]?\s*", heard, re.I
                ) and self.ctx.status not in ("needs_input", "infeasible"):
                    await self.say("You're welcome!")
                    continue
                if STOP.match(heard):
                    await self.say("Okay. Have a great trip!")
                    return
                tool, args = route(heard, self.ctx)
                try:
                    res = await s.call_tool(tool, args)
                except Exception as exc:
                    await self.say(f"Something went wrong reaching the trip service: {exc}")
                    continue
                sc = res.structured_content or {}
                self._remember(sc)
                speech = sc.get("speech") or (res.content[0].text if res.content else "")
                opts = sc.get("voiceOptions") or []
                if sc.get("status") in ("needs_input", "infeasible") and opts:
                    speech += " You can say, " + ", or ".join(o.split(":")[0] for o in opts[:3]) + "."
                await self.say(speech)

    def _remember(self, sc: dict[str, Any]) -> None:
        c = self.ctx
        c.plan_id = sc.get("planId") or c.plan_id
        c.draft_id = sc.get("draftId") or c.draft_id
        c.trip_id = sc.get("tripId") or c.trip_id
        c.status = sc.get("status") or c.status
        receipt = (sc.get("data") or {}).get("receipt")
        if isinstance(receipt, dict) and "index" in receipt:
            c.receipt = receipt["index"]
        if sc.get("status") == "booked":
            c.alerts.clear()


def main(base: str, *, text: bool, mute: bool, model: str, always_on: bool = False) -> None:
    from qed.device.audio import MicEars, PrintVoice, SystemVoice, TextEars

    ears: Ears = TextEars() if text else MicEars(model=model, push_to_talk=not always_on)
    voice: Voice = PrintVoice() if mute else SystemVoice()
    asyncio.run(Device(base, ears, voice).run())
