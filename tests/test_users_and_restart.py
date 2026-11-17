"""Each customer's memory, plans and trips are their own, and survive a restart."""

from __future__ import annotations

import pytest

from plexis.agent.dialog import Dialog
from plexis.engine import Engine
from plexis.partners.client import LocalPartners
from plexis.users import CURRENT_USER, DEMO_USER
from tests.conftest import WHEN

DEMO = DEMO_USER

pytestmark = pytest.mark.asyncio
TRIP = f"Plan 2 nights from Seattle to Portland on {WHEN} for 2 under $3,000, private room"


class As:
    """Run a block as a given signed-in user."""

    def __init__(self, user: str) -> None:
        self.user = user

    def __enter__(self) -> None:
        self.token = CURRENT_USER.set(self.user)

    def __exit__(self, *exc: object) -> None:
        CURRENT_USER.reset(self.token)


async def test_memory_is_per_user(engine: Engine) -> None:
    from plexis.companion.profile import interpret

    with As("alice"):
        for cand in interpret("We prefer no AC"):
            engine.profile.remember(cand, "We prefer no AC")
        assert engine.profile.data.facts
    with As("bob"):
        assert not engine.profile.data.facts, "Bob must not see Alice's household"
        res = await engine.compile(TRIP.replace("private room", "shared room"))
        assert all(c.origin != "profile" for c in res.intent.constraints)


async def test_plans_and_conversations_are_private(engine: Engine) -> None:
    with As("alice"):
        res = await engine.compile(TRIP)
        dialog = Dialog(engine)
        turn = await dialog.turn("Book a trip for me")
    with As("bob"):
        with pytest.raises(KeyError):
            engine.get(res.stored.plan_id)
        other = await dialog.turn("the first one", turn.draft_id)
        assert other.draft_id != turn.draft_id, "Bob can't continue Alice's conversation"


async def test_plan_ids_cannot_be_read_off_the_public_receipts(engine: Engine) -> None:
    with As("alice"):
        res = await engine.compile(TRIP)
    receipt = engine.verify_receipt(res.stored.entry.index)  # anyone may verify any receipt
    assert not receipt["entry"]["certHash"].startswith(res.stored.plan_id)


async def test_same_trip_gives_each_customer_their_own_plan(engine: Engine) -> None:
    with As("alice"):
        mine = await engine.compile(TRIP)
    with As("bob"):
        theirs = await engine.compile(TRIP)
    assert mine.stored.plan_id != theirs.stored.plan_id
    with As("alice"):
        assert engine.get(mine.stored.plan_id).owner == "alice", "Bob's identical plan must not take Alice's"


async def test_one_guest_cannot_teach_plexis_for_the_next(engine: Engine) -> None:
    from plexis.users import GUEST, start_request

    start_request(GUEST)
    engine.learner.teach("When I say beachy I mean near the beach")
    engine.learner.note_plan()
    assert engine.learner.data["habits"]["plans"] == 1, "learning works within the request"
    start_request(GUEST)  # the next unlinked request
    assert not engine.learner.data["lexicon"] and engine.learner.data["habits"]["plans"] == 0
    start_request(DEMO)


async def test_a_failed_replan_does_not_leave_the_monitor_acting_as_that_customer(settings) -> None:  # type: ignore[no-untyped-def]
    first = Engine(settings, LocalPartners())
    with As("alice"):
        res = await first.compile(TRIP)
        await first.book(res.stored.plan_id)
    partners = LocalPartners()
    partners.cancelled.add(res.stored.plan.outbound.id)
    restarted = Engine(settings, partners)

    async def broken(*_: object) -> dict[str, object]:
        raise RuntimeError("partner down mid re-plan")

    restarted._replan_after_cancellation = broken  # type: ignore[method-assign]
    before = CURRENT_USER.get()
    assert await restarted.check_disruptions() == []
    assert CURRENT_USER.get() == before


async def test_plans_survive_a_restart(settings) -> None:  # type: ignore[no-untyped-def]
    first = Engine(settings, LocalPartners())
    res = await first.compile(TRIP)
    restarted = Engine(settings, LocalPartners())  # same disk, empty memory
    sp = restarted.get(res.stored.plan_id)
    assert sp.plan is not None and sp.verdict.ok and sp.plan.total == res.stored.plan.total
    booked = await restarted.book(res.stored.plan_id)
    assert booked["status"] == "booked"


async def test_booked_trips_keep_being_watched_after_a_restart(settings) -> None:  # type: ignore[no-untyped-def]
    first = Engine(settings, LocalPartners())
    res = await first.compile(TRIP)
    booked = await first.book(res.stored.plan_id)
    partners = LocalPartners()
    partners.cancelled.add(res.stored.plan.outbound.id)
    restarted = Engine(settings, partners)
    assert booked["tripId"] in restarted.trips
    alerts = await restarted.check_disruptions()
    assert alerts and alerts[0]["tripId"] == booked["tripId"]
    assert Engine(settings, LocalPartners()).trips[booked["tripId"]].alerts, "the alert itself is saved"
