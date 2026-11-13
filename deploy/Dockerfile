# One image, two entrypoints: `api.main:app` (default) and `python -m mcp_server`.
# Multi-stage keeps uv out of the runtime image; the same /app path in both
# stages lets the venv be copied without fixing shebangs.
# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS runtime

# The base image ships Debian packages that regularly have fixable HIGH/CRITICAL
# CVEs; apply security updates at build time so the scan gate means something.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 fintrace \
    && useradd --uid 10001 --gid fintrace --create-home fintrace
WORKDIR /app
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/data/traces && chown -R fintrace:fintrace /app
USER fintrace
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
