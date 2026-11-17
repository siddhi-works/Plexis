"""Per-user state: each signed-in customer gets their own household memory and learning.

The signed-in user for the current request lives in a context variable, set by
the MCP server from the access token's subject. Engine and dialog code read
`engine.profile` / `engine.learner`, which resolve to that user's stores, so
one customer's "Grandma uses a wheelchair" never shapes another's trip.
"""

from __future__ import annotations

import hashlib
import threading
from contextvars import ContextVar
from pathlib import Path

from qed.agent.learning import Learner
from qed.companion.profile import ProfileStore

DEMO_USER = "household-demo"  # the development sign-in's subject; seeded with the demo household
GUEST = "guest"  # unlinked requests: no memory is read or written

CURRENT_USER: ContextVar[str] = ContextVar("plexis_user", default=DEMO_USER)
# A guest's learning lives for one request only, so one guest can't teach Plexis
# something that changes how it understands the next guest.
_GUEST_LEARNER: ContextVar[Learner | None] = ContextVar("plexis_guest_learner", default=None)


def start_request(user: str) -> None:
    """Act for `user` for the rest of this request."""
    CURRENT_USER.set(user)
    _GUEST_LEARNER.set(None)


def user_dir(state: Path, user: str) -> Path:
    return state / "users" / hashlib.sha256(user.encode()).hexdigest()[:16]


class Users:
    def __init__(self, state: Path, *, seed_demo: bool) -> None:
        self.state = state
        self.seed_demo = seed_demo
        self._lock = threading.Lock()
        self._profiles: dict[str, ProfileStore] = {}
        self._learners: dict[str, Learner] = {}

    def profile(self, user: str | None = None) -> ProfileStore:
        user = user or CURRENT_USER.get()
        with self._lock:
            if user not in self._profiles:
                self._profiles[user] = ProfileStore(
                    user_dir(self.state, user), seed_demo=self.seed_demo and user == DEMO_USER
                )
            return self._profiles[user]

    def learner(self, user: str | None = None) -> Learner:
        user = user or CURRENT_USER.get()
        if user == GUEST:
            guest = _GUEST_LEARNER.get()
            if guest is None:
                guest = Learner(None)
                _GUEST_LEARNER.set(guest)
            return guest
        with self._lock:
            if user not in self._learners:
                d = user_dir(self.state, user)
                d.mkdir(parents=True, exist_ok=True)
                self._learners[user] = Learner(d)
            return self._learners[user]
