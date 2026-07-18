# remote-jobs-gateway: stateless WS <-> Redis bridge for nautobot-app-remote-jobs.
FROM python:3.11-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY remote_jobs_gateway ./remote_jobs_gateway
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

LABEL org.opencontainers.image.title="remote-jobs-gateway" \
      org.opencontainers.image.description="Stateless WebSocket gateway for nautobot-app-remote-jobs" \
      org.opencontainers.image.source="https://github.com/nautobot/nautobot-app-remote-jobs"

# Non-root runtime user; the gateway needs no filesystem state.
RUN useradd --system --no-create-home --uid 10001 gateway

COPY --from=build /install /usr/local

ENV PYTHONUNBUFFERED=1 \
    GATEWAY_BIND=0.0.0.0:8001

USER 10001
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import urllib.request,os;h,_,p=os.environ.get('GATEWAY_BIND','0.0.0.0:8001').rpartition(':');urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=4)"]

ENTRYPOINT ["remote-jobs-gateway"]
