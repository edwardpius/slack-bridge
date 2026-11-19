"""HTTP side: the account-linking callback and a health check.

Reached as https://kagent.<domain>/slack/oauth/callback through the ALB, oauth2-proxy
(skip-auth-route) and kagent-signout (openbao-platform D-28). Pages never echo the code or
state back.
"""
from __future__ import annotations

import html

from aiohttp import web

from .bridge import Callback
from .config import CALLBACK_PATH

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Slack link</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px;
color:#18181b;background:#fafafa}}@media(prefers-color-scheme:dark){{body{{color:#e4e4e7;
background:#18181b}}}}</style></head><body><p>{text}</p></body></html>"""

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; "
           "style-src 'unsafe-inline'"}


def make_app(callback: Callback) -> web.Application:
    async def oauth_callback(request: web.Request) -> web.Response:
        if request.query.get("error"):
            status, text = 400, "Sign-in was cancelled or refused. Ask the bot in Slack again."
        else:
            status, text = await callback(request.query.get("state"), request.query.get("code"))
        return web.Response(status=status, text=_PAGE.format(text=html.escape(text)),
                            content_type="text/html", headers=HEADERS)

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get(CALLBACK_PATH, oauth_callback)
    app.router.add_get("/healthz", healthz)
    return app
