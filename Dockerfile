FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

COPY uv.lock pyproject.toml ./
RUN mkdir -p /routstr

COPY . .

RUN uv sync --frozen --no-dev

ARG GIT_COMMIT=""
ARG GIT_TAG=""
ENV GIT_COMMIT=${GIT_COMMIT}
ENV GIT_TAG=${GIT_TAG}
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["/app/.venv/bin/fastapi", "run", "routstr", "--host", "0.0.0.0"]
