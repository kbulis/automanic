FROM python:3.13-slim

RUN apt-get update && rm -rf /var/lib/apt/lists/*

WORKDIR /service

RUN pip install --quiet --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "mcp>=2,<3" \
    "requests>=2,<3" \
    "discord.py>=2,<3"

COPY direction.md .
COPY service.py .

RUN useradd --create-home --shell /usr/sbin/nologin service && chown -R service:service /service
USER service

EXPOSE 8000

ENTRYPOINT ["python", "service.py"]
