"""Settings from the environment (H-006). Secrets arrive as env vars from an ExternalSecret.

`Settings` never prints a secret: the secret fields are excluded from repr.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_AGENT_URL = (
    "http://kagent-oauth2-proxy.kagent.svc.cluster.local:4180/agents/kagent/research-agent")
CALLBACK_PATH = "/slack/oauth/callback"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str = field(repr=False)
    slack_app_token: str = field(repr=False)
    cognito_client_id: str
    cognito_client_secret: str = field(repr=False)
    cognito_domain: str                      # hosted UI domain, no scheme
    public_base_url: str                     # https://kagent.<domain>
    openbao_addr: str
    agent_url: str = DEFAULT_AGENT_URL       # through oauth2-proxy (REQ-09)
    cognito_idp: str = "IdentityCenter"      # skip Cognito's provider chooser
    openbao_role: str = "slack-bridge"
    openbao_mount: str = "slack-links"
    openbao_ca: str | None = None
    sa_token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    allowed_channels: frozenset[str] = frozenset()   # REQ-13, GUARD-04
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    run_timeout_s: float = 600.0             # REQ-14
    progress_interval_s: float = 5.0         # REQ-11
    runs_per_day: int = 20                   # Budgets
    state_ttl_s: float = 600.0               # REQ-03

    @property
    def redirect_uri(self) -> str:
        return self.public_base_url.rstrip("/") + CALLBACK_PATH

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        required = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "COGNITO_CLIENT_ID",
                    "COGNITO_CLIENT_SECRET", "COGNITO_DOMAIN", "PUBLIC_BASE_URL", "OPENBAO_ADDR"]
        missing = [k for k in required if not e.get(k)]
        if missing:
            raise ConfigError("missing settings: " + ", ".join(missing))
        if not e["PUBLIC_BASE_URL"].startswith("https://"):
            raise ConfigError("PUBLIC_BASE_URL must be https://")
        channels = frozenset(c.strip() for c in e.get("ALLOWED_CHANNELS", "").split(",") if c.strip())
        return cls(
            slack_bot_token=e["SLACK_BOT_TOKEN"],
            slack_app_token=e["SLACK_APP_TOKEN"],
            cognito_client_id=e["COGNITO_CLIENT_ID"],
            cognito_client_secret=e["COGNITO_CLIENT_SECRET"],
            cognito_domain=e["COGNITO_DOMAIN"].removeprefix("https://").rstrip("/"),
            public_base_url=e["PUBLIC_BASE_URL"].rstrip("/"),
            openbao_addr=e["OPENBAO_ADDR"],
            agent_url=e.get("AGENT_URL") or DEFAULT_AGENT_URL,
            cognito_idp=e.get("COGNITO_IDP", "IdentityCenter"),
            openbao_role=e.get("OPENBAO_ROLE", "slack-bridge"),
            openbao_mount=e.get("OPENBAO_MOUNT", "slack-links"),
            openbao_ca=e.get("OPENBAO_CACERT") or None,
            allowed_channels=channels,
            listen_port=int(e.get("LISTEN_PORT", "8080")),
            run_timeout_s=float(e.get("RUN_TIMEOUT_S", "600")),
            runs_per_day=int(e.get("RUNS_PER_DAY", "20")),
        )
