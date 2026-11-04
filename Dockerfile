# syntax=docker/dockerfile:1
# Official Python images have variants for both linux/amd64 and linux/arm64.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GTFS_DATASET=/app/data/merged

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml ./
COPY src ./src
# mcp-proxy is used only by the optional Home Assistant/SSE Compose service.
RUN pip install --no-cache-dir . "mcp-proxy==0.12.0"

# The dataset comes from the upstream git submodule (git submodule update --init).
COPY --chown=app:app upstream/data ./data

USER app

# MCP uses standard input/output, so no TCP port is opened or exposed.
CMD ["jizdni-rady-cz-mcp"]
