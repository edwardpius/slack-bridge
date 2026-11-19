"""Linked accounts: Slack user -> Cognito refresh token, in OpenBao (GUARD-03, REQ-02).

Path: `slack-links/data/<team ID>/<user ID>` (openbao-platform D-28). The bridge logs in with
its ServiceAccount token (Kubernetes auth, role `slack-bridge`). Errors never include tokens
or response bodies.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

_SLACK_ID = re.compile(r"^[A-Z0-9]{2,32}$")


class LinkStoreError(Exception):
    pass


@dataclass(frozen=True)
class Link:
    refresh_token: str = field(repr=False)
    sub: str = ""
    email: str = ""
    linked_at: int = 0


def _key(team: str, user: str) -> str:
    # Slack IDs are upper-case alphanumerics; anything else could escape the path.
    if not (_SLACK_ID.match(team or "") and _SLACK_ID.match(user or "")):
        raise LinkStoreError("invalid Slack team or user ID")
    return f"{team}/{user}"


class LinkStore(Protocol):
    async def get(self, team: str, user: str) -> Link | None: ...
    async def put(self, team: str, user: str, link: Link) -> None: ...
    async def delete(self, team: str, user: str) -> None: ...


class MemoryLinkStore:
    """For tests and local runs without OpenBao."""

    def __init__(self) -> None:
        self.items: dict[str, Link] = {}

    async def get(self, team: str, user: str) -> Link | None:
        return self.items.get(_key(team, user))

    async def put(self, team: str, user: str, link: Link) -> None:
        self.items[_key(team, user)] = link

    async def delete(self, team: str, user: str) -> None:
        self.items.pop(_key(team, user), None)


class OpenBaoLinkStore:
    def __init__(self, addr: str, role: str, mount: str, sa_token_path: str,
                 verify: str | bool = True, transport: httpx.AsyncBaseTransport | None = None):
        self._mount = mount.strip("/")
        self._role = role
        self._sa_token_path = Path(sa_token_path)
        self._http = httpx.AsyncClient(base_url=addr.rstrip("/"), timeout=10.0, verify=verify,
                                       transport=transport)
        self._token: str | None = None
        self._expires = 0.0

    async def _login(self) -> str:
        if self._token and time.monotonic() < self._expires - 60:
            return self._token
        jwt = self._sa_token_path.read_text().strip()      # projected token rotates: re-read
        try:
            r = await self._http.post("/v1/auth/kubernetes/login",
                                      json={"role": self._role, "jwt": jwt})
        except httpx.HTTPError as e:
            raise LinkStoreError(f"OpenBao unreachable ({type(e).__name__})") from None
        if r.status_code != 200:
            raise LinkStoreError(f"OpenBao login refused ({r.status_code})")
        auth = r.json()["auth"]
        self._token = auth["client_token"]
        self._expires = time.monotonic() + float(auth.get("lease_duration") or 300)
        return self._token

    async def _call(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._login()
            try:
                r = await self._http.request(method, f"/v1/{self._mount}/{path}",
                                             headers={"X-Vault-Token": token}, **kw)
            except httpx.HTTPError as e:
                raise LinkStoreError(f"OpenBao unreachable ({type(e).__name__})") from None
            if r.status_code == 403 and attempt == 1:       # token expired early: log in again
                self._token = None
                continue
            return r
        return r

    async def get(self, team: str, user: str) -> Link | None:
        r = await self._call("GET", f"data/{_key(team, user)}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise LinkStoreError(f"OpenBao read {r.status_code}")
        d = (r.json().get("data") or {}).get("data") or {}
        if not d.get("refresh_token"):
            return None
        return Link(d["refresh_token"], d.get("sub", ""), d.get("email", ""),
                    int(d.get("linked_at") or 0))

    async def put(self, team: str, user: str, link: Link) -> None:
        r = await self._call("POST", f"data/{_key(team, user)}", json={"data": {
            "refresh_token": link.refresh_token, "sub": link.sub, "email": link.email,
            "linked_at": link.linked_at}})
        if r.status_code not in (200, 204):
            raise LinkStoreError(f"OpenBao write {r.status_code}")

    async def delete(self, team: str, user: str) -> None:
        # Metadata delete destroys every version (the mount keeps one anyway).
        r = await self._call("DELETE", f"metadata/{_key(team, user)}")
        if r.status_code not in (200, 204, 404):
            raise LinkStoreError(f"OpenBao delete {r.status_code}")

    async def close(self) -> None:
        await self._http.aclose()
