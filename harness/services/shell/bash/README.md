# Shell Bash

MCP service for autonomous agents to run shell commands in a shared working folder.

## What it does

`shell-bash` runs commands under bash in the background, giving autonomous agents a way to:

- Start a command in a given directory with extra environment variables, without waiting for it to finish
- Poll a command for the output it produced since the last poll, by stdout and stderr, and whether it's still running
- Stop a command and free its resources, killing it if it doesn't exit on its own

The image includes python 3 (with pytest, black, ruff, mypy, and ipython), node.js 24, search and data tools (ripgrep, fd, jq, sqlite3), and curl, wget, zip, and unzip.

## Configuration

The service reads the following environment variables:

| Variable | Description |
|---|---|
| SERVICE_NAME | Optional: Registration name for this service |
| SERVICE_ROLE | Optional: Registration role for this service (e.g. shell, feedback, etc.) |
| REGISTRY_URL | Endpoint of registry for agent tool discovery |
| ENDPOINT_URL | Self-stated url for discovering and invoking service tools |
| DIRECTION_MD | Optional override of the bundled agent policy markdown direction.md; replaces it entirely |
| PORT         | Optional override, default = 8000 |
| LOG_LEVEL    | Optional override, default = INFO |

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

And add an entry to your mcp servers (e.g., claude code's .mcp.json):

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
