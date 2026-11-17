# syntax=docker/dockerfile:1
# Official Python images have variants for both linux/amd64 and linux/arm64.

# The SQLite index is architecture-independent, so it is built once on the
# build machine's own platform rather than under emulation for each target.
FROM --platform=$BUILDPLATFORM python:3.12-slim AS index
# The dataset: the upstream's weekly data release. Its main branch gets new
# data only once a month, and Prague's timetables in it run out within two
# weeks. update-upstream.yml moves this to the latest release every day.
ARG UPSTREAM_RELEASE=data-2026-11-16
WORKDIR /build
COPY src ./src
RUN python - <<EOF
import io, urllib.request, zipfile
tag = "${UPSTREAM_RELEASE}"
url = f"https://github.com/tangero/jizdni-rady-czech-republic/releases/download/{tag}/gtfs-cz-{tag.removeprefix('data-')}.zip"
with urllib.request.urlopen(url, timeout=300) as response:
    archive = zipfile.ZipFile(io.BytesIO(response.read()))
for name in archive.namelist():
    if name.startswith("data/merged/") and not name.endswith("/"):
        archive.extract(name, "release")
print(url, sorted(n for n in archive.namelist() if n.startswith("data/merged/")))
EOF
RUN PYTHONPATH=src python -m jizdni_rady_mcp.index release/data/merged /build/gtfs.sqlite

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
