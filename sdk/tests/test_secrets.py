"""Tests for secret providers, sandboxed rendering, and the SecretsClient flow."""

from __future__ import annotations

import pytest
from conftest import FakeResponse, FakeSession
from jinja2.exceptions import SecurityError
from nautobot_remote_jobs_sdk import redaction
from nautobot_remote_jobs_sdk.secrets import SecretNotFoundError, SecretsClient, _render_parameters
from nautobot_remote_jobs_sdk.secrets.providers import (
    EnvironmentVariableProvider,
    TextFileProvider,
)
from nautobot_remote_jobs_sdk.secrets.registry import (
    ProviderRegistry,
    SecretProvider,
    SecretResolutionError,
    UnknownProviderError,
    default_registry,
)

# -- providers ---------------------------------------------------------------


def test_environment_variable_provider(monkeypatch):
    monkeypatch.setenv("MY_SECRET_VAR", "env-secret-value")
    provider = EnvironmentVariableProvider()
    assert provider.resolve({"variable": "MY_SECRET_VAR"}) == "env-secret-value"


def test_environment_variable_provider_missing_var(monkeypatch):
    monkeypatch.delenv("NOT_SET_VAR", raising=False)
    provider = EnvironmentVariableProvider()
    with pytest.raises(SecretResolutionError, match="NOT_SET_VAR"):
        provider.resolve({"variable": "NOT_SET_VAR"})


def test_environment_variable_provider_missing_parameter():
    with pytest.raises(SecretResolutionError, match="variable"):
        EnvironmentVariableProvider().resolve({})


def test_text_file_provider(tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("  file-secret-value\n")
    provider = TextFileProvider()
    # Whitespace stripped, matching core Nautobot behavior.
    assert provider.resolve({"path": str(secret_file)}) == "file-secret-value"


def test_text_file_provider_missing_file(tmp_path):
    provider = TextFileProvider()
    with pytest.raises(SecretResolutionError, match="secret_mounts"):
        provider.resolve({"path": str(tmp_path / "nope.txt")})


def test_text_file_provider_missing_parameter():
    with pytest.raises(SecretResolutionError, match="path"):
        TextFileProvider().resolve({})


# -- registry ----------------------------------------------------------------


def test_default_registry_has_builtin_slugs():
    for slug in ("environment-variable", "text-file", "hashicorp-vault"):
        assert default_registry.get(slug).slug == slug


def test_unknown_provider_raises():
    registry = ProviderRegistry()
    with pytest.raises(UnknownProviderError, match="does-not-exist"):
        registry.get("does-not-exist")


def test_custom_provider_registration():
    class StaticProvider(SecretProvider):
        slug = "static"

        def resolve(self, parameters):
            return parameters["value"]

    registry = ProviderRegistry()
    registry.register(StaticProvider())
    assert registry.get("static").resolve({"value": "x"}) == "x"


# -- sandboxed Jinja rendering -------------------------------------------------


def test_render_parameters_with_obj():
    parameters = {"variable": "PASS_{{ obj.name | upper }}", "static": "unchanged"}
    rendered = _render_parameters(parameters, obj={"name": "dfw-rtr-01"})
    assert rendered == {"variable": "PASS_DFW-RTR-01", "static": "unchanged"}


def test_render_parameters_non_string_passthrough():
    rendered = _render_parameters({"kv_version": 2, "path": "plain"})
    assert rendered == {"kv_version": 2, "path": "plain"}


def test_render_parameters_is_sandboxed():
    hostile = {"variable": "{{ obj.__class__.__mro__ }}"}
    with pytest.raises(SecurityError):
        _render_parameters(hostile, obj=object())


# -- SecretsClient end-to-end (offline) ---------------------------------------


GROUP_ID = "11111111-1111-1111-1111-111111111111"
SECRET_ID = "22222222-2222-2222-2222-222222222222"


def _wire_directory(session: FakeSession, provider: str, parameters: dict) -> None:
    session.add(
        "GET",
        "/api/extras/secrets-groups/",
        FakeResponse(json_data={"results": [{"id": GROUP_ID, "name": "tacacs-prod"}]}),
    )
    session.add(
        "GET",
        "/api/extras/secrets-groups-associations/",
        FakeResponse(json_data={"results": [{"id": "a1", "secret": {"id": SECRET_ID}}]}),
    )
    session.add(
        "GET",
        f"/api/extras/secrets/{SECRET_ID}/",
        FakeResponse(json_data={"id": SECRET_ID, "provider": provider, "parameters": parameters}),
    )


def test_secrets_client_resolves_and_redacts(monkeypatch, fake_session):
    monkeypatch.setenv("TACACS_PASSWORD", "tacacs-secret-99")
    _wire_directory(fake_session, "environment-variable", {"variable": "TACACS_PASSWORD"})

    client = SecretsClient(fake_session, "https://nautobot.example.com")
    value = client.get("tacacs-prod", access_type="Generic", secret_type="password")

    assert value == "tacacs-secret-99"
    # Resolved value must be registered with the redactor.
    assert redaction.redact("x tacacs-secret-99 y") == f"x {redaction.MASK} y"

    # Association lookup carried the filters.
    (_, _, kwargs) = fake_session.calls_for("GET", "secrets-groups-associations")[0]
    assert kwargs["params"] == {
        "secrets_group": GROUP_ID,
        "access_type": "Generic",
        "secret_type": "password",
    }


def test_secrets_client_renders_jinja_with_obj(monkeypatch, fake_session):
    monkeypatch.setenv("PASS_DFW_RTR_01", "rendered-secret-1")
    _wire_directory(
        fake_session,
        "environment-variable",
        {"variable": "PASS_{{ obj.name | upper | replace('-', '_') }}"},
    )
    client = SecretsClient(fake_session, "https://nautobot.example.com")
    value = client.get("tacacs-prod", obj={"name": "dfw-rtr-01"})
    assert value == "rendered-secret-1"


def test_secrets_client_group_not_found(fake_session):
    fake_session.add("GET", "/api/extras/secrets-groups/", FakeResponse(json_data={"results": []}))
    client = SecretsClient(fake_session, "https://nautobot.example.com")
    with pytest.raises(SecretNotFoundError, match="missing-group"):
        client.get("missing-group")
