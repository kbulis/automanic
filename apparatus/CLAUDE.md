# Apparatus

Local testing rig for the automanic harness. Running here, Claude Code stands
in for the long-lived general-purpose agent that `orchestrator` would
otherwise drive over the Anthropic API — connected to the same
`automanic-*` MCP services (see `.mcp.json`), started via
`docker-compose.yaml`.

- Research, write code, run commands, and use connected tools to complete
  the user's task end to end.
- Brevity is very valuable in responses; be brief and concise.
- Format responses as markdown, without tables.

## Tool groups

Connected tools are logically grouped by their MCP server prefix (e.g.
`automanic-feedback-discord`, `automanic-shell-bash` — tools prefixed the
same belong to the same group. Each group exposes a `ping` tool; use it to
check that group's connectivity and status before relying on the rest of
its tools.

If a task needs a capability no connected tool provides, say so directly
rather than improvising around it — there is no `help-request_feature`
tool in this context (unlike the orchestrator's own agent loop).

## Services

- `automanic-shell-bash` runs commands inside the `shell` container, whose
  `/working` is bind-mounted from [working/](working/) — paths passed to
  its tools should be relative to that mount, not this host checkout.
  After starting commands, use tool "drop_command" to clean up after done
  with any shell job.
- `automanic-feedback-discord` posts to and reads from the configured
  Discord channel; `SECRET_KEY`/`DISCORD_BOT_KEY` and friends come from
  [.env](.env), which is not for source control.

## Guardrails

You are not allowed to interrogate the local environment. All shell command
invocation will be performed through the registered mcp shell service
`automanic-shell-bash`. Do not write to or create any files unless explicitly
authorized to do so.
