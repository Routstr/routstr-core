FROM ghcr.io/astral-sh/uv:python3.11-alpine

# Install system dependencies required for secp256k1 and psutil
RUN apk add --no-cache \
    pkgconf \
    build-base \
    automake \
    autoconf \
    libtool \
    m4 \
    perl \
    linux-headers \
    python3-dev \
    musl-dev
RUN apk add git

COPY uv.lock pyproject.toml ./
RUN mkdir -p /routstr

RUN uv add git+https://github.com/saschanaz/secp256k1-py.git#branch=upgrade060

# Installs all dependecies pyproject.toml
RUN uv sync

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
