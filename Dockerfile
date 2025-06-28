FROM ghcr.io/astral-sh/uv:python3.11-alpine

# Install system dependencies required for secp256k1
RUN apk add --no-cache \
    pkgconf \
    build-base \
    automake \
    autoconf \
    libtool \
    m4 \
    perl
RUN apk add git

COPY uv.lock pyproject.toml ./

RUN uv sync

WORKDIR /app

COPY . .

ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["/.venv/bin/fastapi", "run", "router", "--host", "0.0.0.0"]
