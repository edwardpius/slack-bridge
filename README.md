# slack-bridge

Ask research-agent from Slack, as your own hub user. Part of Edward's AI Hub.

- **Spec:** `ai-hub/specs/H-006-slack-bridge/spec.md` · **Decision:** ai-hub HD-006 ·
  **Platform:** openbao-platform D-28 · **Plan:** `ai-hub/docs/slack-integration.md`

## How it works

```
Slack ──Socket Mode (outbound WebSocket)──> slack-bridge (namespace agents)
                                              │ refresh the user's Cognito ID token
                                              │ (refresh token in OpenBao slack-links/)
                                              v
          kagent-oauth2-proxy :4180  (verifies the token's signature)
            -> kagent-signout nginx  -> kagent controller  POST /agents/kagent/research-agent
                                                           (A2A JSON-RPC, streamed)
```

1. A user mentions the bot in an allowed channel, or DMs it.
2. **Not linked yet:** the bot replies privately with a sign-in link (Cognito, through
   Identity Center, PKCE, one-time `state`, 10 minutes). The callback
   `https://kagent.<domain>/slack/oauth/callback` stores the refresh token (7 days) in
   OpenBao under `slack-links/<team>/<user>`.
3. **Linked:** the bridge posts "Researching…" in the thread, refreshes the user's ID
   token, and calls research-agent with it. The controller sees the same principal as the
   kagent UI, so conversations and reports are shared with the UI.
4. Progress updates the status message (at most every 5 s). When the run completes, the
   bridge posts the summary, the full report as a `.md` file, and the hub download link.
5. Replies in the thread continue the same research conversation (in memory: lost on restart).
6. `disconnect` revokes and deletes the link.

The bridge never creates or alters a token, never logs one, and posts only fixed error text
with a reference ID (details in the log and trace).

## Configuration

| Env | |
|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | from `kv/projects/slack-bridge/slack` |
| `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET`, `COGNITO_DOMAIN` | from `kv/projects/slack-bridge/cognito` (layer 50) |
| `PUBLIC_BASE_URL` | `https://kagent.<domain>` |
| `OPENBAO_ADDR`, `OPENBAO_CACERT` | OpenBao, Kubernetes auth role `slack-bridge` |
| `ALLOWED_CHANNELS` | comma-separated channel IDs; DMs always work |
| `AGENT_URL` | default `http://kagent-oauth2-proxy.kagent.svc.cluster.local:4180/agents/kagent/research-agent` |
| `RUN_TIMEOUT_S`, `RUNS_PER_DAY` | 600, 20 |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | optional |

## Commands

```bash
uv run pytest            # unit tests, offline: fake Slack, fake Cognito, fake A2A
uv run slack-bridge      # needs the settings above and a ServiceAccount token
```
