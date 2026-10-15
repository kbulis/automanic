FROM python:3.13-slim

ADD --chmod=644 --checksum=sha256:bd70a5e4a268002704024ceba7f8446024114e94f3f0bdd11c23a9e592be81c6 https://downloads.claude.ai/keys/claude-code.asc /etc/apt/keyrings/claude-code.asc
RUN echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" > /etc/apt/sources.list.d/claude-code.list

RUN apt-get update && apt-get install -y --no-install-recommends claude-code && rm -rf /var/lib/apt/lists/*

WORKDIR /service

RUN pip install --quiet --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "dotenv>=0,<1" \
    "uvicorn>=0,<1" \
    "fastapi>=0,<1" \
    "mcp>=2,<3"

RUN useradd --create-home --shell /usr/sbin/nologin service && chown -R service:service /service
USER service

ENV DISABLE_AUTOUPDATER=1

EXPOSE 8000

COPY service.py .

ENTRYPOINT ["python", "service.py"]
