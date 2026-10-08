# Cached project runtime supplies Python; no package installation or secrets.
ARG BASE_IMAGE=ghcr.io/astral-sh/uv:python3.11-bookworm-slim
FROM ${BASE_IMAGE}
WORKDIR /smoke
COPY fake_openrouter.py .
USER 65534:65534
EXPOSE 8080
CMD ["python", "/smoke/fake_openrouter.py"]
