FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    torch==2.6.0 \
    --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir \
    fastapi==0.111.0 \
    "uvicorn[standard]==0.29.0" \
    pydantic==2.7.1 \
    sentence-transformers==2.7.0 \
    "qdrant-client==1.7.3"

COPY guardrail_service.py .
COPY layer1_input_guardrails.py .
COPY layer4_output_guardrails.py .
COPY data/ ./data/
