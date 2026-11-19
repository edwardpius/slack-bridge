"""Entry point: Slack Socket Mode (outbound only) plus the linking callback server."""
from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from . import otel
from .agent import AgentClient
from .bridge import Bridge
from .cognito import Cognito
from .config import Settings
from .links import OpenBaoLinkStore
from .web import make_app

log = logging.getLogger("slack_bridge")


class SlackWeb:
    """SlackPort on Slack's Web API."""

    def __init__(self, client: AsyncWebClient) -> None:
        self.c = client

    async def post_ephemeral(self, channel, user, text, thread_ts=None):
        await self.c.chat_postEphemeral(channel=channel, user=user, text=text,
                                        thread_ts=thread_ts)

    async def post(self, channel, text, thread_ts=None, blocks=None):
        r = await self.c.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts,
                                          blocks=blocks, unfurl_links=False, unfurl_media=False)
        return r["ts"]

    async def update(self, channel, ts, text, blocks=None):
        await self.c.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)

    async def upload(self, channel, thread_ts, filename, content, title):
        await self.c.files_upload_v2(channel=channel, thread_ts=thread_ts, filename=filename,
                                     content=content, title=title)

    async def dm(self, user, text):
        await self.c.chat_postMessage(channel=user, text=text)


def build(settings: Settings) -> tuple[AsyncApp, Bridge]:
    app = AsyncApp(token=settings.slack_bot_token)
    cognito = Cognito(settings.cognito_domain, settings.cognito_client_id,
                      settings.cognito_client_secret, settings.redirect_uri, settings.cognito_idp)
    links = OpenBaoLinkStore(settings.openbao_addr, settings.openbao_role, settings.openbao_mount,
                             settings.sa_token_path, verify=settings.openbao_ca or True)
    agent = AgentClient(settings.agent_url, settings.run_timeout_s)
    bridge = Bridge(settings, SlackWeb(app.client), links, cognito, agent)

    @app.event("app_mention")
    async def on_mention(event, body):
        await bridge.handle(event, "mention", body.get("team_id", ""))

    @app.event("message")
    async def on_message(event, body):
        if event.get("channel_type") == "im":
            await bridge.handle(event, "dm", body.get("team_id", ""))

    return app, bridge


async def serve(settings: Settings) -> None:
    app, bridge = build(settings)
    auth = await app.client.auth_test()
    bridge.bot_user_id = auth["user_id"]
    runner = web.AppRunner(make_app(bridge.complete_link), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.listen_host, settings.listen_port).start()
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    await handler.connect_async()
    log.info("slack-bridge up: bot %s, %d allowed channel(s), callback %s",
             bridge.bot_user_id, len(settings.allowed_channels), settings.redirect_uri)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")
    await handler.close_async()
    await asyncio.wait_for(bridge.drain(), timeout=20)
    await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Slack's and httpx's own loggers could print URLs with codes; keep them quiet.
    for name in ("httpx", "slack_sdk", "slack_bolt"):
        logging.getLogger(name).setLevel(logging.WARNING)
    otel.setup()
    asyncio.run(serve(Settings.from_env()))


if __name__ == "__main__":
    main()
