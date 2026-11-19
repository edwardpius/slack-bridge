"""research-agent over A2A JSON-RPC, as the linked user (REQ-09 to REQ-11, REQ-14).

One `run` is one A2A message with the user's own ID token. Progress comes as working-state
status updates; the report is the completed task's status message.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

import httpx
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, VERSION_HEADER
from a2a.client.errors import A2AClientError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2 as pb

TERMINAL = {pb.TASK_STATE_COMPLETED: "completed", pb.TASK_STATE_FAILED: "failed",
            pb.TASK_STATE_CANCELED: "canceled", pb.TASK_STATE_REJECTED: "rejected",
            pb.TASK_STATE_INPUT_REQUIRED: "input_required",
            pb.TASK_STATE_AUTH_REQUIRED: "auth_required"}


class AgentError(Exception):
    """The call failed (network, HTTP error, protocol error). The message is safe to log."""


class AgentAuthError(AgentError):
    """401/403: the token was refused on the way in."""


class RunTimeout(AgentError):
    """REQ-14: the run passed its wall-clock cap and was cancelled."""


@dataclass
class RunResult:
    state: str                  # completed | failed | canceled | rejected | ...
    text: str
    context_id: str
    task_id: str


def _text(message: pb.Message) -> str:
    return "".join(p.text for p in message.parts if p.HasField("text")) if message else ""


class AgentClient:
    def __init__(self, url: str, run_timeout_s: float = 600.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url
        self.run_timeout_s = run_timeout_s
        self._transport = transport

    def _http(self, id_token: str, extra_headers: Mapping[str, str] | None) -> httpx.AsyncClient:
        # The SDK's ClientFactory sets A2A-Version; building the transport directly doesn't,
        # and kagent then assumes 0.3 and refuses the call (found live, 2026-11-19).
        headers = {"Authorization": f"Bearer {id_token}",
                   VERSION_HEADER: PROTOCOL_VERSION_CURRENT, **(extra_headers or {})}
        timeout = httpx.Timeout(connect=10.0, read=self.run_timeout_s + 30, write=30.0, pool=10.0)
        return httpx.AsyncClient(headers=headers, timeout=timeout, transport=self._transport)

    async def run(self, question: str, id_token: str, context_id: str | None,
                  on_progress: Callable[[str], Awaitable[None]],
                  extra_headers: Mapping[str, str] | None = None) -> RunResult:
        msg = pb.Message(message_id=uuid.uuid4().hex, role=pb.ROLE_USER,
                         parts=[pb.Part(text=question)], context_id=context_id or "")
        request = pb.SendMessageRequest(message=msg)
        task_id, ctx = "", context_id or ""
        async with self._http(id_token, extra_headers) as http:
            rpc = JsonRpcTransport(http, pb.AgentCard(), self.url)
            try:
                async with asyncio.timeout(self.run_timeout_s):
                    async for ev in rpc.send_message_streaming(request):
                        kind = ev.WhichOneof("payload")
                        if kind == "task":
                            task_id, ctx = ev.task.id or task_id, ev.task.context_id or ctx
                            status = ev.task.status
                        elif kind == "status_update":
                            task_id = ev.status_update.task_id or task_id
                            ctx = ev.status_update.context_id or ctx
                            status = ev.status_update.status
                        elif kind == "message":
                            return RunResult("completed", _text(ev.message),
                                             ev.message.context_id or ctx, task_id)
                        else:
                            continue
                        if status.state in TERMINAL:
                            return RunResult(TERMINAL[status.state], _text(status.message),
                                             ctx, task_id)
                        if status.state == pb.TASK_STATE_WORKING:
                            text = _text(status.message)
                            if text:
                                await on_progress(text)
            except TimeoutError:
                if task_id:
                    try:
                        await rpc.cancel_task(pb.CancelTaskRequest(id=task_id))
                    except (A2AClientError, httpx.HTTPError):
                        pass
                raise RunTimeout(f"run passed {self.run_timeout_s:.0f} s") from None
            except httpx.HTTPStatusError as e:
                raise _http_error(e.response.status_code) from None
            except A2AClientError as e:
                code = _status_of(e)
                if code is not None:
                    raise _http_error(code) from None
                raise AgentError(f"A2A error ({type(e).__name__})") from None
            except httpx.HTTPError as e:
                raise AgentError(f"A2A unreachable ({type(e).__name__})") from None
        raise AgentError("stream ended before the task finished")


def _status_of(e: Exception) -> int | None:
    cause = e.__cause__ or e.__context__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code
    return None


def _http_error(code: int) -> AgentError:
    if code in (401, 403):
        return AgentAuthError(f"A2A refused the token ({code})")
    return AgentError(f"A2A HTTP {code}")
