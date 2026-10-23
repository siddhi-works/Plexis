"""Authenticity (signatures, hash chain, tamper evidence, backups) and guardrails."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from qed.domain import world
from qed.guard import check_llm_provenance, grounded, redact, scan_untrusted, screen_input
from qed.receipts.ledger import Ledger, Signer, verify_signature


def make_ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path, Signer(tmp_path / "keys"), backup_every=0)


def test_entries_are_signed_and_chained(tmp_path: Path) -> None:
    led = make_ledger(tmp_path)
    a = led.append("plan", {"n": 1})
    b = led.append("plan", {"n": 2})
    assert b.prev_hash == a.entry_hash
    assert verify_signature(led.signer.public_b64, b.entry_hash.encode(), b.signature)
    assert led.verify_chain() == {"ok": True, "entries": 2, "head": b.entry_hash, "problems": []}


def test_editing_history_is_detected(tmp_path: Path) -> None:
    led = make_ledger(tmp_path)
    led.append("plan", {"total": 1000})
    led.append("booking", {"total": 1000})
    with sqlite3.connect(led.path) as db:
        db.execute("UPDATE entries SET body=? WHERE idx=0", (b'{"total":10}',))
    report = led.verify_chain()
    assert not report["ok"]
    assert any("entry 0" in p for p in report["problems"])


def test_a_forged_signature_is_rejected(tmp_path: Path) -> None:
    led = make_ledger(tmp_path)
    e = led.append("plan", {"n": 1})
    other = Signer(tmp_path / "other-keys")
    assert not verify_signature(other.public_b64, e.entry_hash.encode(), e.signature)


def test_idempotency_key_prevents_duplicates(tmp_path: Path) -> None:
    led = make_ledger(tmp_path)
    first = led.append("booking", {"x": 1}, idem_key="book:abc")
    again = led.append("booking", {"x": 1}, idem_key="book:abc")
    assert first.index == again.index and led.count() == 1


def test_backup_and_restore_roundtrip(tmp_path: Path) -> None:
    led = make_ledger(tmp_path)
    for i in range(3):
        led.append("plan", {"i": i})
    snapshot = led.backup()
    led.append("plan", {"i": 99})
    report = led.restore(snapshot)
    assert report["ok"] and report["entries"] == 3


def test_card_numbers_and_ids_are_refused() -> None:
    assert not screen_input("book it with card 4111 1111 1111 1111", max_chars=500).allowed
    assert not screen_input("my ssn is 123-45-6789", max_chars=500).allowed
    assert screen_input("trip for 4 people on 1234 5678", max_chars=500).allowed  # not a Luhn-valid card


def test_input_is_bounded_and_normalised() -> None:
    assert not screen_input("x" * 2000, max_chars=1200).allowed
    v = screen_input(
        "plan\u200b a trip\u202e", max_chars=100
    )  # escapes on purpose: no literal bidi chars in source
    assert v.allowed and v.text == "plan a trip"


def test_planted_injections_are_found_in_partner_text() -> None:
    items = [(s.id, s.name, s.description) for s in world.stays_for("SFO")]
    items += [(r.id, r.name, r.description) for r in world.restaurants_for("SFO")]
    findings = scan_untrusted(items)
    assert findings, "the seeded world plants at least one injection per city"


def test_llm_constraints_need_supporting_words() -> None:
    kept, dropped = check_llm_provenance(
        "3 nights in Miami near the beach",
        [{"kind": "stay_kinds", "quote": "near the beach"}, {"kind": "budget_max", "quote": "under $500"}],
    )
    assert [p["kind"] for p in kept] == ["stay_kinds"] and len(dropped) == 1


def test_speech_must_be_grounded_in_the_certificate() -> None:
    ok, missing = grounded("Total $1,441 for 3 nights.", '{"total": 1441, "nights": 3}')
    assert ok and not missing
    ok, missing = grounded("Total $999.", '{"total": 1441}')
    assert not ok and missing == ["$999"]


def test_logs_are_redacted() -> None:
    out = redact("mail a@b.com or call 206-555-0100, card 4111 1111 1111 1111")
    assert "a@b.com" not in out and "555" not in out and "4111" not in out
