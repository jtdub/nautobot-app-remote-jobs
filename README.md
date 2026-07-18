# nautobot-app-remote-jobs

<p align="center">
  <img src="https://raw.githubusercontent.com/jtdub/nautobot-app-remote-jobs/develop/docs/images/icon-remote-jobs.png" class="logo" height="200px">
  <br>
  <a href="https://github.com/jtdub/nautobot-app-remote-jobs/actions"><img src="https://github.com/jtdub/nautobot-app-remote-jobs/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://pypi.org/project/nautobot-remote-jobs/"><img src="https://img.shields.io/pypi/v/nautobot-remote-jobs"></a>
  <br>
  An <a href="https://networktocode.com/nautobot-apps/">App</a> for <a href="https://nautobot.com/">Nautobot</a>.
</p>

## Overview

Nautobot's built-in Jobs execute inside the Nautobot deployment with direct ORM access — bypassing object permissions, granting every job database-superuser-equivalent access, and forcing worker deployments to be complete Nautobot installations. This project introduces a second job execution system in which:

- **Job code runs in isolated containers on remote workers**, never inside Nautobot.
- **Workers interact with Nautobot exclusively through the REST API** using short-lived tokens scoped to the launching user. No ORM, no DB credentials, no Nautobot codebase on the worker. Remote jobs run with the launcher's ObjectPermissions.
- **Workers deploy into named execution zones.** Devices map to zones through configurable membership rules (locations, roles, prefixes, dynamic groups). Jobs targeting a device automatically execute on a worker in that device's zone, with fan-out, failover, and wait policies.
- **Workers connect outbound** to a control-plane gateway over TLS WebSocket and speak JSON-RPC 2.0. Nautobot never initiates connections to workers.
- **Results, logs, and console output land in core `JobResult` / `JobLogEntry` / `JobConsoleEntry` models**, so the existing Job Results UI, saved views, filters, and cancel button work unchanged.

The complete design is in [SPEC.md](https://github.com/jtdub/nautobot-app-remote-jobs/blob/main/SPEC.md). Target: Nautobot >= 3.2.

## Repository layout (mono-repo)

Four distributable artifacts, one repo:

| Path | Artifact | Description |
| --- | --- | --- |
| `nautobot_remote_jobs/` (+ root `pyproject.toml`) | `nautobot-remote-jobs` | The Nautobot app: models, UI, REST API, dispatch, scheduler, cancel strategy, RPC bridge consumer, Kafka log consumer |
| `gateway/` | `remote-jobs-gateway` | Stateless FastAPI WebSocket gateway bridging worker JSON-RPC to Redis pub/sub. No DB access |
| `worker/` | `remote-worker` | Async worker agent: enrollment, claim loop, Docker container runtime with hardening defaults, log sinks |
| `sdk/` | `nautobot-remote-jobs-sdk` | The library job code imports (`ctx.api`, `ctx.logger`, `ctx.secrets`, ...) plus the `remote-jobs publish` CLI |
| `deploy/` | — | docker compose and Helm deployment examples |
| `development/` | — | Nautobot development environment (invoke + docker compose), pinned to 3.2 |

## Quick tour

1. **Define a job**: build an OCI image with your job code (using the SDK), describe it in `remote-job.yaml`, and publish it:

   ```bash
   remote-jobs publish --image registry.example.com/jobs/rotate-admin:1.4.0 \
       --url https://nautobot.example.com --token $NAUTOBOT_TOKEN
   ```

2. **Create an execution zone** and membership rules in the Nautobot UI (Remote Jobs → Execution Zones), then create a worker enrollment token (Remote Jobs → Enrollment Tokens; plaintext shown once).

3. **Deploy a worker** in the zone:

   ```bash
   docker run -d -v /var/run/docker.sock:/var/run/docker.sock \
       -v remote-worker-state:/var/lib/remote-worker \
       -e REMOTE_JOBS_URL=https://nautobot.example.com \
       -e REMOTE_JOBS_GATEWAY_URL=wss://remote-jobs-gw.example.com/ws/worker \
       -e REMOTE_JOBS_ENROLL_TOKEN=<token> \
       remote-worker:latest
   ```

4. **Run it**: Remote Jobs → Job Definitions → Run. The form renders from the job's JSON Schema; results, logs, and live console stream into the core Job Result view.

## Installation (app)

```bash
pip install nautobot-remote-jobs
```

```python
# nautobot_config.py
PLUGINS = ["nautobot_remote_jobs"]
PLUGINS_CONFIG = {
    "nautobot_remote_jobs": {
        "nautobot_url": "https://nautobot.example.com",   # advertised to workers
        "gateway_internal_token": "<shared secret with the gateway>",
        # "worker_ttl_seconds": 90,
        # "lease_seconds": 120,
        # "log_sink_config": {"type": "http"},
    }
}
```

Then run migrations and start the RPC bridge consumer alongside your web/worker processes:

```bash
nautobot-server migrate
nautobot-server remote_jobs_rpc_consumer
```

See `deploy/` for full compose and Helm examples, and `docs/` for the complete documentation set.

## Development

The `development/` environment is the standard Nautobot app dev stack:

```bash
poetry install
poetry shell
invoke build && invoke start   # Nautobot 3.2 + Postgres + Redis
invoke unittest                # app test suite
```

Component test suites run independently:

```bash
(cd gateway && python -m pytest)
(cd worker && python -m pytest)
(cd sdk && python -m pytest)
```

## License

Apache-2.0. See [LICENSE](https://github.com/jtdub/nautobot-app-remote-jobs/blob/main/LICENSE).
