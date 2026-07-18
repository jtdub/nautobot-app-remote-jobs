# remote-worker

The worker agent for [nautobot-app-remote-jobs](../SPEC.md) (SPEC section 12).

A small, fully async Python 3.11+ service deployed per execution zone. It:

- connects **outbound** to the control-plane gateway over TLS WebSocket
  (`/ws/worker`) and speaks JSON-RPC 2.0 (one object per text frame, UUIDv4
  ids, no batches);
- authenticates with an HMAC-SHA256 challenge over its session secret — the
  secret itself never transits the wire;
- claims job offers, pulls **digest-pinned** OCI images, and runs them in
  hardened containers via a pluggable runtime (Docker/Podman socket via
  `aiodocker` in v1);
- streams container stdout/stderr and structured logs through a batching
  `LogSink` (HTTP by default, Kafka optional);
- heartbeats `job.status` every 25 s per running job (lease renewal +
  cancel polling), and reports `job.complete` with exit code and executed
  image digest;
- journals in-flight runs on the state volume so restarts re-attach to
  running containers and `worker.hello` can report `in_flight` run ids.

The agent never imports pynautobot and never touches the Nautobot ORM; its
entire API surface is the enroll/log endpoints and the WebSocket protocol.

## Installation

```bash
pip install .            # websockets + httpx + aiodocker
pip install ".[kafka]"   # optionally adds aiokafka for the Kafka log sink
```

Or build the container image:

```bash
docker build -t remote-worker .
```

## Configuration

Environment variables, plus an optional TOML file pointed to by
`REMOTE_WORKER_CONFIG`. **Environment variables override the TOML file.**

| Env var | TOML key | Default | Description |
| --- | --- | --- | --- |
| `REMOTE_JOBS_URL` | `nautobot_url` | — (required) | Nautobot base URL (enroll + HTTP log sink) |
| `REMOTE_JOBS_GATEWAY_URL` | `gateway_url` | — (required) | Gateway base URL; `/ws/worker` is appended |
| `REMOTE_JOBS_ENROLL_TOKEN` | `enroll_token` | — | Enrollment token, needed on **first boot only** |
| `REMOTE_WORKER_NAME` | `name` | hostname | Worker name registered at enrollment |
| `REMOTE_WORKER_CAPACITY` | `capacity` | `4` | Max concurrent runs |
| `REMOTE_WORKER_CAPABILITIES` | `capabilities` | `[]` | Comma-separated labels, e.g. `ssh-access` |
| `REMOTE_WORKER_PASS_ENV` | `pass_env` | `[]` | Allowlist of agent env vars passed into job containers |
| `REMOTE_WORKER_SECRET_MOUNTS` | `[[secret_mounts]]` | `[]` | `src:dst[,src:dst]`; always mounted read-only |
| `REMOTE_WORKER_STATE_DIR` | `state_dir` | `/var/lib/remote-worker` | State volume (session secret + run journal) |
| `REMOTE_WORKER_HEALTH_HOST`/`_PORT` | `health_host`/`health_port` | `0.0.0.0:8080` | `/healthz` bind |
| `REMOTE_WORKER_MEMORY_LIMIT` | `memory_limit` | none | Per-job-container memory limit (`512m`, `1g`, bytes) |
| `REMOTE_WORKER_CPU_LIMIT` | `cpu_limit` | none | Per-job-container CPU limit (float CPUs) |
| `REMOTE_WORKER_LOG_SINK` | `[log_sink]` | server-controlled | Local log sink override (`http`/`kafka`) |
| `REMOTE_WORKER_KAFKA_BOOTSTRAP_SERVERS` | `log_sink.bootstrap_servers` | — | Kafka bootstrap servers |
| `REMOTE_WORKER_REGISTRY_HOST`/`_USERNAME`/`_PASSWORD` | `[registry_auth."<host>"]` | — | Registry pull credentials |
| `REMOTE_WORKER_TLS_VERIFY` | `tls_verify` | `1` | Set `0` to skip TLS verification (labs only) |
| `REMOTE_WORKER_LOG_LEVEL` | — | `INFO` | Agent log level |
| `DOCKER_HOST` | — | engine default | Container engine socket URL |

Example TOML:

```toml
nautobot_url = "https://nautobot.example.com"
gateway_url = "https://remote-jobs-gw.example.com"
name = "dfw-worker-1"
capacity = 8
capabilities = ["ssh-access"]
pass_env = ["VAULT_ADDR", "VAULT_TOKEN"]
memory_limit = "1g"
cpu_limit = 2.0

[[secret_mounts]]
source = "/etc/remote-worker/secrets/tacacs"
target = "/run/secrets/tacacs"

[registry_auth."registry.example.com"]
username = "puller"
password = "..."
```

## Enrollment and identity (SPEC 7.1/7.2)

On first boot with no persisted state the agent POSTs
`{REMOTE_JOBS_URL}/api/plugins/remote-jobs/enroll/` with
`Authorization: Token <enroll token>` and body
`{name, capabilities, capacity, agent_version}`, then persists the returned
`{worker_id, session_secret}` to `<state_dir>/state.json` (mode `0600`) and
discards the enrollment token. Subsequent boots skip enrollment entirely.

Every WebSocket connection starts with the handshake frame:

```json
{"worker_id": "...", "timestamp": 1752796800, "nonce": "...", "signature": "hmac-sha256-hex"}
```

where `signature = HMAC-SHA256(session_secret, "{worker_id}:{timestamp}:{nonce}")`.
`worker.rotate` replaces the session secret over the established session.

## Run lifecycle (SPEC 12.2)

1. Offer received → journaled to `<state_dir>/journal/<run_id>.json`.
2. `image@sha256:<digest>` pulled — non-digest references are refused.
3. Container created with env = offer env + `NAUTOBOT_URL` + `NAUTOBOT_TOKEN`
   (scoped token) + allowlisted `pass_env` + `REMOTE_JOBS_RUN_ID` /
   `REMOTE_JOBS_ZONE` / `REMOTE_JOBS_DRYRUN`; secret mounts read-only.
4. stdout/stderr stream to the console sink; `job.status` every 25 s.
5. On exit → `job.complete` with exit code and executed image digest. Cancel
   (graceful) → SIGTERM, `grace_seconds`, SIGKILL → `state=TERMINATED`.
   Local timeout → SIGKILL → `state=FAILURE`, error `timeout`.
6. Container deleted, journal entry dropped after the server acks.

Container hardening defaults (SPEC 12.3): read-only rootfs with tmpfs
`/tmp`, `no-new-privileges`, default seccomp profile, non-root user
(`65534:65534`), memory/CPU limits from agent config, and the engine socket
is **never** mounted into job containers (mount attempts are rejected).

## Log sinks (SPEC 10)

Sink selection comes from `log_sink_config` in the `worker.hello` result;
a local override (`REMOTE_WORKER_LOG_SINK` / `[log_sink]`) wins.

- **HTTP (default)**: batches to
  `/api/plugins/remote-jobs/runs/{id}/logs/` and `.../console/`. Flush on
  2 s / 100 entries / 64 KiB, whichever first. Each batch carries a
  monotonically increasing `client_sequence` per run; delivery is
  at-least-once with retry (the server dedupes).
- **Kafka (optional)**: topics `remote-jobs.logs` / `remote-jobs.console`,
  partition key `run_id`. Requires `pip install "remote-worker[kafka]"`;
  without aiokafka installed, selecting it fails with a clear error.

## Restart behaviour

In-flight runs are journaled. After a restart the agent reports their ids as
`in_flight` in `worker.hello`, then re-attaches to containers that are still
running (containers are named `remote-job-<run_id>`), settles containers
that exited while the agent was down, and reports runs whose containers are
gone as `FAILURE`.

## Health

`GET /healthz` (default port 8080, `REMOTE_WORKER_HEALTH_PORT`) returns
`200` with `{"connected": ..., "in_flight": N, "draining": ..., ...}`.

## Running with Docker

```bash
docker run -d --name remote-worker \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v remote-worker-state:/var/lib/remote-worker \
  -e REMOTE_JOBS_URL=https://nautobot.example.com \
  -e REMOTE_JOBS_GATEWAY_URL=https://remote-jobs-gw.example.com \
  -e REMOTE_JOBS_ENROLL_TOKEN=<one-time-token> \
  -p 8080:8080 \
  remote-worker
```

The image runs as a non-root user; access to the engine socket must be
granted by the operator (the `--group-add` above, or a socket proxy).

## Development

```bash
pip install -e ".[dev]"
pytest            # unit tests: no network, no Docker required
```
