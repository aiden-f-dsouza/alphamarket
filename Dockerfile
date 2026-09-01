FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
# Web process. The worker runs the same image with:
#   python -m alphamarket.worker
CMD ["sh", "-c", "alembic upgrade head && uvicorn alphamarket.main:app --host 0.0.0.0 --port 8080"]
