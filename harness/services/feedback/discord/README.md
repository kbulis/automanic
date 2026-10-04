# Feedback Discord

MCP service for autonomous agents to request user feedback through Discord.

## What it does

`feedback-discord` gives autonomous agents a way to:

- Post feedback requests to a configured Discord channel
- Organize responses into sessions
- Feed those responses back into the agent workflow

## Configuration

The service reads the following environment variables:

| Variable | Description |
|---|---|
| SERVICE_NAME | Optional: Registration name for this service |
| SERVICE_ROLE | Optional: Registration role for this service (e.g. feedback, repository, etc.) |
| SECRET_KEY   | Discord bot token used by the service |
| CHANNEL_ID   | Discord channel id where feedback requests are communicated |
| ROBOT_NAME   | Name used when posting feedback requests |
| MESSAGES_URL | Endpoint of participating agent for posting feedback messages |
| REGISTRY_URL | Endpoint of registry for agent tool discovery |
| ENDPOINT_URL | Self-stated url for discovering and invoking service tools |
| DIRECTION_MD | Optional override of the bundled agent policy markdown direction.md; replaces it entirely |
| PORT         | Optional override, default = 8000 |
| LOG_LEVEL    | Optional override, default = INFO |

Note: SECRET_KEY contains a secret key. Do **not** commit your Discord bot key to source control.

## Docker

Pull the latest release:

```bash
docker pull automanic/feedback-discord:latest
```

## Docker Compose with agents

Add this service entry with your env variables mapped:

```yaml
services:

  orchestrator:
    ...

  feedback:
    image: automanic/feedback-discord:latest
    environment:
      SERVICE_NAME: feedback-discord
      SERVICE_ROLE: feedback
      SECRET_KEY: ${DISCORD_BOT_KEY:?DISCORD_BOT_KEY is required}
      CHANNEL_ID: ${DISCORD_CHANNEL:?DISCORD_CHANNEL is required}
      ROBOT_NAME: ${POSTING_AS_NAME:?POSTING_AS_NAME is required}
      ENDPOINT_URL: http://feedback:8000/mcp
      MESSAGES_URL: http://orchestrator:8000/messages
      REGISTRY_URL: http://orchestrator:8000/registry
    ports:
      - "7001:8000"
```

And add an entry to your mcp servers (e.g., claude code's .mcp.json):

```json
{
    "mcpServers": {
        "automanic-feedback-discord": {
            "type": "http",
            "url": "http://localhost:7001/mcp"
        }
    }
}
```
