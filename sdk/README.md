# nautobot-remote-jobs-sdk

The library that Nautobot **remote job** containers import, plus the `remote-jobs`
publishing CLI. Part of the [nautobot-app-remote-jobs](../SPEC.md) project
(SPEC sections 9, 12.3, 13, and 14).

Remote jobs run in isolated containers on remote workers and talk to Nautobot
exclusively through the REST/GraphQL API using a short-lived token scoped to the
launching user. This SDK provides the ergonomic surface for that model.

## Writing a job

```python
from nautobot_remote_jobs_sdk import job

@job.main
def run(ctx):
    devices = ctx.inputs["devices"]          # validated against input_schema
    ctx.logger.info("Starting", grouping="setup")

    password = ctx.secrets.get("tacacs-prod", access_type="Generic", secret_type="password")

    nb = ctx.api                             # pre-configured pynautobot
    data = ctx.graphql(QUERY, variables={"ids": devices})

    if ctx.dryrun:
        ctx.logger.info("Dry run, making no changes")
        return
```

The container entrypoint is simply `python job.py` — `@job.main` executes the
function when the module runs as `__main__`, exits `0` on success, `1` on any
exception, and flushes all buffered logs before the process ends.

## Environment contract

The worker agent injects (SPEC 12.3):

| Variable | Meaning |
| --- | --- |
| `NAUTOBOT_URL` | Base Nautobot URL (also honored by pynautobot, nornir-nautobot, and the `networktocode.nautobot` Ansible collection) |
| `NAUTOBOT_TOKEN` | Short-lived API token scoped to the launching user |
| `REMOTE_JOBS_RUN_ID` | UUID of this run |
| `REMOTE_JOBS_ZONE` | Execution zone name |
| `REMOTE_JOBS_DRYRUN` | `true`/`false` |
| `REMOTE_JOBS_INPUTS` | Job inputs as a JSON object (alternative: the file `/run/remote-jobs/inputs.json`) |
| `REMOTE_JOBS_INPUT_SCHEMA` | Optional JSON Schema; when present, inputs are validated with `jsonschema` before the job starts |

## The `ctx` surface

### `ctx.api` — pynautobot

A lazily-built `pynautobot.api(...)` instance authenticated with the scoped token, with:

- **Retries/backoff**: `retries=3` (pynautobot 3.x mounts a urllib3 `Retry`
  adapter — backoff factor 1, retried statuses 429/500/502/503/504).
- **M2M fields included**: Nautobot 3.0's REST API *excludes* many-to-many
  fields by default (`exclude_m2m`). The SDK passes `exclude_m2m=False`, which
  pynautobot 3.x threads onto every request as the `exclude_m2m=false` query
  parameter via its `default_filters`. Records returned through `ctx.api`
  therefore include M2M fields (tags, tagged VLANs, secrets group
  associations, ...) just like pre-3.0. If you build your own client instead
  of using `ctx.api`, remember to opt in yourself.

Guidance: prefer `ctx.graphql` for reads touching more than ~100 objects, and
bulk REST endpoints for writes. A job hammering per-object GETs is the new N+1.

### `ctx.graphql(query, variables=None)`

POSTs to `{NAUTOBOT_URL}/api/graphql/` with the scoped token, returns the
`data` payload, raises `GraphQLError` when the response carries `errors`.

### `ctx.logger`

Structured logger with `debug` / `info` / `success` / `warning` / `error` /
`failure` methods (mapping 1:1 to Nautobot `JobLogEntry` levels), each accepting
`grouping=`. Entries are:

1. passed through the **redactor** (see below),
2. echoed to stdout (so worker console capture works),
3. buffered and shipped to `/api/plugins/remote-jobs/runs/{run_id}/logs/` in
   batches — flushed on 2 s elapsed / 100 entries / 64 KiB, whichever first.
   Each batch carries a monotonically increasing `client_sequence` so the
   server can dedupe at-least-once delivery:
   `{"client_sequence": N, "entries": [{level, message, grouping, timestamp}]}`.

### `ctx.secrets` — worker-local resolution

`ctx.secrets.get(group, access_type="Generic", secret_type="password", obj=None)`

Nautobot is only the **directory**: the SDK fetches the SecretsGroup, its
associations, and the matching Secret's `provider` slug + `parameters` via the
core REST API. If the parameters contain Jinja2, they are rendered in a
`jinja2.sandbox.SandboxedEnvironment` with `obj` in context (matching core
`Secret.rendered_parameters` semantics). The **value** is then resolved locally:

| Provider slug | Resolution in the job container | Parameters |
| --- | --- | --- |
| `environment-variable` | `os.environ` (populated via the agent's `pass_env` allowlist) | `{"variable": "NAME"}` |
| `text-file` | file mounted via the agent's `secret_mounts` (whitespace-stripped like core) | `{"path": "/run/secrets/..."}` |
| `hashicorp-vault` | `hvac` against the zone-local Vault (`VAULT_ADDR` + `VAULT_TOKEN`, or `VAULT_ROLE_ID`/`VAULT_SECRET_ID` AppRole); install the `vault` extra | `{"path", "key", "mount_point", "kv_version"}` |

Note the deliberate behavior change vs core: `environment-variable` and
`text-file` resolve in the **worker/container** context, not on the Nautobot
server — that is what makes zone-local credentials under one Secret definition
work.

Additional providers plug in through the entry point group
`nautobot_remote_jobs_sdk.secrets_providers` (entry point name = provider slug,
target = a class with `resolve(parameters) -> str`).

Secret values live in memory only, and **every resolved value is registered
with the redactor**.

### Redaction

A module-level registry (`nautobot_remote_jobs_sdk.redaction`) masks every
registered secret value as `(redacted)`. It is wired into `ctx.logger`, and
`@job.main` also wraps `sys.stdout`/`sys.stderr` in redacting proxies so plain
`print()` output captured as console entries is masked too.

### `ctx.inputs`, `ctx.dryrun`

Parsed inputs (dict) and the dry-run flag. When `REMOTE_JOBS_INPUT_SCHEMA` is
set, inputs failing validation abort the job before your code runs (exit 1).

### `ctx.artifacts.upload(path, name=None, content_type=None)`

1. `POST /api/plugins/remote-jobs/runs/{id}/artifacts/` → `{upload_url, artifact_id}`
2. `PUT` file bytes to the presigned `upload_url`
3. `PUT .../artifacts/{artifact_id}/complete/` with the file's SHA-256

Returns the `artifact_id`.

## Publishing job definitions: `remote-jobs publish`

```console
$ remote-jobs publish --image registry.example.com/jobs/rotate-admin:1.4.0 \
    [--manifest remote-job.yaml] [--url https://nautobot...] [--token ...] \
    [--digest sha256:...] [--registry-username ...] [--registry-password ...]
```

- Resolves the tag to a digest via the registry HTTP API v2
  (`HEAD /v2/{repo}/manifests/{tag}`, `Docker-Content-Digest` header), handling
  basic and bearer auth including the anonymous Docker Hub token flow.
  `--digest` overrides resolution.
- Reads the `remote-job.yaml` manifest (see `examples/rotate-local-admin/`);
  the manifest's `inputs` key becomes the definition's `input_schema`,
  `secrets_groups` names become natural-key references.
- Upserts the JobDefinition via
  `/api/plugins/remote-jobs/job-definitions/` — found by `name`, then PATCH
  (existing) or POST (new). CI-friendly: idempotent, exit code 0/1.

`--url`/`--token` default to `NAUTOBOT_URL`/`NAUTOBOT_TOKEN`;
registry credentials default to `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`.

## Installation

```console
pip install nautobot-remote-jobs-sdk           # core
pip install "nautobot-remote-jobs-sdk[vault]"  # + HashiCorp Vault provider
```

## Compatibility

| SDK | pynautobot | Nautobot | app |
| --- | --- | --- | --- |
| 0.1.x | >=3.0,<4.0 | >=3.2 | 0.1.x |

## Development

```console
cd sdk
pip install -e .[dev,vault]
pytest
```
