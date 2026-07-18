"""Built-in local secret providers (SPEC section 9).

These use the same slugs and ``parameters`` shapes as Nautobot core and
``nautobot-secrets-providers``, but resolve in the *job container's* context:
``environment-variable`` reads the container environment (populated through
the agent's ``pass_env`` allowlist), ``text-file`` reads paths mounted via
``secret_mounts``, and ``hashicorp-vault`` talks to the zone-local Vault.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from .registry import SecretProvider, SecretResolutionError, default_registry


class EnvironmentVariableProvider(SecretProvider):
    """Slug ``environment-variable``; parameters ``{"variable": "NAME"}``."""

    slug = "environment-variable"

    def resolve(self, parameters: Dict[str, Any]) -> str:
        variable = parameters.get("variable")
        if not variable:
            raise SecretResolutionError(
                "environment-variable secret is missing the 'variable' parameter"
            )
        try:
            return os.environ[variable]
        except KeyError:
            raise SecretResolutionError(
                f"Environment variable {variable!r} is not set in the job container. "
                "Check the worker agent's pass_env allowlist."
            ) from None


class TextFileProvider(SecretProvider):
    """Slug ``text-file``; parameters ``{"path": "/run/secrets/..."}``.

    Like core Nautobot, surrounding whitespace (including the trailing
    newline most secret files carry) is stripped.
    """

    slug = "text-file"

    def resolve(self, parameters: Dict[str, Any]) -> str:
        path = parameters.get("path")
        if not path:
            raise SecretResolutionError("text-file secret is missing the 'path' parameter")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            raise SecretResolutionError(
                f"Unable to read secret file {path!r}: {exc}. "
                "Check the worker agent's secret_mounts configuration."
            ) from exc


class HashiCorpVaultProvider(SecretProvider):
    """Slug ``hashicorp-vault``; parameters ``{"path", "key", "mount_point", "kv_version"}``.

    Client configuration comes from the container environment (injected via
    the agent's ``pass_env`` allowlist):

    - ``VAULT_ADDR`` (required)
    - ``VAULT_TOKEN`` for token auth, or
    - ``VAULT_ROLE_ID`` + ``VAULT_SECRET_ID`` for AppRole auth.

    Requires the ``vault`` extra (``pip install nautobot-remote-jobs-sdk[vault]``).
    """

    slug = "hashicorp-vault"

    def _build_client(self) -> Any:
        try:
            import hvac  # noqa: PLC0415 - optional dependency
        except ImportError:
            raise SecretResolutionError(
                "hvac is not installed; install nautobot-remote-jobs-sdk[vault] "
                "to resolve hashicorp-vault secrets"
            ) from None

        address = os.environ.get("VAULT_ADDR")
        if not address:
            raise SecretResolutionError("VAULT_ADDR is not set in the job container")

        client = hvac.Client(url=address)
        token = os.environ.get("VAULT_TOKEN")
        role_id = os.environ.get("VAULT_ROLE_ID")
        secret_id = os.environ.get("VAULT_SECRET_ID")
        if token:
            client.token = token
        elif role_id and secret_id:
            client.auth.approle.login(role_id=role_id, secret_id=secret_id)
        else:
            raise SecretResolutionError(
                "No Vault credentials: set VAULT_TOKEN or VAULT_ROLE_ID + VAULT_SECRET_ID"
            )
        return client

    def resolve(self, parameters: Dict[str, Any]) -> str:
        path = parameters.get("path")
        key = parameters.get("key")
        if not path or not key:
            raise SecretResolutionError(
                "hashicorp-vault secret requires 'path' and 'key' parameters"
            )
        mount_point = parameters.get("mount_point", "secret")
        kv_version = str(parameters.get("kv_version", "v2")).lower()

        client = self._build_client()
        try:
            if kv_version in ("v2", "2"):
                response = client.secrets.kv.v2.read_secret_version(
                    path=path, mount_point=mount_point
                )
                data = response["data"]["data"]
            elif kv_version in ("v1", "1"):
                response = client.secrets.kv.v1.read_secret(path=path, mount_point=mount_point)
                data = response["data"]
            else:
                raise SecretResolutionError(f"Unsupported kv_version {kv_version!r}")
        except SecretResolutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize hvac errors
            raise SecretResolutionError(f"Vault read failed for path {path!r}: {exc}") from exc

        if key not in data:
            raise SecretResolutionError(f"Key {key!r} not present at Vault path {path!r}")
        return str(data[key])


def register_builtin_providers() -> None:
    """Register the built-in providers with the default registry."""
    default_registry.register(EnvironmentVariableProvider())
    default_registry.register(TextFileProvider())
    default_registry.register(HashiCorpVaultProvider())


register_builtin_providers()
