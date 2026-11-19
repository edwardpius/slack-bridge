"""Pending account links: one-time `state` values with their PKCE verifier (REQ-02, REQ-03).

A `state` is bound to the Slack team and user it was shown to, lives 10 minutes, and can be
used once. Kept in memory: a restart only means asking to link again.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Pending:
    team: str
    user: str
    verifier: str = field(repr=False)
    created: float


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class PendingLinks:
    def __init__(self, ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic,
                 max_pending: int = 1000) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._max = max_pending
        self._items: dict[str, Pending] = {}

    def create(self, team: str, user: str) -> tuple[str, str]:
        """Returns (state, code_challenge). Any earlier pending link for the user is dropped."""
        self._sweep()
        for k in [k for k, p in self._items.items() if (p.team, p.user) == (team, user)]:
            del self._items[k]
        if len(self._items) >= self._max:
            oldest = min(self._items, key=lambda k: self._items[k].created)
            del self._items[oldest]
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self._items[state] = Pending(team, user, verifier, self._clock())
        return state, challenge_for(verifier)

    def consume(self, state: str | None) -> Pending | None:
        """The pending link for `state`, once; None if unknown, used or expired (REQ-03)."""
        if not state:
            return None
        p = self._items.pop(state, None)
        if p is None or self._clock() - p.created > self._ttl:
            return None
        return p

    def _sweep(self) -> None:
        now = self._clock()
        for k in [k for k, p in self._items.items() if now - p.created > self._ttl]:
            del self._items[k]
