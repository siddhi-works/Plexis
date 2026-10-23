"""End-to-end smoke test against a running `qed dev`: OAuth tiers, discovery, plan, book, disruption."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import sys
import time

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def service_token() -> str:
    r = httpx.post(
        f"{BASE}/oauth/token",
        data={"grant_type": "client_credentials", "scope": "mcp:service"},
        auth=("alexa-plus-simulator", "dev-secret-change-me"),
    )
    r.raise_for_status()
    return r.json()["access_token"]


def user_token() -> str:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = httpx.get(
        f"{BASE}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "alexa-plus-simulator",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "format": "json",
        },
    ).json()["code"]
    r = httpx.post(
        f"{BASE}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": "alexa-plus-simulator",
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def elicit(_ctx, params):  # type: ignore[no-untyped-def]
    print("   [elicit]", params.message[:110])
    schema = params.requested_schema or {}
    content = {}
    for key, prop in (schema.get("properties") or {}).items():
        if "enum" in prop:
            content[key] = prop["enum"][0]
        elif prop.get("type") == "boolean":
            content[key] = True
    return ElicitResult(action="accept", content=content)


notes: list[str] = []


async def on_message(msg):  # type: ignore[no-untyped-def]
    notes.append(str(msg)[:160])


async def session(token: str):  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return client


async def main() -> None:
    r = httpx.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    print("unauthenticated ->", r.status_code, r.headers.get("www-authenticate", "")[:80])
    meta = httpx.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
    print(
        "protected-resource metadata ->",
        meta.status_code,
        meta.json() if meta.status_code == 200 else meta.text[:80],
    )

    import httpx2  # the SDK's transport client

    tok = user_token()
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=httpx2.Timeout(60))
    async with (
        streamable_http_client(f"{BASE}/mcp", http_client=client) as streams,
        ClientSession(streams[0], streams[1], elicitation_callback=elicit, message_handler=on_message) as s,
    ):
        init = await s.initialize()
        print(
            "protocol",
            init.protocol_version,
            "| server",
            init.server_info.name,
            "| apps ext:",
            bool((init.capabilities.extensions or {}).get("io.modelcontextprotocol/ui")),
        )
        tools = await s.list_tools()
        print("tools:", [t.name for t in tools.tools])
        print(" plan_trip _meta:", tools.tools[0].meta)
        t0 = time.perf_counter()
        res = await s.call_tool(
            "plan_trip",
            {
                "request": "Plan a 3 night trip to San Francisco on Nov 13 with Grandma and Maya under $3,000, at least one Thai dinner and a museum"
            },
        )
        sc = res.structured_content
        print(
            f"plan_trip {round((time.perf_counter() - t0) * 1000)}ms -> {sc['status']} | {sc['speech'][:140]}"
        )
        print(" stages:", [(x["name"], x["durationMs"]) for x in sc["trace"]["stages"]])
        print(" result _meta:", res.meta)
        pid = sc["planId"]
        res = await s.call_tool(
            "plan_trip",
            {
                "request": "Plan a 3 night trip from Seattle to San Francisco on Nov 13 for 2 under $1,400, wheelchair accessible hotel rated 4.7+"
            },
        )
        print("impossible ->", res.structured_content["status"], "|", res.structured_content["speech"][:160])
        res = await s.call_tool("explain_choice", {"plan_id": pid, "question": "why not the Copper Hotel?"})
        print("why-not ->", res.structured_content["speech"])
        res = await s.call_tool("book_trip", {"plan_id": pid})
        sc = res.structured_content
        print("book ->", sc["status"], "|", sc["speech"][:120])
        trip = sc["tripId"]
        res = await s.call_tool("book_trip", {"plan_id": pid})
        print("book again ->", res.structured_content["status"])
        res = await s.call_tool("verify_receipt", {"receipt_index": sc["data"]["receipt"]["index"]})
        print("verify ->", res.structured_content["speech"])
        res = await s.call_tool("travel_brief", {"plan_id": pid})
        print("brief ->", res.structured_content["speech"][:140])
        res = await s.call_tool(
            "call_front_desk",
            {"plan_id": pid, "questions": ["Can we check in early?", "Is there a roll-in shower?"]},
        )
        print("call ->", res.structured_content["speech"][:160])
        print("trip", trip)
        card = await s.read_resource("ui://qed/trip-card.html")
        print("card resource mime:", card.contents[0].mime_type, len(card.contents[0].text))
    print("notifications:", notes[:3])


if __name__ == "__main__":
    asyncio.run(main())
