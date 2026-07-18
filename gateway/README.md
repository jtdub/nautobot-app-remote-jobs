# remote-jobs-gateway

Stateless FastAPI/Starlette ASGI service for
[nautobot-app-remote-jobs](../README.md). It terminates worker TLS WebSockets
and bridges JSON-RPC 2.0 frames to Redis pub/sub (SPEC sections 7.2, 8, 8.4).

The gateway holds **no business logic, no database access, and no secrets**
other than the shared internal token used to call the Nautobot app. All
dispatch state lives in Postgres (owned by the app), so a gateway restart only
causes worker reconnects. Scale it horizontally behind any TCP/WS load
balancer.

## Endpoints

| Path | Purpose |
| --- | --- |
| `WS /ws/worker` | Worker WebSocket: HMAC handshake, then JSON-RPC bridging |
| `GET /healthz` | Health check (verifies Redis connectivity; 503 when down) |
| `GET /metrics` | Prometheus metrics |

## Handshake

The worker's first frame after connecting:

```json
{"worker_id": "…", "timestamp": 1700000000, "nonce": "…", "signature": "…"}
```

`signature = hex(HMAC-SHA256(key=session_secret, msg="{worker_id}:{timestamp}:{nonce}"))`.
The session secret never transits the wire. The gateway:

1. checks the timestamp against its allowed clock skew,
2. claims the nonce in Redis with `SET NX EX` (replay protection),
3. calls `POST {GATEWAY_APP_URL}/api/plugins/remote-jobs/internal/verify-session/`
   with `Authorization: Token $GATEWAY_INTERNAL_TOKEN`; the app validates the
   HMAC and answers `{"valid": true, "worker_id": …, "zone_id": …}`.

On success the gateway replies
`{"authenticated": true, "worker_id": …, "zone_id": …}` and starts bridging.
Failures close the socket with code `4400` (bad handshake) or `4401`
(authentication failed).

## Bridging (SPEC 8.4)

| Direction | Transport |
| --- | --- |
| worker → server requests (frames with `id`) | published to `remote-jobs:gateway:rpc` as `{"worker_id", "zone_id", "frame"}`; the app's reply is awaited on `remote-jobs:worker:{worker_id}:rsp:{request_id}` and forwarded to the WS |
| worker → server notifications, and worker replies to server RPCs | published to `remote-jobs:gateway:rpc` fire-and-forget |
| server → worker targeted RPCs (`job.cancel`, `worker.drain`, `worker.ping`, …) | subscribed from `remote-jobs:worker:{worker_id}:cmd`, forwarded verbatim |
| zone fan-out (`job.available`) | subscribed from `remote-jobs:zone:{zone_id}:notify`, forwarded verbatim |

Limits: per-worker token-bucket rate limit (default 30 req/s) and a 256 KiB
max frame size. Violations get JSON-RPC error responses: `-32010`
rate-limited, `-32011` frame too large, `-32012` upstream timeout (the app
error range `-32001`…`-32006` from SPEC 8.3 is reserved for the app).
WebSocket protocol ping/pong keepalive is handled by uvicorn; application
level `worker.ping` frames bridge like any other server → worker RPC.

## Configuration

Environment variables (prefix `GATEWAY_`, via pydantic-settings):

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_REDIS_URL` | `redis://localhost:6379/0` | Redis for pub/sub + nonces |
| `GATEWAY_APP_URL` | `http://localhost:8080` | Nautobot base URL |
| `GATEWAY_INTERNAL_TOKEN` | *(empty)* | Shared token for the internal verify-session API |
| `GATEWAY_BIND` | `0.0.0.0:8001` | `host:port` to listen on |
| `GATEWAY_RATE_LIMIT_RPS` | `30` | Per-worker inbound frames/second |
| `GATEWAY_RATE_LIMIT_BURST` | `30` | Token-bucket burst |
| `GATEWAY_MAX_FRAME_BYTES` | `262144` | Max WS frame size (256 KiB) |
| `GATEWAY_RPC_RESPONSE_TIMEOUT_SECONDS` | `30` | Wait for the app's RPC answer |
| `GATEWAY_HANDSHAKE_TIMEOUT_SECONDS` | `10` | Wait for the handshake frame |
| `GATEWAY_TIMESTAMP_SKEW_SECONDS` | `300` | Allowed handshake clock skew |
| `GATEWAY_NONCE_TTL_SECONDS` | `600` | Nonce replay-protection TTL |
| `GATEWAY_WS_PING_INTERVAL_SECONDS` | `20` | WS protocol ping interval |
| `GATEWAY_WS_PING_TIMEOUT_SECONDS` | `20` | WS protocol pong grace |
| `GATEWAY_LOG_LEVEL` | `INFO` | Logging level |

## Metrics

`remote_jobs_gateway_connections` (gauge),
`remote_jobs_gateway_rpc_frames_total{method,direction}` (counter),
`remote_jobs_gateway_rpc_latency_seconds{method}` (histogram, publish→response
round trip), `remote_jobs_gateway_frame_rejections_total{reason}`,
`remote_jobs_gateway_handshakes_total{result}`.

## Running

```bash
pip install .
GATEWAY_REDIS_URL=redis://redis:6379/0 \
GATEWAY_APP_URL=https://nautobot.example.com \
GATEWAY_INTERNAL_TOKEN=… \
remote-jobs-gateway
```

Or with Docker:

```bash
docker build -t remote-jobs-gateway .
docker run --rm -p 8001:8001 \
  -e GATEWAY_REDIS_URL=redis://redis:6379/0 \
  -e GATEWAY_APP_URL=https://nautobot.example.com \
  -e GATEWAY_INTERNAL_TOKEN=… \
  remote-jobs-gateway
```

## Development

```bash
pip install -e .[dev]
python3 -m pytest
```

Tests use in-memory stubs — no live Redis or Nautobot required.
