# One container serves the API and the demo UI (FastAPI serves app/static at /).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first (cached layer), exactly as pinned in uv.lock, without dev tools.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code, plus the example plans used by LLM_MODE=fake.
COPY app ./app
COPY examples/plans ./examples/plans
RUN uv sync --frozen --no-dev

# Hosts (Render, Cloud Run, Fly, Railway) inject PORT. --proxy-headers makes the per-client rate
# limit see the real client IP behind the platform's load balancer.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec /app/.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
