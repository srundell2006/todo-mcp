# todo-mcp: remote MCP server for Microsoft To Do.
# Built by Portainer from this repo (deploy/docker-compose.yml). Runs as uid 10001 with a
# read-only root filesystem; the only writable place is the /data volume (0700).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown 10001:10001 /data \
 && chmod 0700 /data

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && rm -rf /app/src /app/build

USER 10001:10001
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/healthz', timeout=4).status == 200 else 1)"]
CMD ["todo-mcp"]
