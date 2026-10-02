FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    ca-certificates \
    gnupg \
    zip \
    unzip \
    ripgrep \
    fd-find \
    jq \
    less \
    sqlite3 \
    procps \
    python3 \
    python3-pip \
    python3-venv

RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs

RUN rm -rf /var/lib/apt/lists/*

WORKDIR /service

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "mcp>=2,<3" \
    "requests>=2,<3" \
    "ipython" \
    "pytest" \
    "black" \
    "ruff" \
    "mypy"

COPY service.py .

RUN useradd --create-home --shell /usr/sbin/nologin service && chown -R service:service /service
USER service

EXPOSE 8000

ENTRYPOINT ["python", "service.py"]
