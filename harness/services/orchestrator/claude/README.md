# Orchestrator Claude

Orchestration server for managing autonomous agents and context. Uses
anthropic api with managed context and tooling.

## What it does

`orchestrator-claude` manages agent workflows as prompted through
feedback service message events.

## API

| Endpoint | Description |
|---|---|
| `POST /registry` | Registers an mcp tool service (name, role, direction, endpoint) so its tools become available to the agent. |
| `POST /messages` | Queues a user message for a session for the agent to pick up and process. |

## Configuration

The service reads the following environment variables:

| Variable | Description |
|---|---|
| AGENT_API_KEY | Anthropic api key used by the agent; the service fails to start without it |
| AGENT_MODEL | Optional override, default = claude-opus-5-5 |
| PORT         | Optional override, default = 8000 |
| LOG_LEVEL    | Optional override, default = INFO |

Note: AGENT_API_KEY is secret. Do **not** commit your anthropic api key to source control.

## Docker

Pull the latest release:

```bash
docker pull automanic/orchestrator-claude:latest
```

## Docker Compose with agents

Add this service entry with your env variables mapped:

```yaml
services:

  orchestrator:
    image: automanic/orchestrator-claude:latest
    environment:
      AGENT_API_KEY: ${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required}
      AGENT_MODEL: ${CLAUDE_MODEL:-}
    ports:
      - "7000:8000"
```
