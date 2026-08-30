# Shell Bash

...

## What it does

`shell-bash` ...

## Configuration

The service requires the following environment variables:

| Variable | Description |
|---|---|
| SERVICE_NAME | Optional: Registration name for this service |
| SERVICE_ROLE | Optional: Registration role for this service (e.g. shell, feedback, etc.) |
| ENDPOINT_URL | Self-stated url for discovering and invoking service tools |
| REGISTRY_URL | Endpoint of registry for agent tool discovery |
| PORT         | Optional override, default = 8000 |

## Docker

Pull the latest release:

```bash
docker pull automanic/shell-bash:latest
```

## Docker Compose with agents

Add this service entry with your env variables mapped:

```yaml
services:

  orchestrator:
    ...

  shell:
    image: automanic/shell-bash:latest
    environment:
      SERVICE_NAME: shell-bash
      SERVICE_ROLE: shell
      ENDPOINT_URL: http://shell:8000/mcp
      REGISTRY_URL: http://orchestrator:8000/registry
    ports:
      - "7002:8000"
    volumes:
      - ./working:/working
```

And add an entry to you mcp servers (e.g., claude code's .mcp.json):

```json
{
    "mcpServers": {
        "automanic-shell-bash": {
            "type": "http",
            "url": "http://localhost:7002/mcp"
        }
    }
}
```
