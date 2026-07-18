"""Tests for configuration parsing (env vars + optional TOML, SPEC 12.1)."""

import pytest
from remote_worker.config import (
    ConfigError,
    SecretMount,
    WorkerConfig,
    parse_memory_limit,
)


def test_defaults():
    config = WorkerConfig.load(env={})
    assert config.capacity == 4
    assert config.capabilities == []
    assert config.pass_env == []
    assert config.secret_mounts == []
    assert config.log_sink_override is None
    assert str(config.state_dir) == "/var/lib/remote-worker"
    assert config.health_port == 8080
    assert config.tls_verify is True


def test_env_parsing():
    env = {
        "REMOTE_JOBS_URL": "https://nautobot.example.com",
        "REMOTE_JOBS_GATEWAY_URL": "https://gateway.example.com",
        "REMOTE_JOBS_ENROLL_TOKEN": "tok123",
        "REMOTE_WORKER_NAME": "dfw-worker-1",
        "REMOTE_WORKER_CAPACITY": "8",
        "REMOTE_WORKER_CAPABILITIES": "ssh-access, netconf ,",
        "REMOTE_WORKER_PASS_ENV": "VAULT_ADDR,VAULT_TOKEN",
        "REMOTE_WORKER_SECRET_MOUNTS": "/etc/secrets/tacacs:/run/secrets/tacacs",
        "REMOTE_WORKER_STATE_DIR": "/data/worker",
        "REMOTE_WORKER_HEALTH_PORT": "9999",
        "REMOTE_WORKER_MEMORY_LIMIT": "512m",
        "REMOTE_WORKER_CPU_LIMIT": "1.5",
        "REMOTE_WORKER_TLS_VERIFY": "false",
    }
    config = WorkerConfig.load(env=env)
    assert config.nautobot_url == "https://nautobot.example.com"
    assert config.gateway_url == "https://gateway.example.com"
    assert config.enroll_token == "tok123"
    assert config.name == "dfw-worker-1"
    assert config.capacity == 8
    assert config.capabilities == ["ssh-access", "netconf"]
    assert config.pass_env == ["VAULT_ADDR", "VAULT_TOKEN"]
    assert config.secret_mounts == [SecretMount(source="/etc/secrets/tacacs", target="/run/secrets/tacacs")]
    assert str(config.state_dir) == "/data/worker"
    assert config.health_port == 9999
    assert config.memory_limit_bytes == 512 * 1024 * 1024
    assert config.cpu_limit == 1.5
    assert config.tls_verify is False
    config.validate()  # must not raise


def test_toml_file_and_env_precedence(tmp_path):
    toml_file = tmp_path / "worker.toml"
    toml_file.write_text(
        """
nautobot_url = "https://from-toml.example.com"
gateway_url = "https://gw-toml.example.com"
capacity = 2
capabilities = ["ssh-access"]
pass_env = ["VAULT_ADDR"]
memory_limit = "1g"

[[secret_mounts]]
source = "/etc/secrets/a"
target = "/run/secrets/a"

[log_sink]
type = "kafka"
bootstrap_servers = "kafka1:9092"

[registry_auth."registry.example.com"]
username = "puller"
password = "hunter2"
""",
        encoding="utf-8",
    )
    env = {
        "REMOTE_WORKER_CONFIG": str(toml_file),
        # env must win over TOML:
        "REMOTE_JOBS_URL": "https://from-env.example.com",
        "REMOTE_WORKER_CAPACITY": "6",
    }
    config = WorkerConfig.load(env=env)
    assert config.nautobot_url == "https://from-env.example.com"
    assert config.gateway_url == "https://gw-toml.example.com"
    assert config.capacity == 6
    assert config.capabilities == ["ssh-access"]
    assert config.memory_limit_bytes == 1024**3
    assert config.secret_mounts == [SecretMount(source="/etc/secrets/a", target="/run/secrets/a")]
    assert config.log_sink_override == {"type": "kafka", "bootstrap_servers": "kafka1:9092"}
    assert config.registry_auth_for("registry.example.com/jobs/x@sha256:" + "0" * 64) == {
        "username": "puller",
        "password": "hunter2",
    }
    assert config.registry_auth_for("other.example.com/jobs/x") is None


def test_log_sink_env_override():
    config = WorkerConfig.load(
        env={
            "REMOTE_WORKER_LOG_SINK": "kafka",
            "REMOTE_WORKER_KAFKA_BOOTSTRAP_SERVERS": "k1:9092,k2:9092",
        }
    )
    assert config.log_sink_override == {
        "type": "kafka",
        "bootstrap_servers": "k1:9092,k2:9092",
    }


def test_registry_auth_env_wildcard():
    config = WorkerConfig.load(
        env={
            "REMOTE_WORKER_REGISTRY_USERNAME": "user",
            "REMOTE_WORKER_REGISTRY_PASSWORD": "pass",
        }
    )
    assert config.registry_auth_for("anything.example.com/img@sha256:" + "0" * 64) == {
        "username": "user",
        "password": "pass",
    }


def test_memory_limit_parsing():
    assert parse_memory_limit("512m") == 512 * 1024**2
    assert parse_memory_limit("1g") == 1024**3
    assert parse_memory_limit("64k") == 64 * 1024
    assert parse_memory_limit("1024") == 1024
    assert parse_memory_limit(2048) == 2048
    assert parse_memory_limit("1gb") == 1024**3
    with pytest.raises(ConfigError):
        parse_memory_limit("lots")


def test_invalid_capacity_raises():
    with pytest.raises(ConfigError):
        WorkerConfig.load(env={"REMOTE_WORKER_CAPACITY": "many"})


def test_invalid_secret_mount_raises():
    with pytest.raises(ConfigError):
        WorkerConfig.load(env={"REMOTE_WORKER_SECRET_MOUNTS": "/no-target"})


def test_validate_requires_urls():
    with pytest.raises(ConfigError):
        WorkerConfig.load(env={}).validate()


def test_missing_toml_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        WorkerConfig.load(env={"REMOTE_WORKER_CONFIG": str(tmp_path / "missing.toml")})
