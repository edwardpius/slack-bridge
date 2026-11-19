"""The bridge's logic, independent of Slack's SDK (H-006 REQ-01 to REQ-17).

Slack events come in through `handle`, which returns at once (Slack wants an ack within
3 seconds, REQ-08) and runs research in a background task. Everything Slack-specific goes
through `SlackPort`, so tests use a fake.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Awaitable, Callable, Protocol

from . import otel, slackfmt
from .agent import AgentAuthError, AgentClient, AgentError, RunTimeout
from .cognito import Cognito, CognitoError, LinkExpired
from .config import Settings
from .links import Link, LinkStore, LinkStoreError
from .pending import PendingLinks

log = logging.getLogger("slack_bridge")

IGNORED_SUBTYPES = {"bot_message", "message_changed", "message_deleted", "message_replied",
                    "channel_join", "channel_leave", "thread_broadcast"}
_MENTION = re.compile(r"<@[A-Z0-9]+>")

HELP = ("Ask me a research question and I'll reply in this thread with a cited report. "
        "Reply in the thread to follow up. Send `disconnect` to unlink your hub account.")


class SlackPort(Protocol):
    async def post_ephemeral(self, channel: str, user: str, text: str,
                             thread_ts: str | None = None) -> None: ...
    async def post(self, channel: str, text: str, thread_ts: str | None = None,
                   blocks: list[dict] | None = None) -> str: ...
    async def update(self, channel: str, ts: str, text: str,
                     blocks: list[dict] | None = None) -> None: ...
    async def upload(self, channel: str, thread_ts: str, filename: str, content: str,
                     title: str) -> None: ...
    async def dm(self, user: str, text: str) -> None: ...


def error_text(ref: str) -> str:
    """REQ-16: fixed wording; details only in the log and trace under `ref`."""
    return f"Something went wrong with this request. Reference: `{ref}`."


class Bridge:
    def __init__(self, settings: Settings, slack: SlackPort, links: LinkStore,
                 cognito: Cognito, agent: AgentClient, pending: PendingLinks | None = None,
                 bot_user_id: str = "", clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], dt.date] = dt.date.today) -> None:
        self.s = settings
        self.slack = slack
        self.links = links
        self.cognito = cognito
        self.agent = agent
        self.pending = pending or PendingLinks(settings.state_ttl_s)
        self.bot_user_id = bot_user_id
        self._clock = clock
        self._today = today
        self._running: set[tuple[str, str]] = set()                 # REQ-12
        self._runs_today: dict[tuple[str, str, dt.date], int] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()          # FM-04
        self._threads: dict[tuple[str, str], str] = {}              # REQ-10 (memory only)
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ events
    def accepts(self, event: dict, kind: str) -> bool:
        """REQ-13: who and where the bridge answers."""
        if event.get("bot_id") or event.get("subtype") in IGNORED_SUBTYPES:
            return False
        if event.get("subtype") and kind == "dm":
            return False
        user = event.get("user")
        if not user or user == self.bot_user_id:
            return False
        if kind == "dm":
            return event.get("channel_type") == "im"
        if kind == "mention":
            return event.get("channel") in self.s.allowed_channels
        return False

    def _first_time(self, event: dict) -> bool:
        key = f"{event.get('channel')}:{event.get('ts')}"
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    async def handle(self, event: dict, kind: str, team: str) -> None:
        """Entry point for `app_mention` (kind="mention") and DM `message` (kind="dm")."""
        if not self.accepts(event, kind):
            # A real mention the bridge won't answer (channel not allowed, a bot, an edit):
            # say so, or a wrong ALLOWED_CHANNELS looks like Slack sending nothing.
            if kind == "mention":
                log.info("event kind=mention ignored channel=%s", event.get("channel"))
            return
        if not self._first_time(event):
            return
        task = asyncio.create_task(self._handle(event, kind, team))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Tests and shutdown: wait for background work."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _handle(self, event: dict, kind: str, team: str) -> None:
        user, channel = event["user"], event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        text = " ".join(_MENTION.sub(" ", event.get("text") or "").split())
        ref = uuid.uuid4().hex[:10]
        with otel.span("slack.event", **{"slack.user": otel.user_hash(team, user),
                                         "slack.channel": channel, "slack.thread_ts": thread_ts,
                                         "slack.kind": kind, "bridge.ref": ref}) as sp:
            try:
                outcome = await self._dispatch(team, user, channel, thread_ts, event, text, ref)
            except Exception:                                   # noqa: BLE001 (REQ-16)
                log.exception("event failed ref=%s", ref)
                outcome = "error"
                try:
                    await self.slack.post(channel, error_text(ref), thread_ts=thread_ts)
                except Exception:                               # noqa: BLE001
                    log.exception("could not report the error ref=%s", ref)
            otel.set_attr(sp, "bridge.outcome", outcome)
        log.info("event kind=%s outcome=%s ref=%s", kind, outcome, ref)

    async def _dispatch(self, team: str, user: str, channel: str, thread_ts: str,
                        event: dict, text: str, ref: str) -> str:
        # Private replies go in a thread only if the user wrote in one: Slack shows no
        # reply count for them, so a private reply under a top-level message goes unseen.
        eph = event.get("thread_ts")
        if text.lower() == "disconnect":
            await self.disconnect(team, user, channel, eph)
            return "disconnect"
        link = await self.links.get(team, user)
        if link is None:                                        # REQ-01
            await self._offer_link(team, user, channel, eph)
            return "link_offered"
        if not text or text.lower() == "help":
            await self.slack.post_ephemeral(channel, user, HELP, thread_ts=eph)
            return "help"
        if (team, user) in self._running:                       # REQ-12
            await self.slack.post_ephemeral(
                channel, user, "You already have a research run in progress. "
                "I'll take the next question when it finishes.", thread_ts=eph)
            return "busy"
        day = (team, user, self._today())
        if self._runs_today.get(day, 0) >= self.s.runs_per_day:
            await self.slack.post_ephemeral(
                channel, user, f"You've reached today's limit of {self.s.runs_per_day} "
                "research runs. You can still use the kagent UI.", thread_ts=eph)
            return "daily_limit"
        self._running.add((team, user))
        self._runs_today[day] = self._runs_today.get(day, 0) + 1
        try:
            return await self._research(team, user, channel, thread_ts, event, text, link, ref)
        finally:
            self._running.discard((team, user))

    # ---------------------------------------------------------------- research
    async def _research(self, team: str, user: str, channel: str, thread_ts: str, event: dict,
                        question: str, link: Link, ref: str) -> str:
        key = (channel, thread_ts)
        context_id = self._threads.get(key)
        note = ""
        if event.get("thread_ts") and context_id is None:
            note = ("\n_I don't have the earlier part of this thread any more, so this starts "
                    "a new research conversation._")
        status_ts = await self.slack.post(channel, f":mag: Researching…{note}",
                                          thread_ts=thread_ts)          # REQ-08
        try:
            id_token = await self.cognito.refresh(link.refresh_token)    # REQ-09
        except LinkExpired:                                              # REQ-06
            await self.links.delete(team, user)
            await self.slack.update(channel, status_ts,
                                    "Your hub link has expired. I've sent you a new sign-in link.")
            await self._offer_link(team, user, channel, event.get("thread_ts"))
            return "link_expired"

        last = [0.0]

        async def progress(text: str) -> None:                           # REQ-11
            now = self._clock()
            if now - last[0] < self.s.progress_interval_s:
                return
            last[0] = now
            line = " ".join(text.split())[:200]
            try:
                await self.slack.update(channel, status_ts, f":mag: {_plain(line)}")
            except Exception:                                            # noqa: BLE001
                log.warning("progress update failed ref=%s", ref)

        try:
            with otel.span("a2a.run") as sp:
                result = await self.agent.run(question, id_token, context_id, progress,
                                              extra_headers=otel.trace_headers())
                otel.set_attr(sp, "a2a.context_id", result.context_id)
                otel.set_attr(sp, "a2a.task_id", result.task_id)
                otel.set_attr(sp, "a2a.state", result.state)
        except RunTimeout:                                               # REQ-14
            await self.slack.update(channel, status_ts,
                                    "The research run took longer than "
                                    f"{self.s.run_timeout_s / 60:.0f} minutes and was stopped.")
            return "timeout"
        except AgentAuthError:
            log.warning("token refused by the hub ref=%s", ref)
            await self.slack.update(channel, status_ts, error_text(ref))
            return "auth_refused"
        except AgentError as e:
            log.warning("agent call failed ref=%s: %s", ref, e)
            await self.slack.update(channel, status_ts, error_text(ref))
            return "agent_error"

        if result.context_id:
            self._threads[key] = result.context_id                       # REQ-10
        if result.state != "completed":
            # research-agent's own refusals are short user-facing text (spec 025).
            msg = _plain(result.text)[:500] if result.state == "rejected" and result.text \
                else error_text(ref)
            log.info("run ended %s ref=%s", result.state, ref)
            await self.slack.update(channel, status_ts, msg)
            return result.state
        await self._post_report(channel, thread_ts, status_ts, result.text, result.task_id)
        return "completed"

    async def _post_report(self, channel: str, thread_ts: str, status_ts: str, md: str,
                           task_id: str) -> None:
        """REQ-15: summary, the full report as a file, the hub download link."""
        md = slackfmt.absolutize(md, self.s.public_base_url)
        head, clipped = slackfmt.summary(md)
        lines = [slackfmt.to_mrkdwn(head)]
        if clipped:
            lines.append("_Full report in the attached file._")
        for url in slackfmt.download_links(md):
            lines.append(f"<{url}|Download from the hub>")
        stats = slackfmt.footer(md)
        if stats:
            lines.append(slackfmt.to_mrkdwn(stats))
        body = "\n\n".join(lines)
        await self.slack.update(channel, status_ts, ":white_check_mark: Research done.")
        await self.slack.post(channel, _plain(head)[:200] or "Research report",
                              thread_ts=thread_ts, blocks=slackfmt.blocks_for(body))
        report = md.rsplit("\n---\n", 1)[0]
        await self.slack.upload(channel, thread_ts, f"report-{task_id[:12] or 'research'}.md",
                                report, "Research report")

    # ------------------------------------------------------------------ links
    async def _offer_link(self, team: str, user: str, channel: str, thread_ts: str | None) -> None:
        state, challenge = self.pending.create(team, user)
        url = self.cognito.authorize_url(state, challenge)
        await self.slack.post_ephemeral(
            channel, user,
            "To ask research-agent from Slack, connect your hub account once: "
            f"<{url}|sign in to the hub>. The link works for 10 minutes and only for you. "
            "Then ask your question again.", thread_ts=thread_ts)

    async def complete_link(self, state: str | None, code: str | None) -> tuple[int, str]:
        """The sign-in callback (REQ-02 to REQ-04). Returns (HTTP status, page text)."""
        pending = self.pending.consume(state)
        if pending is None or not code:                                  # REQ-03
            return 400, "This sign-in link is invalid or has expired. Ask the bot in Slack again."
        try:
            tokens = await self.cognito.exchange(code, pending.verifier)
        except (LinkExpired, CognitoError) as e:
            log.warning("code exchange failed: %s", type(e).__name__)
            return 400, "Sign-in didn't complete. Ask the bot in Slack again."
        link = Link(tokens.refresh_token, tokens.sub, tokens.email, int(time.time()))
        try:
            await self.links.put(pending.team, pending.user, link)
        except LinkStoreError as e:
            log.warning("link store failed: %s", e)
            return 503, "Your account couldn't be linked right now. Try again later."
        who = f" as {tokens.email}" if tokens.email else ""
        try:
            await self.slack.dm(pending.user, f"Your hub account is linked{who}. Ask away. "
                                "Send `disconnect` any time to unlink.")
        except Exception:                                                # noqa: BLE001
            log.warning("link confirmation DM failed")
        return 200, f"Your hub account is linked{who}. You can close this tab and go back to Slack."

    async def disconnect(self, team: str, user: str, channel: str, thread_ts: str | None) -> None:
        """REQ-05."""
        link = await self.links.get(team, user)
        if link is not None:
            try:
                await self.cognito.revoke(link.refresh_token)
            except CognitoError as e:
                log.warning("revoke failed (link deleted anyway): %s", e)
            await self.links.delete(team, user)
        await self.slack.post_ephemeral(
            channel, user, "Your hub account is unlinked from Slack." if link else
            "Your Slack account isn't linked to the hub.", thread_ts=thread_ts)


def _plain(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


Callback = Callable[[str | None, str | None], Awaitable[tuple[int, str]]]
