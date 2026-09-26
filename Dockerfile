FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Non-root: this is the one image both process groups in fly.toml run (web
# and worker), so neither ever runs as root in prod. keys/ and .env* stay out
# of the image entirely (.dockerignore); nothing here needs to write to /app
# at runtime.
RUN groupadd --system app \
    && useradd --system --create-home --home-dir /app --gid app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8080
# Web process. The worker (fly.toml [processes].worker, no http_service) runs
# the same image with:
#   python -m alphamarket.worker
CMD ["sh", "-c", "alembic upgrade head && uvicorn alphamarket.main:app --host 0.0.0.0 --port 8080"]
