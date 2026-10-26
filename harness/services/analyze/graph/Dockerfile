FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get upgrade -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --quiet --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "certifi>=2026.10.19" \
    "mcp>=2,<3" \
    "tree-sitter==0.25.2" \
    "tree-sitter-python==0.25.0" \
    "tree-sitter-javascript==0.25.0" \
    "tree-sitter-typescript==0.23.2" \
    "tree-sitter-go==0.25.0" \
    "tree-sitter-hcl==1.2.0" \
    "requests>=2,<3"

WORKDIR /service

COPY direction.md .
COPY service.py .

RUN useradd --create-home --shell /usr/sbin/nologin service && chown -R service:service /service
USER service

EXPOSE 8000

ENTRYPOINT ["python", "service.py"]
