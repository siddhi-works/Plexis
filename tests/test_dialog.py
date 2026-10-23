"""The conversational agent: one question at a time, suggestions, context, forgotten needs."""

from __future__ import annotations

import pytest

from qed.agent.dialog import Dialog
from qed.engine import Engine
from tests.conftest import WHEN

pytestmark = pytest.mark.asyncio


async def talk(dialog: Dialog, *replies: str):  # type: ignore[no-untyped-def]
    draft, turns = None, []
    for r in replies:
        t = await dialog.turn(r, draft)
        draft = t.draft_id
        turns.append(t)
    return turns


async def test_vague_request_gets_one_question_with_suggestions(engine: Engine) -> None:
    (t,) = await talk(Dialog(engine), "Book a trip for me")
    assert t.status == "needs_input" and t.question.slot == "destination"
    assert len(t.suggestions) == 3 and "?" in t.speech


async def test_context_is_kept_across_turns(engine: Engine) -> None:
    turns = await talk(Dialog(engine), "plan a trip to Miami", f"{WHEN} for 3 nights")
    assert turns[-1].question.slot != "destination", "the second answer must not forget Miami"


async def test_ambiguous_places_get_choices(engine: Engine) -> None:
    turns = await talk(Dialog(engine), "Book a trip for me", "somewhere warm", "the second one")
    assert [s.label.split(":")[0] for s in turns[1].suggestions] == ["Miami", "San Diego", "Phoenix"]
    assert "San Diego" in turns[2].speech


async def test_a_bare_number_answers_the_question_not_an_option(engine: Engine) -> None:
    d = Dialog(engine)
    turns = await talk(d, "Book a trip to Boston", "the first one", "3")
    assert d.drafts[turns[-1].draft_id].intent.nights == 3


async def test_unsupported_places_are_explained(engine: Engine) -> None:
    (t,) = await talk(Dialog(engine), "Book a trip for me from Pune to Uttarakhand")
    assert "Uttarakhand" in t.speech and t.status == "needs_input"


async def test_budget_suggestions_are_priced_from_real_quotes(engine: Engine) -> None:
    d = Dialog(engine)
    turns = await talk(d, f"Plan 2 nights from Seattle to Portland on {WHEN} for 2 people")
    t = turns[-1]
    assert t.question.slot == "budget"
    low, typical = await d.estimate(d.drafts[t.draft_id].intent)
    assert low <= typical and "$" in t.suggestions[0].label


async def test_forgotten_room_needs_are_asked_and_respected(engine: Engine) -> None:
    turns = await talk(
        Dialog(engine),
        f"Plan 3 nights from Seattle to Miami on {WHEN} for 2 people under $5,000",
        "no AC, shared room is fine",
    )
    assert turns[0].question.slot == "needs"
    final = turns[-1]
    assert final.status in ("planned", "infeasible")
    rules = {(c.kind, getattr(c, "rule", None)) for c in final.result.intent.constraints}
    assert ("stay_rule", "no_air_conditioning") in rules and ("stay_kinds", None) in rules
    if final.status == "planned":
        stay = final.result.stored.plan.stay.stay
        assert not stay.air_conditioned and stay.kind == "shared_room"
        assert any("Remember" in s.label for s in final.suggestions), "offer to learn the preference"


async def test_household_needs_apply_when_people_are_named_later(engine: Engine) -> None:
    d = Dialog(engine)
    turns = await talk(d, f"Plan 3 nights to San Francisco on {WHEN}", "me Grandma and Maya")
    kinds = {(c.kind, c.origin) for c in d.drafts[turns[-1].draft_id].intent.constraints}
    assert ("stay_accessible", "profile") in kinds and ("diet_all", "profile") in kinds


async def test_choosing_a_fix_by_voice(engine: Engine) -> None:
    turns = await talk(
        Dialog(engine),
        f"3 nights from Seattle to San Francisco on {WHEN} for 2 under $700, shared room",
        "the first one",
    )
    assert turns[0].status == "infeasible" and turns[0].suggestions
    assert turns[1].status == "planned"


async def test_remembered_room_preferences_apply_next_time(engine: Engine) -> None:
    from qed.companion.profile import interpret

    for cand in interpret("we prefer no AC and shared rooms"):
        engine.profile.remember(cand, "we prefer no AC and shared rooms")
    (t,) = await talk(Dialog(engine), f"Plan 2 nights from Seattle to Portland on {WHEN} for 2 under $3,000")
    rules = (
        {(c.kind, getattr(c, "rule", None), c.origin) for c in t.result.intent.constraints}
        if t.result
        else set()
    )
    assert t.status in ("planned", "infeasible"), "the room question is skipped when memory already knows"
    assert ("stay_rule", "no_air_conditioning", "profile") in rules


async def test_amenities_are_asked_for_and_respected(engine: Engine) -> None:
    turns = await talk(
        Dialog(engine),
        f"Plan 3 nights from Seattle to Miami on {WHEN} for 2 people under $6,000",
        "a pool and breakfast included, no spa",
    )
    assert turns[0].question.slot == "needs" and len(turns[0].suggestions) == 3
    final = turns[-1]
    wanted = {(c.amenity, c.want) for c in final.result.intent.constraints if c.kind == "stay_amenity"}
    assert wanted == {("pool", True), ("breakfast_included", True), ("spa", False)}
    if final.status == "planned":
        am = final.result.stored.plan.stay.stay.amenities
        assert "pool" in am and "breakfast_included" in am and "spa" not in am


async def test_teaching_a_phrase_is_used_on_the_next_trip(engine: Engine) -> None:
    d = Dialog(engine)
    (t,) = await talk(d, "when I say beach vibes, I mean beachfront with a pool")
    assert t.status == "learned" and "beachfront" in t.speech
    (t2,) = await talk(
        d, f"Plan 3 nights from Seattle to San Diego on {WHEN} for 2 under $5,000, beach vibes"
    )
    learned = [c for c in d.drafts[t2.draft_id].intent.constraints if c.origin == "learned"]
    assert {c.amenity for c in learned} == {"beachfront", "pool"}
    assert engine.learner.report()["phrases"][0]["uses"] == 1


async def test_unknown_words_invite_teaching(engine: Engine) -> None:
    first, then = await talk(
        Dialog(engine),
        f"Plan 3 nights from Seattle to Boston on {WHEN} for 2 under $3,000, kindly",
        "somewhere cozy",
    )
    assert "teach me" not in first.speech, "no lecture on the first request"
    assert "teach me" in then.speech and engine.learner.report()["unknownPhrases"]


async def test_corrections_are_fixed_and_logged(engine: Engine) -> None:
    d = Dialog(engine)
    turns = await talk(d, "Book a trip to Boston", "the first one", "2", "no, I meant 4 nights")
    assert d.drafts[turns[-1].draft_id].intent.nights == 4
    assert engine.learner.report()["mistakesLogged"] == 1


async def test_repeated_cheaper_requests_become_the_default(engine: Engine) -> None:
    for _ in range(2):
        engine.learner.note_refinement("make it cheaper")
    engine.learner.note_plan()
    d = Dialog(engine)
    (t,) = await talk(d, f"Plan 2 nights from Seattle to Portland on {WHEN}")
    assert d.drafts[t.draft_id].intent.objective == "cheapest"


async def test_a_conversation_survives_a_server_restart(settings) -> None:  # type: ignore[no-untyped-def]
    from qed.partners.client import LocalPartners

    first = Dialog(Engine(settings, LocalPartners()))
    (t,) = await talk(
        first, f"Plan 2 nights from Seattle to Portland on {WHEN} for 2 under $3,000, private room"
    )
    restarted = Dialog(Engine(settings, LocalPartners()))  # same disk state, empty memory
    t2 = await restarted.turn("make it cheaper", t.draft_id)
    assert t2.status in ("planned", "infeasible")
