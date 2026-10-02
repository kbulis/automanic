# Analyze Graph

MCP service for autonomous agents to efficiently organize and analyze code bases using tree-sitter.

## What it does

`analyze-graph` parses a source tree into a code graph stored in sqlite, giving autonomous agents a way to:

- Build or update the graph of a source tree; only changed files are parsed again
- Find where to start in an unfamiliar code base, including each project of a monorepo
- Outline files and look up symbols by name, without reading whole files
- See what a symbol calls, renders, inherits, references, and is used by
- Follow those relationships to find what a change can break and which tests reach it
- Read source lines of a symbol or file

Supported languages: python, javascript, typescript (including jsx and tsx), and go.

## Configuration

The service requires the following environment variables:

| Variable | Description |
|---|---|
| SERVICE_NAME | Optional: Registration name for this service |
| SERVICE_ROLE | Optional: Registration role for this service (e.g. analyze, repository, etc.) |
| ENDPOINT_URL | Self-stated url for discovering and invoking service tools |
| REGISTRY_URL | Endpoint of registry for agent tool discovery |
| PORT         | Optional override, default = 8000 |
| LOG_LEVEL    | Optional override, default = INFO |

## Docker

Pull the latest release:

```bash
docker pull automanic/analyze-graph:latest
```

## Docker Compose with agents

Add this service entry with your env variables mapped, mounting the source trees to analyze:

```yaml
services:

  orchestrator:
    ...

  analyze:
    image: automanic/analyze-graph:latest
    environment:
      SERVICE_NAME: analyze-graph
      SERVICE_ROLE: analyze
      ENDPOINT_URL: http://analyze:8000/mcp
      REGISTRY_URL: http://orchestrator:8000/registry
    ports:
      - "7003:8000"
    volumes:
      - ./working:/working
```

And add an entry to your mcp servers (e.g., claude code's .mcp.json):

```json
{
    "mcpServers": {
        "automanic-analyze-graph": {
            "type": "http",
            "url": "http://localhost:7003/mcp"
        }
    }
}
```
