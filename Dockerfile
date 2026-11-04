# API image for Render (render.yaml). The web app is a separate Node service.
FROM python:3.13-slim

# LightGBM needs the OpenMP runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && apt-get clean
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 FASTEMBED_CACHE_PATH=/opt/fastembed
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY api ./api
COPY agents ./agents
COPY ml ./ml
COPY scripts ./scripts

# fetch the sentence-embedding model now so the first request does not download it
RUN python -c "from ml.claim_model import ClaimClassifier; ClaimClassifier.load()"

# Render sets PORT; AGENT_ENABLED runs the agents inside this one process, so keep a single instance
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
