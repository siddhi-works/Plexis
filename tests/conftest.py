from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn

from qed.config import Settings
from qed.domain import world
from qed.engine import Engine
from qed.partners.client import LocalPartners

# A date safely in the future whenever the suite runs, phrased the way people speak.
TRIP_DAY = world.today() + timedelta(days=30)
WHEN = f"{TRIP_DAY.strftime('%B')} {TRIP_DAY.day}"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    os.environ["QED_STATE_DIR"] = str(tmp_path)
    s = Settings()
    return replace(s, state_dir=tmp_path, solver_workers=4, backup_every=0)


@pytest.fixture
def engine(settings: Settings) -> Engine:
    return Engine(settings, LocalPartners(), seed_demo_household=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Running:
    def __init__(self, app: object, port: int) -> None:
        self.port = port
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Running:
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="session")
def live_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """Partners + QED over real HTTP, as `qed dev` runs them."""
    from qed.partners.servers import build_app
    from qed.server.app import create_app

    state = tmp_path_factory.mktemp("live")
    pport, qport = free_port(), free_port()
    s = replace(
        Settings(),
        state_dir=state,
        port=qport,
        partners_port=pport,
        partners_url=f"http://127.0.0.1:{pport}",
        rate_limit_per_minute=600,
        backup_every=0,
    )
    with Running(build_app(), pport), Running(create_app(s), qport):
        yield {"base": f"http://127.0.0.1:{qport}", "partners": f"http://127.0.0.1:{pport}", "settings": s}
