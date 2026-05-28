FROM ghcr.io/astral-sh/uv:python3.11-alpine

# Install system dependencies
RUN apk add --no-cache \
    pkgconf \
    build-base \
    automake \
    autoconf \
    libtool \
    m4 \
    perl \
    git \
    linux-headers

WORKDIR /app

# Copy the rest of the application (required for uv sync to find the package)
COPY . .

# Install dependencies
RUN uv sync

ARG GIT_COMMIT=""
ARG GIT_TAG=""
ENV GIT_COMMIT=${GIT_COMMIT}
ENV GIT_TAG=${GIT_TAG}
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Run the application
CMD ["/app/.venv/bin/fastapi", "run", "routstr", "--host", "0.0.0.0"]
