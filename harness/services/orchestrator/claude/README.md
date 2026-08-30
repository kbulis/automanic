# Orchestrator Claude

Orchestration server for managing autonoumous agents and context. Uses
anthropic api with managed context and tooling.

## What it does

`orchestration-claude` manages agent workflows as prompted through
feedback service message events.

## API

| Endpoint | Description |
|---|---|
| `POST /registry` | Registers an mcp tool service (name, role, endpoint) so its tools become available to the agent. |
| `POST /messages` | Queues a user message for a session for the agent to pick up and process. |

## Configuration

The service requires the following environment variables:

| Variable | Description |
|---|---|
| AGENT_API_KEY | ... |
| AGENT_MODEL | ... |

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

  orchestrator-claude:
    image: automanic/orchestrator-claude:latest
    environment:
      AGENT_API_KEY: ${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required}
      AGENT_MODEL: ${CLAUDE_MODEL:-}
    ports:
      - "7000:8000"
```
