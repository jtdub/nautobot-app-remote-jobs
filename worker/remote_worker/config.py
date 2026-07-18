"""Configuration loading for the remote worker agent.

Configuration is sourced from environment variables and an optional TOML
file whose path is given by the ``REMOTE_WORKER_CONFIG`` environment
variable. Environment variables take precedence over the TOML file, which
takes precedence over built-in defaults (SPEC 12.1).

Recognized environment variables:

===============================================  =====================================
Variable                                         Meaning
===============================================  =====================================
``REMOTE_JOBS_URL``                              Nautobot base URL (REST endpoints)
``REMOTE_JOBS_GATEWAY_URL``                      Gateway base URL (``/ws/worker``)
``REMOTE_JOBS_ENROLL_TOKEN``                     One-shot enrollment token (first boot)
``REMOTE_WORKER_NAME``                           Worker name (default: hostname)
``REMOTE_WORKER_CAPACITY``                       Max concurrent runs (default 4)
``REMOTE_WORKER_CAPABILITIES``                   Comma-separated capability labels
``REMOTE_WORKER_PASS_ENV``                       Comma-separated env allowlist passed
                                                 into job containers
``REMOTE_WORKER_SECRET_MOUNTS``                  ``src:dst[,src:dst...]`` read-only
                                                 mounts into job containers
``REMOTE_WORKER_STATE_DIR``                      State volume (default
                                                 ``/var/lib/remote-worker``)
``REMOTE_WORKER_HEALTH_HOST`` / ``_PORT``        Health endpoint bind (default
                                                 ``0.0.0.0:8080``)
``REMOTE_WORKER_MEMORY_LIMIT``                   Per-container memory limit
                                                 (bytes or ``512m`` / ``1g``)
``REMOTE_WORKER_CPU_LIMIT``                      Per-container CPU limit (float CPUs)
``REMOTE_WORKER_LOG_SINK``                       Log sink override: ``http``/``kafka``
``REMOTE_WORKER_KAFKA_BOOTSTRAP_SERVERS``        Kafka bootstrap servers (kafka sink)
``REMOTE_WORKER_REGISTRY_HOST`` / ``_USERNAME``
/ ``_PASSWORD``                                  Registry credentials for image pulls
``REMOTE_WORKER_TLS_VERIFY``                     Set to ``0``/``false`` to disable TLS
                                                 verification (labs only)
``REMOTE_WORKER_CONFIG``                         Path to the optional TOML file
===============================================  =====================================
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import tomllib

logger = logging.getLogger(__name__)

ENV_CONFIG_FILE = "REMOTE_WORKER_CONFIG"
DEFAULT_STATE_DIR = Path("/var/lib/remote-worker")

_FALSY = {"0", "false", "no", "off", ""}

_MEMORY_SUFFIXES = {
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}


class ConfigError(ValueError):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class SecretMount:
    """A read-only bind mount projected into every job container."""

    source: str
    target: str
    read_only: bool = True


def _parse_bool(value: str) -> bool:
    return value.strip().lower() not in _FALSY


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_memory_limit(value: str | int) -> int:
    """Parse a memory limit like ``536870912``, ``512m``, or ``1g`` to bytes."""
    if isinstance(value, int):
        return value
    text = value.strip().lower()
    if not text:
        raise ConfigError("empty memory limit")
    suffix = text[-1]
    if suffix in _MEMORY_SUFFIXES:
        number, multiplier = text[:-1], _MEMORY_SUFFIXES[suffix]
    elif suffix == "b" and len(text) > 1 and text[-2] in _MEMORY_SUFFIXES:
        number, multiplier = text[:-2], _MEMORY_SUFFIXES[text[-2]]
    else:
        number, multiplier = text, 1
    try:
        return int(float(number) * multiplier)
    except ValueError as exc:
        raise ConfigError(f"invalid memory limit: {value!r}") from exc


def _parse_secret_mounts_env(value: str) -> list[SecretMount]:
    mounts: list[SecretMount] = []
    for item in _parse_csv(value):
        source, sep, target = item.partition(":")
        if not sep or not source or not target:
            raise ConfigError(f"invalid secret mount {item!r}; expected 'source:target'")
        mounts.append(SecretMount(source=source, target=target))
    return mounts


@dataclass
class WorkerConfig:
    """Fully resolved agent configuration."""

    nautobot_url: str = ""
    gateway_url: str = ""
    enroll_token: str | None = None
    name: str = field(default_factory=socket.gethostname)
    capacity: int = 4
    capabilities: list[str] = field(default_factory=list)
    pass_env: list[str] = field(default_factory=list)
    secret_mounts: list[SecretMount] = field(default_factory=list)
    registry_auth: dict[str, dict[str, str]] = field(default_factory=dict)
    log_sink_override: dict[str, Any] | None = None
    state_dir: Path = DEFAULT_STATE_DIR
    health_host: str = "0.0.0.0"  # noqa: S104 - containerized agent health endpoint
    health_port: int = 8080
    memory_limit_bytes: int | None = None
    cpu_limit: float | None = None
    status_interval: float = 25.0
    backoff_min: float = 1.0
    backoff_max: float = 60.0
    tls_verify: bool = True

    # ------------------------------------------------------------------ load

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "WorkerConfig":
        """Build a config from a TOML file (optional) overlaid with env vars."""
        env = os.environ if env is None else env
        config = cls()
        toml_path = env.get(ENV_CONFIG_FILE)
        if toml_path:
            config._apply_toml(Path(toml_path))
        config._apply_env(env)
        return config

    def _apply_toml(self, path: Path) -> None:
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

        simple_str = {
            "nautobot_url",
            "gateway_url",
            "enroll_token",
            "name",
            "health_host",
        }
        for key in simple_str:
            if key in data:
                setattr(self, key, str(data[key]))
        if "capacity" in data:
            self.capacity = int(data["capacity"])
        if "health_port" in data:
            self.health_port = int(data["health_port"])
        if "capabilities" in data:
            self.capabilities = [str(item) for item in data["capabilities"]]
        if "pass_env" in data:
            self.pass_env = [str(item) for item in data["pass_env"]]
        if "state_dir" in data:
            self.state_dir = Path(str(data["state_dir"]))
        if "memory_limit" in data:
            self.memory_limit_bytes = parse_memory_limit(data["memory_limit"])
        if "cpu_limit" in data:
            self.cpu_limit = float(data["cpu_limit"])
        if "status_interval" in data:
            self.status_interval = float(data["status_interval"])
        if "tls_verify" in data:
            self.tls_verify = bool(data["tls_verify"])
        for mount in data.get("secret_mounts", []):
            self.secret_mounts.append(
                SecretMount(
                    source=str(mount["source"]),
                    target=str(mount["target"]),
                    read_only=bool(mount.get("read_only", True)),
                )
            )
        if "log_sink" in data:
            sink = dict(data["log_sink"])
            if "type" not in sink:
                raise ConfigError("[log_sink] requires a 'type' key")
            self.log_sink_override = sink
        for registry, creds in data.get("registry_auth", {}).items():
            self.registry_auth[str(registry)] = {
                "username": str(creds.get("username", "")),
                "password": str(creds.get("password", "")),
            }

    def _apply_env(self, env: Mapping[str, str]) -> None:
        if "REMOTE_JOBS_URL" in env:
            self.nautobot_url = env["REMOTE_JOBS_URL"]
        if "REMOTE_JOBS_GATEWAY_URL" in env:
            self.gateway_url = env["REMOTE_JOBS_GATEWAY_URL"]
        if "REMOTE_JOBS_ENROLL_TOKEN" in env:
            self.enroll_token = env["REMOTE_JOBS_ENROLL_TOKEN"]
        if "REMOTE_WORKER_NAME" in env:
            self.name = env["REMOTE_WORKER_NAME"]
        if "REMOTE_WORKER_CAPACITY" in env:
            try:
                self.capacity = int(env["REMOTE_WORKER_CAPACITY"])
            except ValueError as exc:
                raise ConfigError("REMOTE_WORKER_CAPACITY must be an integer") from exc
        if "REMOTE_WORKER_CAPABILITIES" in env:
            self.capabilities = _parse_csv(env["REMOTE_WORKER_CAPABILITIES"])
        if "REMOTE_WORKER_PASS_ENV" in env:
            self.pass_env = _parse_csv(env["REMOTE_WORKER_PASS_ENV"])
        if "REMOTE_WORKER_SECRET_MOUNTS" in env:
            self.secret_mounts = _parse_secret_mounts_env(env["REMOTE_WORKER_SECRET_MOUNTS"])
        if "REMOTE_WORKER_STATE_DIR" in env:
            self.state_dir = Path(env["REMOTE_WORKER_STATE_DIR"])
        if "REMOTE_WORKER_HEALTH_HOST" in env:
            self.health_host = env["REMOTE_WORKER_HEALTH_HOST"]
        if "REMOTE_WORKER_HEALTH_PORT" in env:
            try:
                self.health_port = int(env["REMOTE_WORKER_HEALTH_PORT"])
            except ValueError as exc:
                raise ConfigError("REMOTE_WORKER_HEALTH_PORT must be an integer") from exc
        if "REMOTE_WORKER_MEMORY_LIMIT" in env:
            self.memory_limit_bytes = parse_memory_limit(env["REMOTE_WORKER_MEMORY_LIMIT"])
        if "REMOTE_WORKER_CPU_LIMIT" in env:
            try:
                self.cpu_limit = float(env["REMOTE_WORKER_CPU_LIMIT"])
            except ValueError as exc:
                raise ConfigError("REMOTE_WORKER_CPU_LIMIT must be a number") from exc
        if "REMOTE_WORKER_TLS_VERIFY" in env:
            self.tls_verify = _parse_bool(env["REMOTE_WORKER_TLS_VERIFY"])
        if "REMOTE_WORKER_LOG_SINK" in env:
            override = dict(self.log_sink_override or {})
            override["type"] = env["REMOTE_WORKER_LOG_SINK"]
            self.log_sink_override = override
        if "REMOTE_WORKER_KAFKA_BOOTSTRAP_SERVERS" in env:
            override = dict(self.log_sink_override or {"type": "kafka"})
            override["bootstrap_servers"] = env["REMOTE_WORKER_KAFKA_BOOTSTRAP_SERVERS"]
            self.log_sink_override = override
        username = env.get("REMOTE_WORKER_REGISTRY_USERNAME")
        password = env.get("REMOTE_WORKER_REGISTRY_PASSWORD")
        if username or password:
            registry = env.get("REMOTE_WORKER_REGISTRY_HOST", "*")
            self.registry_auth[registry] = {
                "username": username or "",
                "password": password or "",
            }

    # -------------------------------------------------------------- helpers

    def validate(self) -> None:
        """Validate required settings for normal operation."""
        if not self.nautobot_url:
            raise ConfigError("REMOTE_JOBS_URL is required")
        if not self.gateway_url:
            raise ConfigError("REMOTE_JOBS_GATEWAY_URL is required")
        if self.capacity < 1:
            raise ConfigError("capacity must be >= 1")

    def registry_auth_for(self, image_ref: str) -> dict[str, str] | None:
        """Return pull credentials matching *image_ref*'s registry, if any."""
        if not self.registry_auth:
            return None
        head = image_ref.split("/", 1)[0]
        registry = head if ("." in head or ":" in head or head == "localhost") else "docker.io"
        return self.registry_auth.get(registry) or self.registry_auth.get("*")

    @property
    def state_file(self) -> Path:
        """Path of the persisted enrollment state file."""
        return self.state_dir / "state.json"

    @property
    def journal_dir(self) -> Path:
        """Directory holding the in-flight run journal."""
        return self.state_dir / "journal"
