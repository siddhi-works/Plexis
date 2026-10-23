"""Trip structure: which days exist, which meals and activity slots must be filled.

This is the *specification* of a well-formed itinerary. The CP-SAT model, the
Z3 cross-check and the independent verifier all read it, so the three agree on
what "a complete trip" means while implementing the checks separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

ARRIVAL_DINNER_CUTOFF = 19 * 60 + 30  # must land by 7:30 pm to plan day-one dinner
DEPARTURE_BREAKFAST_CUTOFF = 10 * 60  # must leave after 10 am to plan last-day breakfast
ARRIVAL_AFTERNOON_CUTOFF = 14 * 60  # must land by 2 pm to plan a day-one activity
FULL_DAY_MINUTES = 8 * 60
AFTERNOON_MINUTES = 4 * 60


@dataclass(frozen=True)
class MealSlot:
    key: str  # "d2-lunch"
    day: int  # 1-based trip day
    date: date
    meal: str
    conditional: str | None = None  # "arrival" | "departure" | None


@dataclass(frozen=True)
class ActivityDay:
    day: int
    date: date
    full: bool  # False = arrival-afternoon slot
    minutes: int


@dataclass(frozen=True)
class Schedule:
    start: date
    nights: int
    meals: tuple[MealSlot, ...]
    activity_days: tuple[ActivityDay, ...]

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.nights)

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.nights + 1)]


def build_schedule(start: date, nights: int) -> Schedule:
    meals: list[MealSlot] = []
    days = nights + 1
    for d in range(1, days + 1):
        on = start + timedelta(days=d - 1)
        if d == 1:
            meals.append(MealSlot(f"d{d}-dinner", d, on, "dinner", conditional="arrival"))
        elif d == days:
            meals.append(MealSlot(f"d{d}-breakfast", d, on, "breakfast", conditional="departure"))
        else:
            for meal in ("breakfast", "lunch", "dinner"):
                meals.append(MealSlot(f"d{d}-{meal}", d, on, meal))
    activity_days = [ActivityDay(1, start, full=False, minutes=AFTERNOON_MINUTES)]
    activity_days += [
        ActivityDay(d, start + timedelta(days=d - 1), full=True, minutes=FULL_DAY_MINUTES)
        for d in range(2, days)
    ]
    return Schedule(start=start, nights=nights, meals=tuple(meals), activity_days=tuple(activity_days))
