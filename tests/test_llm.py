"""The Bedrock escalation path, with the model stubbed: provenance checks and graceful degradation."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from qed.engine import Engine
from qed.partners.client import LocalPartners
from tests.conftest import WHEN

pytestmark = pytest.mark.asyncio


async def test_llm_proposals_must_quote_the_request(settings) -> None:  # type: ignore[no-untyped-def]
    eng = Engine(replace(settings, llm_mode="bedrock"), LocalPartners(), seed_demo_household=False)

    async def fake_parse(utterance, unresolved):  # type: ignore[no-untyped-def]
        from qed.guard import check_llm_provenance

        proposals = [
            {"kind": "category_min", "params": {"category": "park", "count": 1}, "quote": "somewhere green"},
            {"kind": "budget_max", "params": {"amount": 50}, "quote": "cheap as chips"},  # not in the request
        ]
        kept, dropped = check_llm_provenance(utterance, proposals)
        return kept, dropped, ["near the beach"]

    eng.bedrock.parse = fake_parse  # type: ignore[method-assign]
    eng.bedrock.rephrase = lambda *a, **k: _none()  # type: ignore[method-assign]
    res = await eng.compile(f"3 nights from Seattle to Portland on {WHEN}, somewhere green")
    kinds = {(c.kind, c.span.text if c.span else None) for c in res.intent.constraints}
    assert ("category_min", "somewhere green") in kinds
    assert all(c.kind != "budget_max" for c in res.intent.constraints), "a hallucinated rule must be dropped"
    codes = {d.code for d in res.intent.diagnostics}
    assert {"QED-W011", "QED-W012"} <= codes
    assert res.intent.parser == "rules+bedrock"


async def _none() -> None:
    return None


async def test_missing_cloud_credentials_degrade_to_the_deterministic_compiler(settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for k in [k for k in os.environ if k.startswith("AWS_ACCESS") or k == "AWS_PROFILE"]:
        monkeypatch.delenv(k, raising=False)
    eng = Engine(replace(settings, llm_mode="bedrock"), LocalPartners(), seed_demo_household=False)
    res = await eng.compile(f"3 nights from Seattle to Portland on {WHEN} near the water")
    assert res.status == "planned"
    assert eng.bedrock.last_error is not None
