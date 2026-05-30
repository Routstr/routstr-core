FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libsecp256k1-dev \
        autoconf \
        automake \
        libtool \
    && rm -rf /var/lib/apt/lists/*

COPY uv.lock pyproject.toml ./
RUN mkdir -p /routstr

RUN uv sync --frozen --no-dev --no-install-project

WORKDIR /app

COPY . .

ARG GIT_COMMIT=""
ARG GIT_TAG=""
ENV GIT_COMMIT=${GIT_COMMIT}
ENV GIT_TAG=${GIT_TAG}
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["/.venv/bin/fastapi", "run", "routstr", "--host", "0.0.0.0"]
