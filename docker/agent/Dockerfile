# The agent, as one image.
#
# No docker here, CLI or socket. The sandbox containers every command runs in
# are started by the bundle's sandbox launcher (docker/sandbox-launcher), the
# one service holding the host daemon's socket: a socket in this container
# made a compromise of the agent process root on the host. The agent asks the
# launcher over HTTP (agent/tools/sandbox_launcher.py), and the launcher
# decides the image, the limits and what may be mounted.

FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

# git: the agent's own worktree/commit operations run in this container.
# ripgrep: the planner's repo search.
# curl + ca-certificates: healthcheck and TLS to the model provider.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ripgrep curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY scripts/ ./scripts/
COPY skills/ ./skills/
COPY memory/ ./memory/
COPY pytest.ini ruff.toml ./
# The stack -> image map. Without it every project's checks, the agent's own
# and the reviewer's it runs for them, fall back to the default image.
COPY docker/stack-images.json ./docker/stack-images.json
COPY --from=frontend /build/dist ./frontend/dist

COPY docker/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    API_PORT=8100 \
    # AGENT_PROJECT_ROOTS, not AGENT_PROJECTS_ROOT. provisioning.py reads the
    # first; the second was set here and read nowhere, so the allow-list fell
    # back to its /home default and onboarding REFUSED every path under
    # /projects -- the only place the bundle puts anything.
    AGENT_PROJECT_ROOTS=/projects

EXPOSE 8100
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=6 \
    CMD curl -fsS http://127.0.0.1:8100/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
