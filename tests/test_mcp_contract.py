"""Live contract tests: real HTTP, real MCP client, the Alexa+ add-on rules."""

from __future__ import annotations

import base64
import hashlib
import secrets

import httpx
import httpx2
import jsonschema
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp_types import ElicitResult

from tests.conftest import WHEN

pytestmark = pytest.mark.asyncio
CLIENT = ("alexa-plus-simulator", "dev-secret-change-me")


def service_token(base: str) -> str:
    r = httpx.post(
        f"{base}/oauth/token", data={"grant_type": "client_credentials", "scope": "mcp:service"}, auth=CLIENT
    )
    assert r.status_code == 200 and r.json()["scope"] == "mcp:service" and "refresh_token" not in r.json()
    return r.json()["access_token"]


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
    bad = httpx.post(
        f"{base}/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "code_verifier": "wrong" * 10},
    )
    assert bad.status_code == 400, "PKCE must reject a wrong verifier"
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
        data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def accept_all(choice_index: int = 0):  # type: ignore[no-untyped-def]
    async def cb(_ctx, params):  # type: ignore[no-untyped-def]
        content = {}
        for key, prop in ((params.requested_schema or {}).get("properties") or {}).items():
            if "enum" in prop:
                content[key] = prop["enum"][choice_index]
            elif prop.get("type") == "boolean":
                content[key] = True
        return ElicitResult(action="accept", content=content)

    return cb


async def decline(_ctx, _params):  # type: ignore[no-untyped-def]
    return ElicitResult(action="decline")


async def run(base: str, token: str, fn, elicit=None):  # type: ignore[no-untyped-def]
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(60))
    async with (
        streamable_http_client(f"{base}/mcp", http_client=client) as st,
        ClientSession(st[0], st[1], elicitation_callback=elicit) as s,
    ):
        init = await s.initialize()
        return await fn(s, init)


async def test_unauthenticated_requests_are_refused_with_discovery(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    r = httpx.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    meta = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"].endswith("/mcp") and meta["authorization_servers"]
    asm = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    assert "S256" in asm["code_challenge_methods_supported"]
    assert set(asm["grant_types_supported"]) == {"client_credentials", "authorization_code"}
    bad = httpx.post(f"{base}/oauth/token", data={"grant_type": "client_credentials"}, auth=("x", "y"))
    assert bad.status_code == 401


async def test_discovery_and_tool_contract(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]

    async def check(s, init):  # type: ignore[no-untyped-def]
        assert init.protocol_version == "2025-11-25"
        tools = (await s.list_tools()).tools
        names = {t.name for t in tools}
        assert {"plan_trip", "refine_trip", "explain_choice", "book_trip", "verify_receipt"} <= names
        for t in tools:
            assert t.description and t.input_schema["type"] == "object"
            assert t.output_schema is not None, f"{t.name} must declare an outputSchema"
        plan = next(t for t in tools if t.name == "plan_trip")
        assert plan.meta["ui"]["resourceUri"] == "ui://qed/trip-card.html"
        card = await s.read_resource("ui://qed/trip-card.html")
        assert card.contents[0].mime_type == "text/html;profile=mcp-app"
        return tools

    await run(base, service_token(base), check)


async def test_plan_output_matches_its_schema(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]

    async def go(s, init):  # type: ignore[no-untyped-def]
        tools = {t.name: t for t in (await s.list_tools()).tools}
        res = await s.call_tool(
            "plan_trip",
            {"request": f"3 nights from Seattle to Portland on {WHEN} for 2 under $2,500, private room"},
        )
        assert not res.is_error
        jsonschema.validate(res.structured_content, tools["plan_trip"].output_schema)
        sc = res.structured_content
        assert sc["status"] == "planned" and sc["planId"]
        assert sc["data"]["proof"]["passed"] == sc["data"]["proof"]["checks"]
        assert len(sc["voiceOptions"]) <= 3
        assert res.content[0].text == sc["speech"], "spoken and on-screen content come from one source"
        assert "plan_trip" not in sc["speech"] and sc["planId"] not in sc["speech"], (
            "never read ids or tool names aloud"
        )
        return sc

    await run(base, service_token(base), go)


async def test_guests_cannot_book_but_linked_accounts_can(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    request = {
        "request": f"2 nights from Boston to New York on {WHEN} for 2 under $2,000, private room with AC"
    }

    async def guest(s, init):  # type: ignore[no-untyped-def]
        plan = await s.call_tool("plan_trip", request)
        res = await s.call_tool("book_trip", {"plan_id": plan.structured_content["planId"]})
        assert res.is_error and res.structured_content["status"] == "needs_account_linking"

    await run(base, service_token(base), guest)

    async def linked(s, init):  # type: ignore[no-untyped-def]
        plan = await s.call_tool("plan_trip", request)
        pid = plan.structured_content["planId"]
        assert plan.structured_content["status"] == "planned", plan.structured_content["speech"]
        booked = await s.call_tool("book_trip", {"plan_id": pid})
        bsc = booked.structured_content
        assert bsc["status"] == "booked", (bsc["status"], bsc["speech"], bsc.get("data", {}).get("steps"))
        again = await s.call_tool("book_trip", {"plan_id": pid})
        assert again.structured_content["status"] == "already_booked", again.structured_content["speech"]
        idx = booked.structured_content["data"]["receipt"]["index"]
        v = await s.call_tool("verify_receipt", {"receipt_index": idx})
        assert v.structured_content["status"] == "verified"

    await run(base, user_token(base), linked, elicit=accept_all())


async def test_declining_the_payment_prompt_books_nothing(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]

    async def go(s, init):  # type: ignore[no-untyped-def]
        plan = await s.call_tool(
            "plan_trip",
            {"request": f"2 nights from Austin to Denver on {WHEN} for 2 under $3,000, private room"},
        )
        res = await s.call_tool("book_trip", {"plan_id": plan.structured_content["planId"]})
        assert res.structured_content["status"] == "cancelled"

    await run(base, user_token(base), go, elicit=decline)


async def test_impossible_requests_offer_fixes_as_choices(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]

    async def go(s, init):  # type: ignore[no-untyped-def]
        res = await s.call_tool(
            "plan_trip",
            {"request": f"3 nights from Seattle to San Francisco on {WHEN} for 2 under $700, shared room"},
        )
        sc = res.structured_content
        assert sc["status"] == "infeasible" and sc["draftId"] and sc["voiceOptions"], (
            "no blocking prompt: fixes come back as options"
        )
        fixed = await s.call_tool("plan_trip", {"request": "the first one", "draft_id": sc["draftId"]})
        assert fixed.structured_content["status"] == "planned", "choosing the first fix should yield a plan"

    await run(base, service_token(base), go, elicit=accept_all())


async def test_cors_allows_the_simulator_origin_only(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    ok = httpx.options(
        f"{base}/mcp",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert (
        "mcp-session-id" in ok.headers.get("access-control-expose-headers", "").lower()
        or ok.status_code == 200
    )
    evil = httpx.options(
        f"{base}/mcp", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert evil.headers.get("access-control-allow-origin") is None


async def test_dns_rebinding_origin_is_rejected(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    r = httpx.post(
        f"{base}/mcp",
        headers={
            "Authorization": f"Bearer {service_token(base)}",
            "Origin": "https://evil.example",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "x", "version": "1"},
            },
        },
    )
    assert r.status_code in (400, 403)


async def test_health_readiness_and_metrics(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    assert httpx.get(f"{base}/healthz").json() == {"status": "ok"}
    ready = httpx.get(f"{base}/readyz").json()
    assert ready["ready"] and ready["ledger"]["ok"]
    metrics = httpx.get(f"{base}/metrics").text
    assert "qed_compile_seconds" in metrics and "qed_stage_seconds" in metrics
    r = httpx.get(f"{base}/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_tokens_are_only_minted_for_this_resource(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]
    wrong = httpx.post(
        f"{base}/oauth/token",
        auth=CLIENT,
        data={
            "grant_type": "client_credentials",
            "scope": "mcp:service",
            "resource": "https://other.example/mcp",
        },
    )
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"
    right = httpx.post(
        f"{base}/oauth/token",
        auth=CLIENT,
        data={"grant_type": "client_credentials", "scope": "mcp:service", "resource": f"{base}/mcp"},
    )
    assert right.status_code == 200


async def test_alexa_compat_401_omits_www_authenticate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from starlette.testclient import TestClient

    from qed.config import Settings
    from qed.partners.client import LocalPartners
    from qed.server.app import create_app

    app = create_app(replace(Settings(), state_dir=tmp_path, alexa_compat_401=True), partners=LocalPartners())
    r = TestClient(app).post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401 and "www-authenticate" not in r.headers


async def test_a_conversation_collects_details_one_question_at_a_time(live_stack) -> None:  # type: ignore[no-untyped-def]
    base = live_stack["base"]

    async def go(s, init):  # type: ignore[no-untyped-def]
        r = (await s.call_tool("plan_trip", {"request": "Book a trip for me"})).structured_content
        assert (
            r["status"] == "needs_input"
            and r["data"]["question"]["slot"] == "destination"
            and len(r["voiceOptions"]) == 3
        )
        draft = r["draftId"]
        for reply in [
            "to Boston",
            f"on {WHEN}",
            "2 nights",
            "just me",
            "from New York",
            "no fixed budget",
            "no AC",
        ]:
            r = (await s.call_tool("plan_trip", {"request": reply, "draft_id": draft})).structured_content
            assert r["draftId"] == draft
            if r["status"] != "needs_input":
                break
        assert r["status"] in ("planned", "infeasible"), r["speech"]

    await run(base, service_token(base), go)
