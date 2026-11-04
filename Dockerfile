# syntax=docker/dockerfile:1
# Official Python images have variants for both linux/amd64 and linux/arm64.

# The SQLite index is architecture-independent, so it is built once on the
# build machine's own platform rather than under emulation for each target.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS index
WORKDIR /build
COPY src ./src
# The dataset comes from the upstream git submodule (git submodule update --init).
COPY upstream/data/merged ./data
RUN PYTHONPATH=src python -m jizdni_rady_mcp.index data /build/gtfs.sqlite

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY pyproject.toml ./
COPY src ./src
# mcp-proxy is used only by the optional Home Assistant/SSE Compose service.
RUN pip install --no-cache-dir . "mcp-proxy==0.12.0"

# Only the index goes into the image, not the GTFS files it was built from.
# The server finds it there without any environment variable: mcp-proxy
# starts it with an empty environment.
COPY --from=index /build/gtfs.sqlite /usr/local/share/jizdni-rady-cz-mcp/gtfs.sqlite

USER app

# MCP uses standard input/output, so no TCP port is opened or exposed.
CMD ["jizdni-rady-cz-mcp"]
