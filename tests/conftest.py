"""Fakes: Slack, Cognito, the A2A endpoint. No test touches the network."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from urllib.parse import parse_qs

import httpx
import pytest
from a2a.types import a2a_pb2 as pb
from google.protobuf import json_format

from slack_bridge.agent import AgentClient
from slack_bridge.bridge import Bridge
from slack_bridge.cognito import Cognito
from slack_bridge.config import Settings
from slack_bridge.links import MemoryLinkStore
from slack_bridge.pending import PendingLinks, challenge_for

TEAM, USER, OTHER, BOT = "T0TEAM", "U0ALICE", "U0BOB", "U0BOT"
CHANNEL, DM = "C0RESEARCH", "D0ALICE"

SECRET_MARKERS = ("refresh-", "idtok.", "xoxb-", "xapp-", "client-secret", "code-")


def make_settings(**kw) -> Settings:
    base = dict(slack_bot_token="xoxb-test", slack_app_token="xapp-test",
                cognito_client_id="bridgeclient", cognito_client_secret="client-secret-x",
                cognito_domain="auth.example.com", public_base_url="https://kagent.example.com",
                openbao_addr="https://openbao.example", allowed_channels=frozenset({CHANNEL}),
                progress_interval_s=0.0, run_timeout_s=5.0)
    base.update(kw)
    return Settings(**base)


def fake_jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._n = 0

    async def post_ephemeral(self, channel, user, text, thread_ts=None):
        self.calls.append(("ephemeral", channel, user, text, thread_ts))

    async def post(self, channel, text, thread_ts=None, blocks=None):
        self._n += 1
        self.calls.append(("post", channel, text, thread_ts, blocks))
        return f"1700000000.{self._n:06d}"

    async def update(self, channel, ts, text, blocks=None):
        self.calls.append(("update", channel, ts, text))

    async def upload(self, channel, thread_ts, filename, content, title):
        self.calls.append(("upload", channel, thread_ts, filename, content))

    async def dm(self, user, text):
        self.calls.append(("dm", user, text))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def all_text(self) -> str:
        return json.dumps(self.calls, default=str)


class FakeCognito:
    """Token, revoke endpoints. Codes map to users; refresh tokens can be revoked."""

    def __init__(self) -> None:
        self.codes: dict[str, tuple[str, str]] = {}       # code -> (sub, email)
        self.challenges: dict[str, str] = {}               # code -> expected PKCE challenge
        self.refresh_valid: dict[str, str] = {}            # refresh token -> sub
        self.revoked: list[str] = []
        self.requests: list[dict] = []
        self.down = False

    def add_code(self, code, sub, email, challenge):
        self.codes[code] = (sub, email)
        self.challenges[code] = challenge

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append({"path": request.url.path, **form})
        if request.url.path == "/oauth2/revoke":
            self.refresh_valid.pop(form.get("token", ""), None)
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200)
        gt = form.get("grant_type")
        if gt == "authorization_code":
            code = form.get("code", "")
            if code not in self.codes or challenge_for(form.get("code_verifier", "")) != self.challenges[code]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            sub, email = self.codes.pop(code)
            rt = f"refresh-{sub}"
            self.refresh_valid[rt] = sub
            return httpx.Response(200, json={"id_token": fake_jwt({"sub": sub, "email": email}),
                                             "refresh_token": rt, "access_token": "a"})
        if gt == "refresh_token":
            sub = self.refresh_valid.get(form.get("refresh_token", ""))
            if sub is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": f"idtok.{sub}", "access_token": "a"})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


def sse(events: list[pb.StreamResponse]) -> bytes:
    out = []
    for ev in events:
        body = {"jsonrpc": "2.0", "id": "1", "result": json_format.MessageToDict(ev)}
        out.append(f"data: {json.dumps(body)}\n\n")
    return "".join(out).encode()


def status(state, text="", task_id="task-1", ctx="ctx-1") -> pb.StreamResponse:
    msg = pb.Message(message_id="m", role=pb.ROLE_AGENT, parts=[pb.Part(text=text)]) if text else None
    st = pb.TaskStatus(state=state, message=msg) if msg else pb.TaskStatus(state=state)
    return pb.StreamResponse(status_update=pb.TaskStatusUpdateEvent(
        task_id=task_id, context_id=ctx, status=st))


def task(task_id="task-1", ctx="ctx-1") -> pb.StreamResponse:
    return pb.StreamResponse(task=pb.Task(id=task_id, context_id=ctx,
                                          status=pb.TaskStatus(state=pb.TASK_STATE_SUBMITTED)))


REPORT = """# Lithium supply in 2030

## Summary

Supply grows **fast** but *unevenly*; see [IEA](https://www.iea.org/report) for detail.

- Australia leads
- Chile second

## Findings

Long findings text.

[Download the report](/reports/abc123/run1/report.md)

---
*12 turns · 4 searches · 9 pages read · $0.14 · 87 s*"""


class FakeA2A:
    """The kagent A2A endpoint behind oauth2-proxy: checks the bearer token like the proxy."""

    def __init__(self) -> None:
        self.valid_tokens: set[str] = set()
        self.requests: list[dict] = []
        self.script: list[pb.StreamResponse] = [
            task(), status(pb.TASK_STATE_WORKING, "Researching your question…"),
            status(pb.TASK_STATE_WORKING, "Searching the web: lithium supply 2030"),
            status(pb.TASK_STATE_COMPLETED, REPORT)]
        self.hang = False
        self.cancelled: list[str] = []
        self.status_code = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        auth = request.headers.get("authorization", "")
        self.requests.append({"method": body["method"], "auth": auth, "params": body["params"],
                              "headers": dict(request.headers)})
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, text="unauthorized")
        if self.status_code != 200:
            return httpx.Response(self.status_code, text="nope")
        if body["method"] == "CancelTask":
            self.cancelled.append(body["params"]["id"])
            t = pb.Task(id=body["params"]["id"], status=pb.TaskStatus(state=pb.TASK_STATE_CANCELED))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": json_format.MessageToDict(t)})
        if self.hang:
            async def slow():
                yield sse([task()])
                await asyncio.sleep(3600)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=slow())
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=sse(self.script))


class World:
    def __init__(self, **settings_kw) -> None:
        self.settings = make_settings(**settings_kw)
        self.slack = FakeSlack()
        self.links = MemoryLinkStore()
        self.cognito_fake = FakeCognito()
        self.a2a = FakeA2A()
        self.now = [1000.0]
        self.day = [dt.date(2026, 11, 19)]
        self.cognito = Cognito(self.settings.cognito_domain, self.settings.cognito_client_id,
                               self.settings.cognito_client_secret, self.settings.redirect_uri,
                               transport=httpx.MockTransport(self.cognito_fake.handler))
        self.agent = AgentClient(self.settings.agent_url, self.settings.run_timeout_s,
                                 transport=httpx.MockTransport(self.a2a.handler))
        self.pending = PendingLinks(600, clock=lambda: self.now[0])
        self.bridge = Bridge(self.settings, self.slack, self.links, self.cognito, self.agent,
                             self.pending, bot_user_id=BOT, clock=lambda: self.now[0],
                             today=lambda: self.day[0])

    async def link(self, user=USER, sub="sub-alice", email="alice@example.com"):
        """Run the whole linking flow for `user`; returns the callback result."""
        state, challenge = self.pending.create(TEAM, user)
        code = f"code-{user}"
        self.cognito_fake.add_code(code, sub, email, challenge)
        self.a2a.valid_tokens.add(f"idtok.{sub}")
        return await self.bridge.complete_link(state, code)

    async def say(self, text, user=USER, channel=CHANNEL, kind="mention", ts=None,
                  thread_ts=None, **extra):
        self.now[0] += 1
        ev = {"type": "app_mention" if kind == "mention" else "message", "user": user,
              "channel": channel, "text": f"<@{BOT}> {text}" if kind == "mention" else text,
              "ts": ts or f"{self.now[0]:.6f}", **extra}
        if kind == "dm":
            ev["channel_type"] = "im"
        if thread_ts:
            ev["thread_ts"] = thread_ts
        await self.bridge.handle(ev, kind, TEAM)
        await self.bridge.drain()
        return ev


@pytest.fixture
def world() -> World:
    return World()
