"""Durable plans and trips (SQLite), so a restart loses nothing.

A plan is stored with the exact partner data it was solved against, so it can
be rebuilt and re-verified after a restart. Booked trips are stored too, which
keeps cancellation monitoring running across restarts.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import orjson

from qed.ir import FixIt, TripIntent
from qed.solver.catalog import Catalog
from qed.solver.plan import Plan


class Store:
    def __init__(self, state: Path) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(state / "state.sqlite", check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, owner TEXT NOT NULL, body BLOB NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS trips (trip_id TEXT PRIMARY KEY, owner TEXT NOT NULL, body BLOB NOT NULL)"
        )

    # ------------------------------------------------------------------ plans

    def save_plan(self, sp: Any) -> None:
        cat = sp.catalog
        body = {
            "plan_id": sp.plan_id,
            "owner": sp.owner,
            "created": sp.created,
            "intent": sp.intent.model_dump(mode="json"),
            "catalog": {
                **cat.snapshot(),
                "sources": cat.sources,
                "degraded": cat.degraded,
                "affinity": cat.affinity,
                "highlights": cat.highlights,
            },
            "plan": sp.plan.model_dump(mode="json") if sp.plan is not None else None,
            "certificate": sp.certificate,
            "entry_index": sp.entry.index if sp.entry is not None else None,
            "conflict": None
            if sp.conflict is None
            else {
                "mus": sp.conflict.mus,
                "texts": sp.conflict.texts,
                "origins": sp.conflict.origins,
                "fixits": [f.model_dump(mode="json") for f in sp.conflict.fixits],
                "solve_calls": sp.conflict.solve_calls,
            },
        }
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO plans VALUES (?,?,?)", (sp.plan_id, sp.owner, orjson.dumps(body))
            )

    def load_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT body FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        body = orjson.loads(row[0])
        c = body["catalog"]
        catalog = Catalog.from_snapshot(c)
        catalog.sources, catalog.degraded = c.get("sources", {}), c.get("degraded", [])
        catalog.affinity, catalog.highlights = c.get("affinity", {}), c.get("highlights", {})
        body["intent"] = TripIntent.model_validate(body["intent"])
        body["catalog"] = catalog
        body["plan"] = Plan.model_validate(body["plan"]) if body["plan"] else None
        if body["conflict"]:
            body["conflict"]["fixits"] = [FixIt.model_validate(f) for f in body["conflict"]["fixits"]]
        return body

    # ------------------------------------------------------------------ trips

    def save_trip(self, trip: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO trips VALUES (?,?,?)",
                (trip.trip_id, trip.owner, orjson.dumps(asdict(trip))),
            )

    def load_trips(self) -> list[dict[str, Any]]:
        return [orjson.loads(r[0]) for r in self._db.execute("SELECT body FROM trips")]
