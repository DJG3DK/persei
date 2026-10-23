# The sandbox launcher: the only container in the bundle given the Docker
# socket (services/sandbox-launcher/launcher.py says what it will and will
# not start). The agent asks it for every sandbox container over HTTP, with
# a key only the two of them mount.
#
# The docker CLI and Python's standard library, nothing else: no pip
# install, no compose plugin, no daemon. The sandbox image's build context
# and the stack -> image map are baked in, so it can build the one and
# allow-list the other without the agent handing it either.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/sandbox-launcher/launcher.py ./launcher.py
COPY docker/stack-images.json ./docker/stack-images.json
COPY docker/agent-sandbox/ ./docker/agent-sandbox/

ENV PYTHONUNBUFFERED=1 \
    SANDBOX_LAUNCHER_PORT=8300 \
    SANDBOX_STACK_IMAGES=/app/docker/stack-images.json \
    SANDBOX_BUILD_CONTEXT=/app/docker/agent-sandbox

EXPOSE 8300
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8300/health', timeout=3)" || exit 1

CMD ["python", "launcher.py"]
