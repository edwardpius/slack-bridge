"""The bridge's Cognito client (openbao-platform D-28): sign-in link, code exchange, refresh,
revocation. Errors never carry tokens or response bodies.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx


class CognitoError(Exception):
    """Cognito unreachable or answered unexpectedly: transient, keep the link."""


class LinkExpired(Exception):
    """The refresh token is no longer valid (expired, revoked, user removed): REQ-06."""


@dataclass(frozen=True)
class Tokens:
    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    sub: str = ""
    email: str = ""


def jwt_claims(token: str) -> dict:
    """Payload of a token received directly from Cognito's token endpoint over TLS.

    Not a verification: the controller's oauth2-proxy verifies every token the bridge sends
    (GUARD-01). Used only to record which hub user a Slack user linked to.
    """
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


class Cognito:
    def __init__(self, domain: str, client_id: str, client_secret: str, redirect_uri: str,
                 idp: str = "IdentityCenter", transport: httpx.AsyncBaseTransport | None = None):
        self._base = f"https://{domain}"
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._idp = idp
        self._http = httpx.AsyncClient(base_url=self._base, timeout=10.0, transport=transport,
                                       auth=httpx.BasicAuth(client_id, client_secret))

    def authorize_url(self, state: str, code_challenge: str) -> str:
        q = {"response_type": "code", "client_id": self._client_id,
             "redirect_uri": self._redirect_uri, "scope": "openid email profile",
             "state": state, "code_challenge": code_challenge,
             "code_challenge_method": "S256"}
        if self._idp:
            q["identity_provider"] = self._idp
        return f"{self._base}/oauth2/authorize?{urlencode(q)}"

    async def _token(self, data: dict) -> dict:
        try:
            r = await self._http.post("/oauth2/token", data=data)
        except httpx.HTTPError as e:
            raise CognitoError(f"token endpoint unreachable ({type(e).__name__})") from None
        if r.status_code == 200:
            return r.json()
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""
        if r.status_code == 400 and err in ("invalid_grant", "invalid_request"):
            raise LinkExpired(err)
        raise CognitoError(f"token endpoint {r.status_code} {err}".strip())

    async def exchange(self, code: str, verifier: str) -> Tokens:
        body = await self._token({"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": self._redirect_uri,
                                  "client_id": self._client_id, "code_verifier": verifier})
        if not body.get("id_token") or not body.get("refresh_token"):
            raise CognitoError("token endpoint returned no id_token or refresh_token")
        claims = jwt_claims(body["id_token"])
        return Tokens(body["id_token"], body["refresh_token"],
                      sub=str(claims.get("sub", "")), email=str(claims.get("email", "")))

    async def refresh(self, refresh_token: str) -> str:
        """A fresh ID token for the linked user (REQ-09)."""
        body = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token,
                                  "client_id": self._client_id})
        if not body.get("id_token"):
            raise CognitoError("refresh returned no id_token")
        return body["id_token"]

    async def revoke(self, refresh_token: str) -> None:
        """REQ-05. Best effort: the link is deleted whether or not this succeeds."""
        try:
            r = await self._http.post("/oauth2/revoke", data={"token": refresh_token,
                                                              "client_id": self._client_id})
        except httpx.HTTPError as e:
            raise CognitoError(f"revoke unreachable ({type(e).__name__})") from None
        if r.status_code != 200:
            raise CognitoError(f"revoke {r.status_code}")

    async def close(self) -> None:
        await self._http.aclose()
