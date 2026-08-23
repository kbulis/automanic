FROM python:3-slim

RUN apt-get update && rm -rf /var/lib/apt/lists/*

WORKDIR /service

RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "mcp>=2,<3" \
    "requests>=2,<3" \
    "discord.py>=2,<3"

COPY service.py .

EXPOSE 8000

ENTRYPOINT ["python", "service.py"]
