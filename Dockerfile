FROM python:3.13-slim

# Install system dependencies required for secp256k1 and git
RUN apt-get update && apt-get install -y \
    build-essential \
    pkg-config \
    autoconf \
    automake \
    libtool \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Set working directory
WORKDIR /app

# Copy dependency files
COPY uv.lock pyproject.toml ./

# Install dependencies
RUN uv sync --frozen

# Copy application code
COPY . .

# Set environment variables
ENV PORT=8000
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "fastapi", "run", "router", "--host", "0.0.0.0"]
