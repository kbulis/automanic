FROM python:3-slim

RUN apt-get update && rm -rf /var/lib/apt/lists/*

WORKDIR /service

RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip && pip install --no-cache-dir --root-user-action=ignore \
    "dotenv>=0,<1" \
    "uvicorn>=0,<1" \
    "fastapi>=0,<1" \
    "anthropic>=1,<2" \
    "mcp>=2,<3"

COPY service.py .

EXPOSE 8000

ENTRYPOINT ["python", "service.py"]
