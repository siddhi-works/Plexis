from __future__ import annotations

from datetime import date, timedelta

from qed.domain import world
from qed.ir import ActivitiesPerDay, TripIntent
from qed.solver.catalog import Catalog


def catalog_for(intent: TripIntent) -> Catalog:
    assert intent.origin and intent.destination and intent.start_date and intent.nights
    end = intent.start_date + timedelta(days=intent.nights)
    return Catalog(
        outbound=world.search_transport(intent.origin, intent.destination, intent.start_date),
        inbound=world.search_transport(intent.destination, intent.origin, end),
        stays=world.quote_stays(intent.destination, intent.start_date, intent.nights, intent.travelers),
        restaurants=list(world.restaurants_for(intent.destination)),
        attractions=list(world.attractions_for(intent.destination)),
        weather=world.forecast(intent.destination, intent.start_date, intent.nights + 1),
    )


def make_intent(
    origin: str,
    dest: str,
    start: date,
    nights: int,
    travelers: int,
    constraints: list,
    objective: str = "balanced",
) -> TripIntent:  # type: ignore[type-arg]
    it = TripIntent(
        utterance="test",
        origin=origin,
        destination=dest,
        start_date=start,
        nights=nights,
        travelers=travelers,
        objective=objective,
        constraints=list(constraints),
    )  # type: ignore[arg-type]
    if not any(c.kind == "activities_per_day" for c in it.constraints):
        it.constraints.append(ActivitiesPerDay(origin="default", min=1, max=2))
    return it.renumber()
