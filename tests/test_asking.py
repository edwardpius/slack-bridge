"""H-006 REQ-08 to REQ-17: asking research-agent and posting the answer."""
from __future__ import annotations

import logging

from a2a.types import a2a_pb2 as pb
from conftest import (BOT, CHANNEL, DM, OTHER, REPORT, SECRET_MARKERS, TEAM, USER, World,
                      status, task)


async def test_req08_reply_in_thread_then_report(world):
    await world.link()
    ev = await world.say("lithium supply in 2030?")
    first = world.slack.of("post")[0]
    assert first[1] == CHANNEL and first[3] == ev["ts"] and "Researching" in first[2]
    assert world.slack.of("upload")[0][2] == ev["ts"]


async def test_req09_calls_a2a_with_the_users_own_fresh_token(world):
    await world.link()
    await world.say("lithium supply in 2030?")
    req = world.a2a.requests[0]
    assert req["method"] == "SendStreamingMessage"
    assert req["auth"] == "Bearer idtok.sub-alice"
    text = req["params"]["message"]["parts"][0]["text"]
    assert text == "lithium supply in 2030?"                  # mention stripped
    refreshes = [r for r in world.cognito_fake.requests if r.get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1                                # refreshed at the run's start


async def test_req09_two_users_run_as_themselves(world):
    """FM-01: each Slack user's call carries their own token."""
    await world.link(USER, "sub-alice")
    await world.link(OTHER, "sub-bob", "bob@example.com")
    await world.say("q1", user=USER)
    await world.say("q2", user=OTHER)
    assert [r["auth"] for r in world.a2a.requests] == ["Bearer idtok.sub-alice",
                                                        "Bearer idtok.sub-bob"]


async def test_req10_thread_follow_up_continues_the_context(world):
    await world.link()
    ev = await world.say("first question")
    await world.say("follow up", thread_ts=ev["ts"])
    assert "contextId" not in world.a2a.requests[0]["params"]["message"]
    assert world.a2a.requests[1]["params"]["message"]["contextId"] == "ctx-1"


async def test_req10_unknown_thread_after_restart_starts_new_and_says_so(world):
    await world.link()
    await world.say("follow up", thread_ts="1600000000.000001")
    assert "contextId" not in world.a2a.requests[0]["params"]["message"]
    assert "new research conversation" in world.slack.of("post")[0][2]


async def test_req11_progress_updates_are_throttled(world):
    w = World(progress_interval_s=5.0)
    await w.link()
    await w.say("q")
    progress = [c for c in w.slack.of("update") if "Searching" in c[3] or "Researching your" in c[3]]
    assert len(progress) == 1                                 # second came within 5 s


async def test_req11_progress_updates_the_status_message(world):
    await world.link()
    await world.say("q")
    status_ts = "1700000000.000001"
    texts = [c[3] for c in world.slack.of("update") if c[2] == status_ts]
    assert any("Searching the web" in t for t in texts)


async def test_req12_one_run_per_user(world):
    await world.link()
    world.a2a.hang = True
    w = world
    w.settings = w.settings
    e1 = {"user": USER, "channel": CHANNEL, "text": f"<@{BOT}> one", "ts": "1.1"}
    e2 = {"user": USER, "channel": CHANNEL, "text": f"<@{BOT}> two", "ts": "1.2"}
    await w.bridge.handle(e1, "mention", TEAM)
    import asyncio
    await asyncio.sleep(0.05)
    await w.bridge.handle(e2, "mention", TEAM)
    await asyncio.sleep(0.05)
    assert any("already have a research run" in c[3] for c in w.slack.of("ephemeral"))
    assert len([r for r in w.a2a.requests if r["method"] == "SendStreamingMessage"]) == 1
    for t in list(w.bridge._tasks):
        t.cancel()
    await w.bridge.drain()


async def test_req13_ignores_bots_edits_self_and_other_channels(world):
    await world.link()
    await world.say("q", channel="C0OTHER")                              # not allowed
    await world.say("q", bot_id="B1")                                    # a bot
    await world.say("q", subtype="message_changed")                      # an edit
    await world.say("q", user=BOT)                                       # itself
    assert world.slack.calls == [("dm", USER, world.slack.calls[0][2])]  # only the link DM
    assert world.a2a.requests == []


async def test_req13_dm_works_without_allowlist(world):
    await world.link()
    await world.say("q", channel=DM, kind="dm")
    assert world.a2a.requests and world.slack.of("upload")[0][1] == DM


async def test_fm04_duplicate_event_delivery_runs_once(world):
    await world.link()
    await world.say("q", ts="1700.5")
    await world.say("q", ts="1700.5")
    assert len(world.a2a.requests) == 1


async def test_req14_timeout_cancels_the_task(world):
    w = World(run_timeout_s=0.2)
    await w.link()
    w.a2a.hang = True
    await w.say("q")
    assert w.a2a.cancelled == ["task-1"]
    assert any("longer than" in c[3] for c in w.slack.of("update"))


async def test_req15_summary_file_and_absolute_download_link(world):
    await world.link()
    await world.say("q")
    post = [c for c in world.slack.of("post") if c[4]][0]
    text = "\n".join(b["text"]["text"] for b in post[4])
    assert "*Lithium supply in 2030*" in text and "*fast*" in text and "_unevenly_" in text
    assert "<https://www.iea.org/report|IEA>" in text
    assert "<https://kagent.example.com/reports/abc123/run1/report.md|Download from the hub>" in text
    assert "Long findings text" not in text and "Full report in the attached file" in text
    assert "$0.14" in text
    upload = world.slack.of("upload")[0]
    assert upload[3] == "report-task-1.md" and "Long findings text" in upload[4]
    assert "12 turns" not in upload[4]                       # stats footer not in the file


async def test_req15_long_report_fits_slack_limits(world):
    await world.link()
    long_report = "# T\n\n## Summary\n\n" + ("word " * 2000) + "\n\n## More\n\nx"
    world.a2a.script = [task(), status(pb.TASK_STATE_COMPLETED, long_report)]
    await world.say("q")
    post = [c for c in world.slack.of("post") if c[4]][0]
    assert all(len(b["text"]["text"]) <= 3000 for b in post[4])
    assert len(world.slack.of("upload")[0][4]) > 10000


async def test_req16_failed_run_posts_fixed_error_with_reference(world, caplog):
    await world.link()
    world.a2a.script = [task(), status(pb.TASK_STATE_FAILED,
                                       "The research run failed (KeyError) at http://internal:8080")]
    await world.say("q")
    last = world.slack.of("update")[-1][3]
    assert "Reference:" in last and "internal" not in last and "KeyError" not in last


async def test_req16_rejection_text_is_shown(world):
    await world.link()
    world.a2a.script = [status(pb.TASK_STATE_REJECTED, "Keep the question under 2000 characters.")]
    await world.say("q")
    assert "under 2000 characters" in world.slack.of("update")[-1][3]


async def test_req16_unexpected_exception_is_not_leaked(world, caplog):
    await world.link()

    async def boom(*a, **k):
        raise RuntimeError("secret detail http://kagent-controller:8083 refresh-sub-alice")
    world.bridge.agent.run = boom
    await world.say("q")
    assert "secret detail" not in world.slack.all_text()
    assert "Reference:" in world.slack.of("post")[-1][2]


async def test_fm02_token_refused_by_proxy(world):
    """An unsigned or foreign token is refused at oauth2-proxy (here: the fake's 401)."""
    await world.link()
    world.a2a.valid_tokens.clear()
    await world.say("q")
    assert "Reference:" in world.slack.of("update")[-1][3]
    assert world.slack.of("upload") == []


async def test_req17_bridge_only_forwards_tokens_cognito_issued(world):
    """The bearer token is exactly what Cognito's refresh returned: never built locally."""
    await world.link()
    await world.say("q")
    assert world.a2a.requests[0]["auth"] == "Bearer idtok.sub-alice"


async def test_daily_limit(world):
    w = World(runs_per_day=1)
    await w.link()
    await w.say("q1")
    await w.say("q2")
    assert any("today's limit" in c[3] for c in w.slack.of("ephemeral"))
    assert len(w.a2a.requests) == 1


async def test_req18_fm09_no_secret_in_logs_or_slack(world, caplog):
    caplog.set_level(logging.DEBUG)
    await world.link()
    await world.say("q")
    world.cognito_fake.refresh_valid.clear()
    await world.say("q2")
    await world.say("disconnect")
    logged = caplog.text
    slack = world.slack.all_text()
    for marker in SECRET_MARKERS:
        assert marker not in logged, marker
    for marker in ("refresh-", "idtok.", "xoxb-", "xapp-", "client-secret"):
        assert marker not in slack, marker
    assert "client-secret" not in repr(world.settings) and "xoxb" not in repr(world.settings)


async def test_help_for_empty_mention(world):
    await world.link()
    await world.say("")
    assert "research question" in world.slack.of("ephemeral")[-1][3]
    assert world.a2a.requests == []
    assert REPORT  # fixture import used


async def test_req13_ignored_mention_is_logged_without_text(world, caplog):
    caplog.set_level(logging.INFO, logger="slack_bridge")
    await world.say("secret question text", channel="C0OTHER")
    assert "kind=mention ignored channel=C0OTHER" in caplog.text
    assert "secret question text" not in caplog.text
