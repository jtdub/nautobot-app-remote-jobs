"""Job execution context: env contract parsing and the ``ctx`` object.

Env contract (SPEC 12.3): ``NAUTOBOT_URL``, ``NAUTOBOT_TOKEN``,
``REMOTE_JOBS_RUN_ID``, ``REMOTE_JOBS_ZONE``, ``REMOTE_JOBS_DRYRUN``, plus
job inputs from ``REMOTE_JOBS_INPUTS`` (JSON string) or the file
``/run/remote-jobs/inputs.json``, and an optional
``REMOTE_JOBS_INPUT_SCHEMA`` (JSON Schema) used to validate the inputs.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, Optional

from . import http
from .artifacts import ArtifactsClient
from .graphql import GraphQLClient
from .logging import JobLogger, LogClient
from .secrets import SecretsClient

#: Default path the worker agent mounts job inputs at.
INPUTS_FILE = "/run/remote-jobs/inputs.json"

_TRUTHY = {"1", "true", "yes", "on"}


class ContextError(RuntimeError):
    """The environment does not satisfy the remote-jobs env contract."""


class InputValidationError(ContextError):
    """The provided inputs do not validate against the input schema."""


def _parse_bool(value: Optional[str]) -> bool:
    return bool(value) and value.strip().lower() in _TRUTHY


def _load_inputs(environ: Mapping[str, str], inputs_file: str) -> Dict[str, Any]:
    """Load job inputs from env var or the mounted inputs file."""
    raw = environ.get("REMOTE_JOBS_INPUTS")
    source = "REMOTE_JOBS_INPUTS"
    if raw is None and os.path.exists(inputs_file):
        source = inputs_file
        with open(inputs_file, "r", encoding="utf-8") as handle:
            raw = handle.read()
    if raw is None or raw.strip() == "":
        return {}
    try:
        inputs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContextError(f"Job inputs from {source} are not valid JSON: {exc}") from exc
    if not isinstance(inputs, dict):
        raise ContextError(f"Job inputs from {source} must be a JSON object")
    return inputs


def _validate_inputs(inputs: Dict[str, Any], raw_schema: Optional[str]) -> None:
    """Validate *inputs* against the optional JSON Schema."""
    if not raw_schema:
        return
    try:
        schema = json.loads(raw_schema)
    except json.JSONDecodeError as exc:
        raise ContextError(f"REMOTE_JOBS_INPUT_SCHEMA is not valid JSON: {exc}") from exc
    import jsonschema  # noqa: PLC0415 - keep import local to the validation path

    try:
        jsonschema.validate(instance=inputs, schema=schema)
    except jsonschema.ValidationError as exc:
        raise InputValidationError(f"Job inputs failed schema validation: {exc.message}") from exc


class Context:
    """The ``ctx`` object handed to a ``@job.main``-decorated ``run(ctx)``.

    Attributes:
        nautobot_url: Base Nautobot URL (``NAUTOBOT_URL``).
        run_id: This run's UUID (``REMOTE_JOBS_RUN_ID``).
        zone: Execution zone name (``REMOTE_JOBS_ZONE``).
        dryrun: Whether the run is a dry run (``REMOTE_JOBS_DRYRUN``).
        inputs: Validated job inputs.
        logger: Structured :class:`~nautobot_remote_jobs_sdk.logging.JobLogger`.
        secrets: :class:`~nautobot_remote_jobs_sdk.secrets.SecretsClient`.
        artifacts: :class:`~nautobot_remote_jobs_sdk.artifacts.ArtifactsClient`.
        graphql: Callable :class:`~nautobot_remote_jobs_sdk.graphql.GraphQLClient`.
    """

    def __init__(
        self,
        nautobot_url: str,
        token: str,
        run_id: str,
        zone: str = "",
        dryrun: bool = False,
        inputs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.nautobot_url = nautobot_url.rstrip("/")
        self._token = token
        self.run_id = run_id
        self.zone = zone
        self.dryrun = dryrun
        self.inputs: Dict[str, Any] = inputs if inputs is not None else {}

        session = http.build_session(token=token)
        self._session = session
        self._log_client = LogClient(session, self.nautobot_url, run_id)
        self.logger = JobLogger(client=self._log_client)
        self.secrets = SecretsClient(session, self.nautobot_url)
        self.artifacts = ArtifactsClient(session, self.nautobot_url, run_id)
        self.graphql = GraphQLClient(session, self.nautobot_url)
        self._api: Any = None

    @classmethod
    def from_env(
        cls,
        environ: Optional[Mapping[str, str]] = None,
        inputs_file: str = INPUTS_FILE,
    ) -> "Context":
        """Build a context from the remote-jobs env contract.

        Raises:
            ContextError: on a missing/invalid contract variable.
            InputValidationError: when inputs fail schema validation.
        """
        env = environ if environ is not None else os.environ

        nautobot_url = env.get("NAUTOBOT_URL", "").strip()
        token = env.get("NAUTOBOT_TOKEN", "").strip()
        run_id = env.get("REMOTE_JOBS_RUN_ID", "").strip()
        missing = [
            name
            for name, value in (
                ("NAUTOBOT_URL", nautobot_url),
                ("NAUTOBOT_TOKEN", token),
                ("REMOTE_JOBS_RUN_ID", run_id),
            )
            if not value
        ]
        if missing:
            raise ContextError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Is this process running under a remote-jobs worker agent?"
            )

        inputs = _load_inputs(env, inputs_file)
        _validate_inputs(inputs, env.get("REMOTE_JOBS_INPUT_SCHEMA"))

        return cls(
            nautobot_url=nautobot_url,
            token=token,
            run_id=run_id,
            zone=env.get("REMOTE_JOBS_ZONE", ""),
            dryrun=_parse_bool(env.get("REMOTE_JOBS_DRYRUN")),
            inputs=inputs,
        )

    @property
    def api(self) -> Any:
        """Pre-configured :class:`pynautobot.core.api.Api` (built lazily).

        Configuration notes:

        - Token auth against ``NAUTOBOT_URL`` with the scoped per-job token.
        - ``retries=3``: pynautobot 3.x mounts a urllib3 ``Retry`` adapter
          (backoff factor 1, statuses 429/500/502/503/504).
        - ``exclude_m2m=False``: Nautobot 3.0's REST API excludes
          many-to-many fields by default. pynautobot 3.x exposes this as a
          first-class kwarg that adds ``exclude_m2m=false`` to
          ``default_filters``, i.e. it is threaded onto every request's
          query string -- so M2M fields (tags, secrets group associations,
          interface tagged VLANs, ...) are present on records the SDK
          returns, matching pre-3.0 expectations.
        """
        if self._api is None:
            import pynautobot  # noqa: PLC0415 - imported lazily

            self._api = pynautobot.api(
                self.nautobot_url,
                token=self._token,
                retries=3,
                exclude_m2m=False,
            )
        return self._api

    # -- lifecycle (driven by the @job.main decorator) ---------------------

    def _start(self) -> None:
        """Start background services (the time-based log flusher)."""
        self._log_client.start()

    def close(self) -> None:
        """Stop background services and flush any buffered logs."""
        self._log_client.stop()
