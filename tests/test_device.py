"""The voice-only device: routing utterances to tools, and cleaning up what speech recognition hears."""

from __future__ import annotations

from qed.device.audio import clean_transcript
from qed.device.device import Context, route


def test_answers_mid_conversation_stay_in_the_conversation() -> None:
    c = Context(draft_id="abc123abc123", status="needs_input")
    assert route("the first one", c) == (
        "plan_trip",
        {"request": "the first one", "draft_id": "abc123abc123"},
    )


def test_book_it_books_but_book_a_trip_plans() -> None:
    c = Context(plan_id="p1", draft_id="d1", status="planned")
    assert route("book it", c)[0] == "book_trip"
    assert route("book a trip to Boston", c)[0] == "plan_trip"


def test_memory_and_questions_route_to_their_tools() -> None:
    c = Context(plan_id="p1", status="planned")
    assert route("remember that we prefer no AC", c)[0] == "household_memory"
    assert route("why not the Harbor Hotel?", c)[0] == "explain_choice"
    assert route("what should we pack", c)[0] == "travel_brief"


def test_common_mishearings_are_fixed() -> None:
    assert clean_transcript("Plant three nights to Miami.") == "plan three nights to Miami"
    assert clean_transcript("Book at.") == "book it"
