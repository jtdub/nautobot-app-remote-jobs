# remote-worker: async worker agent for nautobot-app-remote-jobs.
#
# The agent needs access to a container engine socket (Docker or Podman) to
# run job containers. The image runs as a non-root user; the OPERATOR must
# mount the socket and grant access to it, e.g.:
#
#   docker run -d \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     --group-add "$(stat -c %g /var/run/docker.sock)" \
#     -v remote-worker-state:/var/lib/remote-worker \
#     -e REMOTE_JOBS_URL=... -e REMOTE_JOBS_GATEWAY_URL=... \
#     -e REMOTE_JOBS_ENROLL_TOKEN=... \
#     remote-worker
#
# Job containers themselves NEVER receive the socket (SPEC 12.3).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --uid 1000 --user-group --create-home worker \
    && mkdir -p /var/lib/remote-worker \
    && chown worker:worker /var/lib/remote-worker

WORKDIR /opt/remote-worker
COPY pyproject.toml README.md ./
COPY remote_worker ./remote_worker
RUN pip install --no-cache-dir .

USER worker

# State volume: session secret + in-flight run journal.
VOLUME ["/var/lib/remote-worker"]

# /healthz endpoint (port tunable via REMOTE_WORKER_HEALTH_PORT).
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('REMOTE_WORKER_HEALTH_PORT','8080'), timeout=4)"

ENTRYPOINT ["remote-worker"]
