# ShadowDev in a Linux container, so it runs the same on Linux, macOS and Windows (Docker Desktop, or
# Docker in WSL2). The sandbox (bwrap) and prlimit are Linux tools: the container brings them along.
# docker-compose.yml runs it with what bwrap needs to create its sandbox; see deploy/README.md, Docker.
#
# Projects' setup and test commands run in this container too (in the sandbox), so it carries the
# toolchains they usually need: node and npm, and uv for Python. A project that needs more (Java, Go,
# a browser for its tests) gets a FROM shadowdev image of its own with those added.

FROM node:22.21.1-bookworm-slim AS node

FROM python:3.12.12-slim-bookworm

# git, the sandbox, prlimit (util-linux), what tools fetching over https need, and the time zones
# developers' working hours are in (tzdata).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git bubblewrap util-linux ca-certificates curl tini tzdata \
 && rm -rf /var/lib/apt/lists/*

# node and npm (outside $HOME, so the sandbox sees them read-only), the claude CLI, and uv.
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g --no-fund --no-audit @anthropic-ai/claude-code \
 && npm cache clean --force
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

# ShadowDev's one dependency, as uv.lock has it.
RUN pip install --no-cache-dir cryptography==50.0.2

# A user of its own, not root. HOST_UID/HOST_GID: the host user's, so files in the mounted data/ stay
# theirs (`make docker-up` passes them). A group with that ID may exist already (macOS's staff, 20).
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN uid=$([ "$HOST_UID" = 0 ] && echo 1000 || echo "$HOST_UID") gid=$([ "$HOST_GID" = 0 ] && echo 1000 || echo "$HOST_GID") \
 && (getent group "$gid" >/dev/null || groupadd --gid "$gid" shadowdev) \
 && useradd --uid "$uid" --gid "$gid" --create-home shadowdev

WORKDIR /app
# The code stays root's: the server can read it, not change it.
COPY . .
# data/ and worktrees/ are mounted over these; a new volume starts with their owner and mode.
RUN install -d -m 700 -o shadowdev data worktrees \
 && install -d -o shadowdev /home/shadowdev/.npm /home/shadowdev/.cache

USER shadowdev
ENV HOST=0.0.0.0 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
# tini reaps the processes runs leave behind and passes SIGTERM on: runs in progress finish first.
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "shadowdev"]
