"""H-006 REQ-01 to REQ-07: linking a Slack user to their hub account."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from conftest import CHANNEL, OTHER, TEAM, USER


async def test_req01_unlinked_user_gets_private_signin_link_and_no_run(world):
    await world.say("what is lithium supply in 2030?")
    eph = world.slack.of("ephemeral")
    assert len(eph) == 1 and eph[0][2] == USER
    assert "sign in to the hub" in eph[0][3]
    assert world.a2a.requests == []                       # no research call
    assert world.slack.of("post") == []                   # nothing public


async def test_req01_signin_link_has_pkce_state_and_identity_center(world):
    await world.say("hi")
    url = world.slack.of("ephemeral")[0][3].split("<", 1)[1].split("|", 1)[0]
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert urlsplit(url).netloc == "auth.example.com"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["redirect_uri"] == "https://kagent.example.com/slack/oauth/callback"
    assert q["identity_provider"] == "IdentityCenter" and q["state"]
    assert "code_verifier" not in url


async def test_req02_callback_stores_refresh_token_and_confirms_by_dm(world):
    status, text = await world.link()
    assert status == 200 and "alice@example.com" in text
    link = await world.links.get(TEAM, USER)
    assert link.refresh_token == "refresh-sub-alice" and link.sub == "sub-alice"
    assert world.slack.of("dm")[0][1] == USER
    assert "refresh-" not in repr(link)                   # never in reprs or logs


async def test_req03_unknown_reused_or_expired_state_is_refused(world):
    state, challenge = world.pending.create(TEAM, USER)
    world.cognito_fake.add_code("code-1", "sub-alice", "a@x", challenge)
    assert (await world.bridge.complete_link("nope", "code-1"))[0] == 400
    assert (await world.bridge.complete_link(None, "code-1"))[0] == 400
    assert (await world.bridge.complete_link(state, "code-1"))[0] == 200
    assert (await world.bridge.complete_link(state, "code-1"))[0] == 400      # reused
    state2, ch2 = world.pending.create(TEAM, OTHER)
    world.cognito_fake.add_code("code-2", "sub-bob", "b@x", ch2)
    world.now[0] += 601                                                       # expired
    assert (await world.bridge.complete_link(state2, "code-2"))[0] == 400
    assert await world.links.get(TEAM, OTHER) is None


async def test_req03_state_binds_the_slack_user_it_was_shown_to(world):
    """FM-01: Bob's sign-in through a link shown to Alice links Alice's Slack user only
    to what Alice's link says; Bob's own Slack user stays unlinked."""
    state, challenge = world.pending.create(TEAM, USER)
    world.cognito_fake.add_code("code-x", "sub-alice", "a@x", challenge)
    await world.bridge.complete_link(state, "code-x")
    assert (await world.links.get(TEAM, USER)).sub == "sub-alice"
    assert await world.links.get(TEAM, OTHER) is None


async def test_req03_pkce_verifier_must_match(world):
    state, _ = world.pending.create(TEAM, USER)
    world.cognito_fake.add_code("code-y", "sub-alice", "a@x", "wrong-challenge")
    status, _ = await world.bridge.complete_link(state, "code-y")
    assert status == 400 and await world.links.get(TEAM, USER) is None


async def test_req04_refused_signin_stores_nothing(world):
    """An unassigned Identity Center user never gets a code; Cognito's error ends here."""
    from aiohttp.test_utils import TestClient, TestServer

    from slack_bridge.web import make_app
    client = TestClient(TestServer(make_app(world.bridge.complete_link)))
    await client.start_server()
    try:
        r = await client.get("/slack/oauth/callback?error=access_denied&state=s")
        assert r.status == 400 and "refused" in await r.text()
        assert r.headers["Cache-Control"] == "no-store"
    finally:
        await client.close()
    assert world.links.items == {}


async def test_callback_page_never_echoes_code_or_state(world):
    from aiohttp.test_utils import TestClient, TestServer

    from slack_bridge.web import make_app
    client = TestClient(TestServer(make_app(world.bridge.complete_link)))
    await client.start_server()
    try:
        r = await client.get("/slack/oauth/callback?code=code-SECRET&state=<script>")
        body = await r.text()
        assert r.status == 400 and "code-SECRET" not in body and "<script>" not in body
    finally:
        await client.close()


async def test_req05_disconnect_revokes_and_deletes(world):
    await world.link()
    await world.say("disconnect")
    assert world.cognito_fake.revoked == ["refresh-sub-alice"]
    assert await world.links.get(TEAM, USER) is None
    assert "unlinked" in world.slack.of("ephemeral")[-1][3]


async def test_req05_disconnect_deletes_even_if_revoke_fails(world):
    await world.link()
    world.cognito_fake.down = True
    await world.say("disconnect")
    assert await world.links.get(TEAM, USER) is None


async def test_req06_failed_refresh_unlinks_and_offers_new_link(world):
    await world.link()
    world.cognito_fake.refresh_valid.clear()             # removed from Identity Center
    await world.say("a question")
    assert await world.links.get(TEAM, USER) is None
    assert any("expired" in c[3] for c in world.slack.of("update"))
    assert "sign in to the hub" in world.slack.of("ephemeral")[-1][3]
    assert world.a2a.requests == []


async def test_transient_cognito_outage_keeps_the_link(world):
    await world.link()
    world.cognito_fake.down = True
    await world.say("a question")
    assert await world.links.get(TEAM, USER) is not None
    assert "Reference:" in world.slack.of("post")[-1][2]


async def test_links_reject_path_like_ids(world):
    import pytest

    from slack_bridge.links import LinkStoreError
    with pytest.raises(LinkStoreError):
        await world.links.get(TEAM, "../kv/data")
    with pytest.raises(LinkStoreError):
        await world.links.get("t0team", USER)
    assert CHANNEL  # keep import used
