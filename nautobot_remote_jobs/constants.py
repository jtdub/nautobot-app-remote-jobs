"""Constants for nautobot_remote_jobs."""

# Queue type registered with the core CancelFactory (see SPEC section 11).
REMOTE_QUEUE_TYPE = "remote"

# Regex for OCI image digests.
IMAGE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

# JSON Schema annotation marking a device-bearing input field (SPEC 6.4).
TARGET_ANNOTATION = "x-remote-jobs-target"

# Redacted placeholder stored in RemoteJobRun.inputs for writeOnly schema keys.
REDACTED_PLACEHOLDER = "__redacted__"

# Redis channel name templates (SPEC 8.4).
CHANNEL_ZONE_NOTIFY = "remote-jobs:zone:{zone_id}:notify"
CHANNEL_WORKER_CMD = "remote-jobs:worker:{worker_id}:cmd"
CHANNEL_GATEWAY_RPC = "remote-jobs:gateway:rpc"
CHANNEL_WORKER_RSP = "remote-jobs:worker:{worker_id}:rsp:{request_id}"

# Server-enforced ingestion limits (SPEC 5).
LOG_BATCH_MAX_ENTRIES = 500
LOG_BATCH_MAX_BYTES = 256 * 1024

# Max artifact upload size accepted by the content endpoint (override via
# PLUGINS_CONFIG["nautobot_remote_jobs"]["artifact_max_bytes"]).
DEFAULT_ARTIFACT_MAX_BYTES = 100 * 1024 * 1024

# Max target devices a single per_device/fan_out submission may expand into,
# bounding the per-request row creation (override via "fanout_max_devices").
DEFAULT_FANOUT_MAX_DEVICES = 500

# JSON-RPC application error codes (SPEC 8.3).
RPC_UNAUTHORIZED = -32001
RPC_UNKNOWN_RUN = -32002
RPC_ILLEGAL_TRANSITION = -32003
RPC_LEASE_EXPIRED = -32004
RPC_SINGLETON_HELD = -32005
RPC_DRAINING = -32006

# Event topics (SPEC 16).
EVENT_PREFIX = "nautobot.remote_jobs.run"

# Default settings, overridable via PLUGINS_CONFIG["nautobot_remote_jobs"].
DEFAULT_WORKER_TTL_SECONDS = 90
DEFAULT_LEASE_SECONDS = 120
DEFAULT_LAST_SEEN_THROTTLE_SECONDS = 15
DEFAULT_TOKEN_EXTRA_TTL_SECONDS = 60
