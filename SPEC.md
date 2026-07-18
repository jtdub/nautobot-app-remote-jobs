# nautobot-app-remote-jobs

Implementation specification. Target: Nautobot >= 3.2 (developed against the `next` branch, v3.2.0b2 as of 2026-07-17).

## 1. Purpose

Nautobot's built-in Jobs execute inside the Nautobot deployment with direct ORM access. This bypasses object permissions, grants every job the equivalent of database superuser access, and forces worker deployments to be complete Nautobot installations (settings, DB credentials, Redis, the full dependency tree). The Kubernetes job queue type introduced in core does not change this: the launched pod runs `nautobot-server runjob_with_job_result` and is therefore a full Nautobot install with ORM and DB access.

This app introduces a second job execution system in which:

- Job code runs in isolated containers on remote workers, never inside Nautobot.
- Workers interact with Nautobot exclusively through the REST API using short-lived tokens scoped to the launching user. No ORM, no DB credentials, no Nautobot codebase on the worker.
- Workers deploy into named execution zones. Devices map to zones through configurable membership rules. Jobs targeting a device automatically execute on a worker in that device's zone.
- Workers connect outbound to a control plane gateway over TLS WebSocket and speak JSON-RPC 2.0. Nautobot never initiates connections to workers.
- Results, logs, and console output land in core `JobResult`, `JobLogEntry`, and `JobConsoleEntry` models so the existing Job Results UI, saved views, filters, and cancel button work unchanged.

### Non-goals (v1)

- Replacing or modifying core Jobs. Both systems coexist.
- Job Buttons and Job Hooks integration (phase 4+).
- Windows worker support.
- A server-side secrets relay. Secret values never transit the control plane.

## 2. Verified constraints from the Nautobot codebase

These facts were verified against `nautobot/nautobot@next` (77a9920, 2026-07-17) and drive design decisions below. Re-verify against 3.2 GA before release.

| Fact | Location | Design consequence |
| --- | --- | --- |
| `JobQueueTypeChoices` is a closed ChoiceSet (`celery`, `kubernetes`); `JobResult.enqueue_job` hard-branches on queue type (`# TODO: make this branch aware!`) | `extras/choices.py`, `extras/models/jobs.py` | Do not piggyback core Job/JobQueue dispatch. Own the definition and dispatch models. Consider upstreaming a queue-type registration hook separately. |
| `JobResultViewSet` has no Create/Update mixins; `JobLogEntryViewSet` is read-only | `extras/api/views.py` | Workers cannot write results via the core API. The app ships its own worker-facing ingestion endpoints that write core models server-side via ORM. |
| `JobResult.job_model` is `null=True, on_delete=SET_NULL`; `JobResult.worker` is a free CharField; `name` is independent | `extras/models/jobs.py` | Core `JobResult` can represent externally executed runs. Reuse it instead of a parallel results model. |
| `JobConsoleEntry` (3.1) stores per-result console output lines with stdout/stderr types, designed for tail-by-polling | `extras/models/jobs.py` | Worker stdout/stderr batches write here; the core live console works for remote jobs. |
| 3.2 cancel: `CancelFactory.strategies` is a plain class-attribute dict keyed by queue type with `UnknownStrategy` (reap-only) fallback; `JobResult` gained `revoked_by`, `terminated_at`, `revocation_type`, `TYPE_ABANDONED` | `extras/jobs_cancel.py`, release notes | Register a `remote` strategy in `CancelFactory` at app `ready()`. The core Cancel button then works for remote runs. Mirror the terminate/reap/abandoned taxonomy exactly. |
| Approvals are a generic multi-stage framework; `Job.approval_required` was removed in 3.0; `ScheduledJob` uses `ApprovableModelMixin` | `extras/models/approvals.py`, 3.0 release notes | Adopt `ApprovableModelMixin` on the run-request and schedule models. Build zero approval logic. |
| `Secret` stores only `provider` slug + `parameters` JSON; values resolve server-side via `registry["secrets_providers"]`; the API exposes definitions and a boolean `/check/`, never values; `rendered_parameters(obj)` renders parameters as Jinja2 with an object context | `extras/models/secrets.py`, `extras/secrets/providers.py`, `extras/api/views.py` | Workers resolve secret values locally using the same provider slugs and parameters fetched via API. See section 9. |
| The events framework (`nautobot.core.events`) is publish-only (Redis, syslog brokers). Nautobot cannot consume Kafka | `core/events/` | Kafka log ingestion requires a consumer process shipped by this app as a management command. See section 10. |
| REST API excludes M2M fields by default in 3.0 (`exclude_m2m`) | 3.0 release notes | The SDK sets `exclude_m2m=False` as its pynautobot default and documents the behavior. |
| 3.x UI is Bootstrap 5 + HTMX with generic templates; legacy templates removed | 3.0/3.1 release notes | All app views extend `generic/object_retrieve.html` and friends. |
| Celery worker count display uses live `celery inspect` | `extras/models/jobs.py` (`JobQueue.display`) | The heartbeat-TTL Worker model replaces this pattern for remote workers and surfaces equivalent counts in zone displays. |

## 3. Architecture

```
                        Nautobot deployment (trusted control plane)
  +---------------------------------------------------------------------+
  |  nautobot-app-remote-jobs (Django app)                              |
  |    models / UI / REST API / dispatch / scheduler beat task          |
  |    CancelFactory strategy / log HTTP ingestion / token minting      |
  |                          ^            ^                             |
  |        ORM writes to JobResult / JobLogEntry / JobConsoleEntry      |
  +--------------|-----------------------|------------------------------+
                 | Redis pub/sub         | ORM (optional Kafka consumer
                 v                       |  management command process)
  +-----------------------------+        |
  |  WS gateway (stateless      |        |
  |  ASGI service, no DB)       |   +----+-----------------+
  +-------------^---------------+   | Kafka (optional log  |
                | outbound TLS WS   | transport)           |
                | JSON-RPC 2.0      +----------^-----------+
  +-------------|------------------------------|---------- execution zone
  |  +----------+-----------+                  |
  |  |  worker agent        |------------------+
  |  |  (async Python)      |
  |  +----------+-----------+
  |             | runs
  |  +----------v-----------+     scoped token, REST/GraphQL only
  |  |  job container       | --------------------------------------> Nautobot API
  |  |  (SDK + job code)    | ---> devices in zone (SSH/NETCONF/etc.)
  |  +----------------------+
  +---------------------------------------------------------------------+
```

Components:

1. **Nautobot app** (`nautobot_remote_jobs`): models, UI, worker-facing REST endpoints, dispatch logic, per-job token minting, Celery beat scheduler task, cancel strategy, HTTP log ingestion, optional Kafka consumer management command.
2. **Gateway** (`remote-jobs-gateway`): a stateless FastAPI/Starlette ASGI service terminating worker WebSockets. Holds no business logic and no DB access. Authenticates sessions against the app, then bridges JSON-RPC frames to Redis pub/sub. Horizontally scalable; a gateway restart only causes worker reconnects because all dispatch state lives in Postgres.
3. **Worker agent** (`remote-worker`): a small async Python service deployed per zone. Connects outbound to the gateway, claims work, pulls and runs job container images, injects tokens and env, streams logs, reports results. Ships as a single container image.
4. **Job SDK** (`nautobot-remote-jobs-sdk`): the library job code imports. Wraps pynautobot, logging, secrets resolution, typed inputs, dryrun.
5. **Job containers**: one OCI image per job (or job bundle), referenced by digest.

Trust boundaries: the app and the Kafka consumer are trusted (ORM access). The gateway is semi-trusted (sees control messages and tokens in transit, no DB). Workers and job containers are untrusted (API-only, scoped tokens, zone-local network access).

## 4. Data models

All models live in `nautobot_remote_jobs.models`. Field types reference Nautobot conventions (`PrimaryModel`, `OrganizationalModel`, `BaseModel`, `CHARFIELD_MAX_LENGTH`).

### 4.1 `JobDefinition(PrimaryModel)`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | CharField, unique | |
| `description` | CharField, blank | |
| `enabled` | BooleanField, default False | Disabled definitions are not runnable, matching core Job semantics |
| `image` | CharField | OCI reference without digest, e.g. `registry.example.com/jobs/rotate-admin:1.4.0` |
| `image_digest` | CharField | `sha256:...`. Dispatch always sends `image@digest`. Updating the definition updates the digest |
| `input_schema` | JSONField | JSON Schema (draft 2020-12) describing job inputs. Drives dynamic UI form rendering and server-side validation |
| `zone_policy` | CharField, choices | `pinned` (always `default_zone`), `any` (any zone with capacity), `per_device` (resolve zone from each target device), `fan_out` (decompose multi-zone targets into child runs) |
| `default_zone` | FK ExecutionZone, null | Required when `zone_policy=pinned` |
| `secrets_groups` | M2M `extras.SecretsGroup` | Groups the job may resolve. Drives token permission constraints |
| `capabilities` | JSONField, default list | Labels a worker must advertise to claim, e.g. `["ssh-access"]` |
| `timeout_seconds` | PositiveIntegerField, default 1800 | Hard wall clock limit |
| `grace_seconds` | PositiveIntegerField, default 30 | SIGTERM to SIGKILL window on cancel/timeout |
| `retry_max` | PositiveSmallIntegerField, default 0 | Automatic requeue count for `ABANDONED` runs only. Never auto-retry `FAILURE` |
| `singleton` | BooleanField, default False | At most one non-terminal run of this definition at a time |
| `dryrun_supported` | BooleanField, default False | |
| `requires_zone_local` | BooleanField, default True | When True, failover to another zone is refused |
| `notes`, `tags` | inherited | |

Validation: `image_digest` matches `sha256:[0-9a-f]{64}`; `input_schema` must be a valid JSON Schema; `zone_policy=pinned` requires `default_zone`.

### 4.2 `ExecutionZone(PrimaryModel)`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | CharField, unique | |
| `description` | CharField, blank | |
| `enabled` | BooleanField, default True | |
| `priority` | PositiveIntegerField, default 100 | Lower wins when a device matches multiple zones. Ties broken by name for determinism |
| `failover_policy` | CharField, choices | `none` (fail fast), `wait` (queue up to `max_wait_seconds`), `failover` (try `failover_zones` in order) |
| `max_wait_seconds` | PositiveIntegerField, default 300 | Used by `wait` and as per-hop wait for `failover` |
| `failover_zones` | M2M self through `ZoneFailover(order)` | Ordered |

Display shows live worker count (`N online / M total`) computed from heartbeat TTL.

### 4.3 `ZoneMembershipRule(BaseModel)`

Multiple rules per zone are OR-ed. Criteria within a rule are AND-ed. Empty criterion = wildcard for that dimension, but at least one criterion or `dynamic_group` must be set.

| Field | Type | Notes |
| --- | --- | --- |
| `zone` | FK ExecutionZone, related_name `membership_rules` | |
| `locations` | M2M `dcim.Location` | |
| `include_descendant_locations` | BooleanField, default True | Location is a tree |
| `roles` | M2M `extras.Role` | Device roles |
| `prefixes` | M2M `ipam.Prefix` | Matched by device primary IP containment (v4 or v6) |
| `dynamic_group` | FK `extras.DynamicGroup`, null | Escape hatch for arbitrary combinations |
| `weight` | PositiveIntegerField, default 100 | Rule evaluation order within the zone (cosmetic; rules are OR-ed) |

Resolver contract: `resolve_zone(device) -> ExecutionZone | None`. Implementation evaluates all enabled zones' rules, collects matches, returns the lowest `priority` zone. Must be cheap: cache rule sets, evaluate prefix containment in SQL where possible. Ship a **Zone Coverage report view** listing (a) devices matching no zone, (b) devices matching multiple zones with the resolved winner, (c) zones with zero online workers.

### 4.4 `Worker(PrimaryModel)`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | CharField, unique | Set at enrollment |
| `zone` | FK ExecutionZone | |
| `enabled` | BooleanField, default True | Admin kill switch. Disabled workers cannot claim |
| `draining` | BooleanField, default False | Set by `worker.drain`; finishes in-flight, claims nothing |
| `capabilities` | JSONField, default list | |
| `capacity` | PositiveSmallIntegerField, default 4 | Max concurrent runs |
| `agent_version` | CharField, blank | |
| `identity_fingerprint` | CharField | SHA-256 of the worker's session credential (see 7) |
| `last_seen` | DateTimeField, null | Updated on hello, claim, status, and gateway ping relay (throttled to >= 15s between writes) |

`status` property: `ONLINE` if `last_seen` within `REMOTE_JOBS_WORKER_TTL` (default 90s) and enabled and not draining; `DRAINING`; `OFFLINE` otherwise.

### 4.5 `WorkerEnrollmentToken(BaseModel)`

Single-purpose bootstrap credential. Grants nothing except the enroll exchange.

| Field | Type | Notes |
| --- | --- | --- |
| `token_hash` | CharField | SHA-256 of the token. Plaintext shown once at creation, never stored |
| `zone` | FK ExecutionZone | Enrollment is zone-bound |
| `expires` | DateTimeField | Default now + 24h |
| `single_use` | BooleanField, default True | |
| `used_at` | DateTimeField, null | |
| `worker` | FK Worker, null | Set on use |
| `created_by` | FK user | Audit |

### 4.6 `RemoteJobRun(PrimaryModel)` with `ApprovableModelMixin`

One row per execution attempt unit. For `fan_out`, a parent run aggregates child runs (one per zone or per device, see 6.4).

| Field | Type | Notes |
| --- | --- | --- |
| `job_definition` | FK JobDefinition, PROTECT | |
| `job_result` | OneToOne `extras.JobResult`, PROTECT | Created at submission. `job_model=None`, `name=f"[remote] {definition.name}"`, `user=launcher`, `worker=worker.name` once claimed |
| `parent` | FK self, null, related_name `children` | |
| `zone` | FK ExecutionZone, null | Resolved zone. Null until resolution for `per_device` |
| `worker` | FK Worker, null, SET_NULL | Set at claim |
| `device` | FK `dcim.Device`, null | For per-device child runs |
| `state` | CharField, choices | See state machine (6) |
| `inputs` | JSONField | Validated against `input_schema`. Redacted keys (schema `writeOnly: true`) stored as `"__redacted__"` |
| `dryrun` | BooleanField, default False | |
| `lease_expires_at` | DateTimeField, null | |
| `attempt` | PositiveSmallIntegerField, default 1 | |
| `image_digest_executed` | CharField, blank | Reported by the worker in `job.complete`. Provenance |
| `scoped_token` | FK `users.Token`, null, SET_NULL | Deleted on terminal state |
| `queued_at`, `offered_at`, `claimed_at`, `started_at`, `finished_at` | DateTimeField, null | |

Sync rule: `RemoteJobRun.state` transitions mirror into `job_result.status` (mapping in 6.3) within the same transaction. `JobResult` is the user-facing record; `RemoteJobRun` is the dispatch record.

### 4.7 `RemoteJobSchedule(PrimaryModel)` with `ApprovableModelMixin`

| Field | Type | Notes |
| --- | --- | --- |
| `job_definition` | FK JobDefinition | |
| `name` | CharField, unique | |
| `enabled` | BooleanField, default True | |
| `interval` | CharField, choices | `once`, `hourly`, `daily`, `weekly`, `custom` |
| `crontab` | CharField, blank | Required for `custom`. Standard 5-field cron |
| `start_time` | DateTimeField | |
| `user` | FK user, PROTECT | Runs execute as this user (token scoping) |
| `inputs` | JSONField | |
| `last_run_at` | DateTimeField, null | |

Executed by an app-provided Celery beat periodic task (`remote_jobs.dispatch_scheduled`, every 60s) running in the existing Nautobot Celery infrastructure. The beat task only enqueues `RemoteJobRun` rows; it never talks to workers.

### 4.8 Log and artifact models

Logs and console output go to core `JobLogEntry` and `JobConsoleEntry`. One app model supplements them:

`RunArtifact(BaseModel)`: `run` FK, `name`, `content_type`, `size_bytes`, `storage_path`, `sha256`. Artifacts upload via presigned URL to the configured Django storage backend (3.1 unified `STORAGES`); the ingestion endpoint records the row after upload completes.

## 5. Worker-facing REST API

Namespace: `/api/plugins/remote-jobs/`. Two authentication classes:

- **Enrollment auth**: the plaintext enrollment token in `Authorization: Token ...`, valid only for `POST /enroll/`.
- **Session auth**: the worker session credential (7.2), valid for the worker endpoints. All handlers enforce that a worker may only touch runs it has claimed and may not skip state transitions.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/enroll/` | Exchange enrollment token for worker identity + session credential. Body: `name`, `capabilities`, `capacity`, `agent_version`. Creates `Worker` |
| POST | `/runs/{id}/logs/` | Batched structured log entries -> `JobLogEntry`. Body: `[{level, message, grouping, timestamp}]`. Server enforces max 500 entries or 256 KiB per batch |
| POST | `/runs/{id}/console/` | Batched console output -> `JobConsoleEntry`. Body: `[{output_type, text, timestamp}]` |
| POST | `/runs/{id}/artifacts/` | Request presigned upload URL: `{name, content_type, size_bytes}` -> `{upload_url, artifact_id}`. Follow-up `PUT /runs/{id}/artifacts/{artifact_id}/complete/` with `sha256` |
| GET | `/workers/self/` | Worker's own record (zone, draining flag, config) |

Everything else (claim, status, complete, cancel, offers, token delivery) flows over JSON-RPC. The REST log endpoints exist so the default deployment needs no Kafka; the agent treats them as one `LogSink` implementation (10).

The human-facing REST API (standard `NautobotModelViewSet` CRUD for all models in section 4, plus `POST /job-definitions/{id}/run/`) follows normal Nautobot API conventions and ObjectPermissions, and is not detailed further here.

## 6. Dispatch: leases and the state machine

### 6.1 States

```
PENDING --> OFFERED --> CLAIMED --> RUNNING --> SUCCESS
   |            |           |           |------> FAILURE
   |            |           |           |------> TERMINATED   (cancel, graceful or killed)
   |            |           |           '------> ABANDONED    (lease expired, worker gone)
   |            |           '---------> PENDING (claim not confirmed in time)
   |            '---------> PENDING     (offer not claimed in time)
   |----> CANCELLED    (cancelled before any worker involvement)
   '----> FAILED_DISPATCH (no zone resolved / no capacity and policy=none / wait timeout)
```

`ABANDONED` runs with `attempt < retry_max + 1` re-enter `PENDING` with `attempt += 1`. `FAILURE` never auto-retries.

### 6.2 Claim protocol

Dispatch state lives in Postgres. The claimable-work query uses `SELECT ... FOR UPDATE SKIP LOCKED` on `RemoteJobRun` filtered by `state=PENDING`, zone, and required capabilities, ordered by `queued_at`. Two paths trigger claims:

1. **Push**: on enqueue, the app publishes a `work-available` notification on the zone's Redis channel; the gateway relays a lightweight `job.available` JSON-RPC notification to idle workers in that zone, which respond with `job.claim`.
2. **Pull**: workers with free capacity send `job.claim` on connect and after each completion.

A successful claim, in one transaction: lock the row, set `state=CLAIMED`, `worker`, `claimed_at`, `lease_expires_at = now + REMOTE_JOBS_LEASE_SECONDS` (default 120s), mint the scoped token (7.3), and return the full job offer as the RPC result. Lease renewal piggybacks on `job.status` (every <= 30s from a running job). A reaper beat task (`remote_jobs.reap_expired`, every 30s) moves expired `CLAIMED`/`RUNNING` runs to `ABANDONED`, marks the JobResult revoked with `revocation_type=ABANDONED`, deletes the scoped token, and applies retry policy.

Singleton: enforced at claim time with a `select_for_update` check for existing non-terminal runs of the definition; violation returns a structured RPC error and the run stays `PENDING` behind the running one.

### 6.3 JobResult mapping

| RemoteJobRun.state | JobResult.status | Extra fields |
| --- | --- | --- |
| PENDING / OFFERED | PENDING | |
| CLAIMED | PENDING | `worker` set |
| RUNNING | STARTED | `date_started` |
| SUCCESS | SUCCESS | `date_done` |
| FAILURE | FAILURE | `date_done` |
| TERMINATED | REVOKED per 3.2 semantics | `revoked_by`, `terminated_at`, `revocation_type=TERMINATED` |
| ABANDONED | REVOKED | `revocation_type=ABANDONED` |
| CANCELLED / FAILED_DISPATCH | FAILURE with explanatory log | |

Re-verify exact 3.2 status/revocation field semantics against GA before implementing; the taxonomy above matches `JobRevocationTypeChoices` on `next` today.

### 6.4 Zone resolution and fan-out

At submission, resolve per `zone_policy`:

- `pinned`: `default_zone`.
- `any`: cheapest zone with an online worker having free capacity; else apply the *definition's default zone's* failover policy semantics (`none`/`wait`).
- `per_device`: inputs must include a device-bearing field (schema annotation `x-remote-jobs-target: device`). One child run per device, each in `resolve_zone(device)`; parent run aggregates.
- `fan_out`: like `per_device` but grouped: one child run per distinct zone, carrying that zone's device subset in inputs.

Parent run state derives from children: `SUCCESS` iff all children succeed; `FAILURE` if any child fails; terminal only when all children are terminal. Parent has its own `JobResult` whose log receives per-child summary lines.

Failover: when a resolved zone has no online capacity, apply that zone's `failover_policy`. `failover` walks `failover_zones` in order, skipping the hop entirely when `requires_zone_local=True` on the definition (fail with an explicit log line instead).

## 7. Identity, enrollment, and tokens

### 7.1 Enrollment flow

1. Operator creates a `WorkerEnrollmentToken` in the UI for a zone; plaintext shown once.
2. Agent starts with `REMOTE_JOBS_ENROLL_TOKEN` + `REMOTE_JOBS_URL`, calls `POST /enroll/`.
3. App validates hash, expiry, single-use; creates `Worker`; returns `{worker_id, session_secret}`. `identity_fingerprint = sha256(session_secret)`. The agent persists `session_secret` in its state volume and discards the enrollment token.
4. Optional hardening (phase 4): return a client certificate instead and require mTLS at the gateway.

### 7.2 Session identity

The WebSocket connect handshake carries `worker_id` + an HMAC challenge-response over `session_secret` (never the secret itself). The gateway validates via one internal app call, then tags the Redis bridge channels with the worker identity. Session credentials rotate via a `worker.rotate` RPC (new secret delivered over the established session, old one invalidated after ack).

### 7.3 Per-job scoped tokens

Minted at claim, delivered only inside the `job.claim` RPC result over TLS:

- A `users.Token` owned by the **launching user** (`write_enabled=True`, `expires = now + timeout_seconds + grace_seconds + 60`).
- Because token auth acts as the user, all reads and writes the job performs are constrained by the launcher's ObjectPermissions, exactly like the permission model users already understand. Document clearly: *remote jobs run with the launcher's permissions*, which is the fix for the ORM bypass.
- Deleted by the app on any terminal state and by the reaper. Never written to Kafka, logs, K8s manifests, or the DB in plaintext (the FK stores the token object; Nautobot stores token keys, which is acceptable as it is core behavior, and rows are short-lived).
- v2 consideration: an app middleware that further constrains a job token to an allowlist of endpoints/objects (e.g. only the referenced SecretsGroups). Out of scope for v1.

### 7.4 Threat model summary

| Asset | Protection |
| --- | --- |
| Stolen enrollment token | Grants only a pending worker registration in a specific zone, visible in UI; single-use + 24h expiry |
| Stolen session secret | Can claim work for that zone. Mitigations: fingerprint pinning, `enabled=False` kill switch, rotate RPC, audit of claims per worker |
| Stolen per-job token | Bounded by launcher permissions and minutes-scale TTL |
| Compromised gateway | Sees control frames and in-flight tokens; cannot reach DB. Deploy separately, minimal image, no secrets at rest |
| Malicious job image | Digest-pinned references; optional cosign verification (phase 4); container hardening (12.3); zone egress policy is the operator's responsibility and is documented |

## 8. JSON-RPC 2.0 control protocol ("the wire protocol")

Transport: WebSocket, text frames, one JSON-RPC 2.0 object per frame. Batch requests unsupported. All requests carry `id` (UUIDv4) except notifications. The agent treats the connection as expendable: on any error it reconnects with jittered exponential backoff (1s..60s) and re-sends `worker.hello`.

### 8.1 Methods: worker -> server

| Method | Params | Result | Notes |
| --- | --- | --- | --- |
| `worker.hello` | `{worker_id, agent_version, capabilities, capacity, in_flight: [run_ids]}` | `{server_time, draining, lease_seconds, log_sink_config}` | First frame after auth. `in_flight` lets the server reconcile runs surviving an agent restart |
| `job.claim` | `{max: int}` | `{offers: [JobOffer]}` | May return empty. Offers count toward capacity immediately |
| `job.status` | `{run_id, state: "RUNNING", progress?: {current, total, message}}` | `{lease_expires_at, cancel_requested: bool}` | Doubles as lease renewal and cancel poll fallback |
| `job.complete` | `{run_id, state: SUCCESS\|FAILURE\|TERMINATED, exit_code, image_digest_executed, error?: str, artifact_ids?: []}` | `{ok: true}` | Idempotent per run_id |
| `worker.rotate` | `{}` | `{session_secret}` | |

`JobOffer`:

```json
{
  "run_id": "uuid",
  "definition": "rotate-local-admin",
  "image": "registry.example.com/jobs/rotate-admin@sha256:abc...",
  "inputs": {"devices": ["uuid1"], "dryrun": false},
  "timeout_seconds": 1800,
  "grace_seconds": 30,
  "nautobot_url": "https://nautobot.example.com",
  "token": "<scoped-api-token>",
  "secrets_groups": ["tacacs-prod"],
  "env": {"REMOTE_JOBS_RUN_ID": "uuid", "REMOTE_JOBS_ZONE": "dfw-dc1"}
}
```

### 8.2 Methods: server -> worker

| Method | Params | Notes |
| --- | --- | --- |
| `job.available` | `{zone}` | Notification. Nudges idle workers to `job.claim` |
| `job.cancel` | `{run_id, mode: "graceful"\|"kill"}` | Agent SIGTERMs the container; after `grace_seconds` (or immediately for `kill`) SIGKILLs; then sends `job.complete` with `state=TERMINATED` |
| `worker.drain` | `{}` | Sets local draining; agent stops claiming, finishes in-flight |
| `worker.ping` | `{}` | Gateway-level liveness beyond WS ping/pong; agent replies `{}` |

### 8.3 Error codes

Standard JSON-RPC errors plus app range: `-32001 unauthorized`, `-32002 unknown_run`, `-32003 illegal_transition`, `-32004 lease_expired`, `-32005 singleton_held`, `-32006 draining`.

### 8.4 Gateway <-> app bridging

Redis channels: `remote-jobs:zone:{zone_id}:notify` (fan-out `job.available`), `remote-jobs:worker:{worker_id}:cmd` (targeted server->worker RPCs), `remote-jobs:gateway:rpc` (worker->server requests, handled by app consumers with results published to `remote-jobs:worker:{worker_id}:rsp:{request_id}`). App-side handling runs in a dedicated lightweight consumer (either a `nautobot-server remote_jobs_rpc_consumer` process or Celery-based handlers; decide in phase 1 based on latency testing, target < 250ms claim round trip). The gateway enforces per-worker rate limits (default 30 req/s) and max frame size (256 KiB).

## 9. Secrets resolution contract

Principles: secret **values** never transit Nautobot, the gateway, Kafka, or the DB. Nautobot is the directory; workers resolve locally.

Flow inside the job container (implemented by the SDK):

1. `job.secrets.get(group="tacacs-prod", access_type="Generic", secret_type="password")`.
2. SDK fetches (with the scoped token) the SecretsGroup, its associations, and each Secret's `provider` + `parameters` via the core API.
3. If parameters contain Jinja2 (`{{ obj... }}`), the SDK renders them in a **sandboxed** Jinja2 environment (`jinja2.sandbox.SandboxedEnvironment`) with the target object (fetched via API) as `obj`, matching core `Secret.rendered_parameters` semantics.
4. SDK resolves the value via its local provider registry, keyed by the same slugs core and `nautobot-secrets-providers` use:

| Provider slug | Worker-side resolution | v1 |
| --- | --- | --- |
| `environment-variable` | Read from the **job container's** environment (operator injects via agent config `pass_env` allowlist) | yes |
| `text-file` | Read from a path mounted into the container (agent config `secret_mounts`) | yes |
| `hashicorp-vault` | hvac client using zone-local `VAULT_ADDR` + AppRole/token from agent config, passed to containers via the allowlist | yes |
| `aws-secrets-manager` / others | boto3 etc., same pattern | phase 3, pluggable via entry points |

5. Values are held in memory only. The SDK registers every resolved value with the log redactor (any log line containing a resolved value is masked before leaving the process).

Documented behavior changes vs core: `environment-variable` and `text-file` resolve in the **worker/container** context, not the Nautobot server. This is intentional (zone-local credentials under one Secret definition) and must be called out in migration docs.

Token scoping note: viewing Secret parameters is reconnaissance-sensitive. v1 relies on launcher ObjectPermissions; recommended operator posture is constraining `view_secret` permissions to the groups users actually need. The definition's `secrets_groups` M2M exists so a future middleware (7.3 v2) can enforce it mechanically.

## 10. Log pipeline

The agent and SDK write to a `LogSink` interface with two shipped implementations. Selection comes from `log_sink_config` in the `worker.hello` result, so it is centrally controlled.

Record types: **structured** entries (level, grouping, message; from `job.logger`) -> `JobLogEntry`; **console** lines (stdout/stderr captured from the container) -> `JobConsoleEntry`.

### 10.1 HTTP sink (default)

Batches to the REST endpoints in section 5. Flush on: 2s elapsed, 100 entries, or 64 KiB, whichever first. At-least-once with client-side retry; the server dedupes by `(run_id, client_sequence)` where each batch carries a monotonically increasing sequence.

### 10.2 Kafka sink (optional, for scale)

- Topics: `remote-jobs.logs` and `remote-jobs.console`. Partition key: `run_id` (guarantees per-run ordering). Suggested 12 partitions, operator-tunable.
- Message: JSON `{run_id, sequence, entries: [...]}` matching the HTTP batch schema. No tokens, no secrets (redactor runs before the sink).
- Consumer: `nautobot-server remote_jobs_log_consumer`, shipped by the app, deployed as its own long-running container (same operational pattern as ChatOps workers). Runs inside the Nautobot codebase, writes `JobLogEntry`/`JobConsoleEntry` via ORM in bulk (`bulk_create` per poll batch), commits offsets after DB commit (at-least-once + sequence dedupe = effectively exactly-once).
- Consumer lag is exported as a Prometheus gauge and shown as a staleness badge on the live console view when lag > 5s.

Nautobot core cannot consume Kafka (its events framework is publish-only); the consumer command is therefore part of this app's deliverables, not a configuration of core.

## 11. Cancel integration

At `AppConfig.ready()`:

```python
from nautobot.extras.jobs_cancel import CancelFactory
CancelFactory.strategies[REMOTE_QUEUE_TYPE] = RemoteCancelStrategy  # "remote"
```

`RemoteCancelStrategy` implements the abstract strategy interface (`next` branch: terminate + reap paths):

- **Terminate**: publish `job.cancel {mode: graceful}` on the worker's Redis command channel. The agent SIGTERMs, waits `grace_seconds`, SIGKILLs, sends `job.complete(state=TERMINATED)`. The app then finalizes `revoked_by`, `terminated_at`, `revocation_type=TERMINATED`. If no `job.complete` arrives within `grace_seconds + 60`, fall through to reap.
- **Reap**: for runs whose worker is offline: mark `ABANDONED`, revocation fields accordingly, delete scoped token, apply retry policy.
- Liveness check maps to worker `status` (heartbeat TTL) instead of Celery inspect.

`RemoteJobRun` rows tag their JobResult so the factory routes to this strategy (mechanism: the run's JobResult `celery_kwargs["queue_type"] = "remote"` or equivalent field the CancelFactory keys on; confirm the exact lookup path against 3.2 GA, and pin a regression test that fails if `CancelFactory.strategies` stops being a plain dict).

Because `CancelFactory.strategies` is not a documented extension point, this integration is guarded: if registration fails at ready(), the app logs a warning and falls back to its own Cancel button on the run detail view. Upstreaming a formal registration hook to core is a tracked follow-up.

## 12. Worker agent (`remote-worker`)

### 12.1 Design

- Python 3.11+, fully async (`asyncio`, `websockets`, `httpx`). No pynautobot in the agent; the agent's API surface is the enroll/log/artifact endpoints and the WS protocol only.
- Container runtime abstraction with a Docker/Podman implementation v1 (via `aiodocker` or the podman socket). K8s runtime (launch Jobs in a namespace) is phase 3.
- State volume: session secret, in-flight run journal (for `worker.hello` reconciliation after restart).
- Config: env vars + optional TOML. Keys: `REMOTE_JOBS_URL`, `REMOTE_JOBS_GATEWAY_URL`, `REMOTE_JOBS_ENROLL_TOKEN` (first boot), `capacity`, `capabilities`, `pass_env` allowlist, `secret_mounts`, registry auth, log sink overrides.
- Ships as one OCI image; deployment examples for docker compose and Helm in `deploy/`.

### 12.2 Run lifecycle inside the agent

1. Receive offer -> journal it -> pull `image@digest` (verify digest; optional cosign verify, phase 4).
2. Create container: env = offer `env` + `NAUTOBOT_URL` + `NAUTOBOT_TOKEN` (scoped token) + allowlisted `pass_env`; mounts = `secret_mounts` read-only; no host network unless declared by capability; workdir tmpfs.
3. Stream stdout/stderr -> console sink. Send `job.status` every 25s.
4. On exit: capture exit code, send `job.complete`. On cancel: SIGTERM/SIGKILL per 8.2. On timeout (`timeout_seconds` locally enforced as well): same as kill, `state=FAILURE`, error `timeout`.
5. Delete container, drop journal entry.

### 12.3 Container hardening defaults

`--read-only` rootfs with tmpfs `/tmp`, `no-new-privileges`, default seccomp, non-root user, memory/CPU limits from agent config, no Docker socket mount ever. These are defaults, overridable per agent config with loud documentation.

Standard env contract (also honored by pynautobot, nornir-nautobot, and the `networktocode.nautobot` Ansible collection, which is the migration story): `NAUTOBOT_URL`, `NAUTOBOT_TOKEN`, plus `REMOTE_JOBS_RUN_ID`, `REMOTE_JOBS_ZONE`, `REMOTE_JOBS_DRYRUN`.

## 13. Job SDK (`nautobot-remote-jobs-sdk`)

Pins: `pynautobot>=3.0,<4.0`. Compatibility matrix maintained in docs (SDK x app x Nautobot).

```python
from nautobot_remote_jobs_sdk import job

@job.main
def run(ctx):
    devices = ctx.inputs["devices"]          # validated + typed from input_schema
    ctx.logger.info("Starting", grouping="setup")
    password = ctx.secrets.get("tacacs-prod", access_type="Generic", secret_type="password")
    nb = ctx.api                              # pre-configured pynautobot (token, exclude_m2m=False, retries)
    data = ctx.graphql(QUERY, variables={...})# bulk reads
    if ctx.dryrun:
        ...
```

Provided surface: `ctx.api` (pynautobot with scoped token, `exclude_m2m=False` default, retry/backoff wrapper), `ctx.graphql`, `ctx.logger` (structured -> sink, auto-redaction), `ctx.secrets` (section 9), `ctx.inputs`, `ctx.dryrun`, `ctx.artifacts.upload(path)`. The `@job.main` wrapper handles input parsing, exit codes (0 success, nonzero failure), and final log flush.

Guidance baked into docs: prefer GraphQL for reads touching > ~100 objects; use bulk REST endpoints for writes; a job hammering per-object GETs is the new N+1.

## 14. Job manifest and publishing

Manifests are the source of truth for `JobDefinition` fields and live with the job code:

```yaml
# remote-job.yaml
name: rotate-local-admin
description: Rotate local admin passwords on network devices
image: registry.example.com/jobs/rotate-admin
zone_policy: per_device
capabilities: [ssh-access]
secrets_groups: [tacacs-prod]
timeout_seconds: 1800
dryrun_supported: true
requires_zone_local: true
inputs:            # JSON Schema
  type: object
  required: [devices]
  properties:
    devices:
      type: array
      items: {type: string, format: uuid}
      x-remote-jobs-target: device
    commit:
      type: boolean
      default: false
```

Publishing (v1): `remote-jobs publish --image registry.../rotate-admin:1.4.0` CLI (part of the SDK package) resolves the tag to a digest, reads the manifest (from the repo file or the image's `com.nautobot.remote-jobs.manifest` OCI label), and upserts the JobDefinition via the human REST API. CI-friendly. Git-repo-based sync (datasource style) is phase 3.

The UI run form renders dynamically from `input_schema` (server-side form generation for common types: string, int, bool, enum, uuid-array with object pickers for `x-remote-jobs-target` fields; JSON textarea fallback for the rest).

## 15. UI

Bootstrap 5 / HTMX, generic templates. Navigation menu "Remote Jobs":

- Job Definitions (list/detail/edit, Run button -> dynamic form, run history tab)
- Runs (list mirrors Job Results columns + zone/worker/attempt; detail links to the core JobResult for logs/console; parent runs show child table)
- Execution Zones (list/detail with rules, live worker counts, failover chain)
- Zone Coverage report (4.3)
- Workers (list with status badges, drain/disable actions, enrollment token creation)
- Schedules (list/detail; approval state surfaces via the core approval workflow UI)

## 16. Observability

App/consumer metrics (Prometheus, via nautobot capabilities): `remote_jobs_runs_total{state,zone,definition}`, `remote_jobs_queue_depth{zone}`, `remote_jobs_claim_latency_seconds`, `remote_jobs_run_duration_seconds{definition}`, `remote_jobs_workers_online{zone}`, `remote_jobs_log_consumer_lag`. Gateway: connections, RPC rates, per-method latency. Agent: local queue, container stats, sink flush failures.

Lifecycle events published through `nautobot.core.events.publish_event`: `nautobot.remote_jobs.run.{queued,claimed,started,completed,abandoned,terminated}` with run/zone/worker/definition payloads (no inputs, no tokens).

Health: gateway and agent expose `/healthz`; app-side checks follow the core `django-health-check` + Prometheus gauge pattern.

## 17. Feature parity map vs core Jobs

| Core feature | Remote jobs answer |
| --- | --- |
| Run from UI/API | Dynamic form from `input_schema`; `POST /job-definitions/{id}/run/` |
| Centralized results/logs | Core JobResult/JobLogEntry/JobConsoleEntry reused |
| Live console | JobConsoleEntry via sinks |
| Stop/cancel | Core Cancel button via CancelFactory strategy + JSON-RPC `job.cancel` |
| Scheduling | `RemoteJobSchedule` + app beat task |
| Approvals | `ApprovableModelMixin` on runs and schedules (core 3.0 framework) |
| Dryrun | Manifest flag + `ctx.dryrun` + `REMOTE_JOBS_DRYRUN` |
| Singleton | Claim-time lock |
| Permissions | Launcher-scoped tokens; ObjectPermissions apply for real |
| Job Buttons / Job Hooks | Phase 4+ (hooks: subscribe to change events, enqueue runs) |

## 18. Repository layout (this repo)

```
nautobot-app-remote-jobs/
  SPEC.md
  pyproject.toml                  # the Nautobot app package: nautobot_remote_jobs
  nautobot_remote_jobs/
    __init__.py                   # NautobotAppConfig, ready() strategy registration
    models/                       # section 4, one module per area
    api/                          # human API + worker ingestion API
    rpc/                          # JSON-RPC handlers + Redis bridge consumer
    dispatch/                     # claim, leases, zone resolver, reaper, beat tasks
    cancel.py                     # RemoteCancelStrategy
    management/commands/          # remote_jobs_log_consumer, remote_jobs_rpc_consumer
    ui/  templates/  navigation.py
    tests/
  gateway/                        # remote-jobs-gateway (FastAPI), own pyproject + Dockerfile
  worker/                         # remote-worker agent, own pyproject + Dockerfile
  sdk/                            # nautobot-remote-jobs-sdk (+ `remote-jobs` CLI)
  deploy/                         # compose + Helm examples
  development/                    # nautobot dev env pinned to 3.2 / next
  docs/
```

Four distributable artifacts, one repo, independent versioning via tags (`app-vX`, `gateway-vX`, `worker-vX`, `sdk-vX`) or a synced version to start (simpler; recommended until 1.0).

## 19. Implementation phases

**Phase 1, walking skeleton**: models + migrations; enrollment; gateway with hello/claim/status/complete; agent running a hardcoded busybox "job" via Docker; HTTP log sink -> JobLogEntry; JobResult creation and state sync; manual run from a minimal UI form. Exit criterion: a run submitted in the UI executes in a container on a remote docker host and its logs appear in the core Job Result view.

**Phase 2, correctness and safety**: leases + reaper + ABANDONED; cancel end-to-end (strategy registration + `job.cancel`); per-job scoped tokens (replacing any phase-1 static token); secrets resolution (env, file, vault) with redaction; input_schema validation + dynamic form; singleton; console sink; SDK 0.1 with `ctx.api`/`ctx.logger`/`ctx.secrets`.

**Phase 3, topology and scale**: zone membership rules + resolver + coverage report; per_device and fan_out with parent/child runs; failover policies; schedules + beat + approvals; Kafka sink + consumer command; `remote-jobs publish` CLI; GraphQL helper; worker drain + rolling upgrade docs; K8s runtime for the agent.

**Phase 4, hardening and polish**: cosign verification; mTLS gateway option; artifact storage; token-narrowing middleware; Job Hooks-style event triggers; UI polish; load testing (target: 50 workers, 10k runs/day, p95 claim < 250ms); upstream PR for CancelFactory/queue-type registration hooks.

## 20. Open questions

1. RPC handling in the app: dedicated consumer process vs Celery-backed handlers (decide in phase 1 on measured claim latency).
2. Exact 3.2 GA field semantics for revocation mapping (6.3) and the CancelFactory lookup key (11); both are pinned to `next` behavior today.
3. Whether `RemoteJobRun` needs its own ObjectPermission-relevant fields (tenant?) for multi-tenant deployments; JobQueue grew a `tenant` FK in core, suggesting demand.
4. Gateway session store for HMAC challenge nonces: Redis (shared with bridge) vs stateless signed nonces.
5. Whether to publish worker liveness transitions (ONLINE/OFFLINE) as events for alerting, and debounce policy.
