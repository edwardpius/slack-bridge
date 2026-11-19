# Working in this repo

Spec-driven, as the other hub repos: the spec is `ai-hub/specs/H-006-slack-bridge/spec.md`.
No spec, no code; never build past a `[NEEDS CLARIFICATION: …]` marker.

## Rules that are code, not prompt text

- **Only Cognito-issued tokens:** the bridge never creates, signs or alters a JWT (REQ-17).
  It calls kagent with the asking user's own ID token, through oauth2-proxy.
- **No token, secret or stack trace in Slack, logs or traces** (REQ-16, REQ-18): fixed error
  text with a reference ID. `test_req18_fm09_no_secret_in_logs_or_slack` must keep passing.
- **Answer only where allowed** (REQ-13): allowlisted channels and DMs; never bots or itself.
- Refresh tokens live only in OpenBao `slack-links/`; nothing else may read that mount.

## Commands

```bash
uv run pytest            # offline unit tests
```

Live checks need the hub and a Slack workspace: ask before running them. Reference
requirement IDs in tests and commit messages.
