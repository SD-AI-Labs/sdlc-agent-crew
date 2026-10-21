# uv-based image: the same lockfile that CI and local development use.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependency layer first so code changes do not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra anthropic --extra postgres

COPY . .
RUN uv sync --frozen --extra anthropic --extra postgres

EXPOSE 8000
CMD ["deliveryops-api"]
