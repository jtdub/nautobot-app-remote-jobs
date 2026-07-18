"""Worker-local secrets resolution (``ctx.secrets``), SPEC section 9.

Nautobot is the *directory*: the SDK fetches the SecretsGroup, its
associations, and each Secret's ``provider`` slug + ``parameters`` via the
core REST API using the scoped per-job token. The *value* is then resolved
locally by a provider registry keyed by the same slugs core and
``nautobot-secrets-providers`` use. Secret values never transit Nautobot,
the gateway, or the control plane, and every resolved value is registered
with the redactor.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests
from jinja2.sandbox import SandboxedEnvironment

from .. import redaction
from ..http import join_url
from . import providers as _providers  # noqa: F401 - registers built-in providers
from .registry import (
    ProviderRegistry,
    SecretProvider,
    SecretResolutionError,
    SecretsError,
    UnknownProviderError,
    default_registry,
)

__all__ = [
    "SecretsClient",
    "SecretsError",
    "SecretNotFoundError",
    "SecretResolutionError",
    "SecretProvider",
    "ProviderRegistry",
    "UnknownProviderError",
    "default_registry",
]

logger = logging.getLogger(__name__)


class SecretNotFoundError(SecretsError):
    """The group, association, or secret does not exist (or is not visible)."""


def _render_parameters(parameters: Dict[str, Any], obj: Any = None) -> Dict[str, Any]:
    """Render Jinja2 templating in string parameters, sandboxed.

    Matches core ``Secret.rendered_parameters(obj)`` semantics: each string
    value containing template markers is rendered by a
    :class:`jinja2.sandbox.SandboxedEnvironment` with ``obj`` in context.
    Non-string values and plain strings pass through untouched.
    """
    environment = SandboxedEnvironment(autoescape=False)
    rendered: Dict[str, Any] = {}
    for key, value in parameters.items():
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            rendered[key] = environment.from_string(value).render(obj=obj)
        else:
            rendered[key] = value
    return rendered


class SecretsClient:
    """Fetch secret *definitions* from Nautobot; resolve *values* locally."""

    def __init__(
        self,
        session: requests.Session,
        nautobot_url: str,
        registry: Optional[ProviderRegistry] = None,
    ) -> None:
        self._session = session
        self._api_base = join_url(nautobot_url, "api")
        self._registry = registry if registry is not None else default_registry

    # -- REST directory lookups -------------------------------------------

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self._session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _lookup_group(self, group_name: str) -> Dict[str, Any]:
        payload = self._get_json(join_url(self._api_base, "extras/secrets-groups"), params={"name": group_name})
        results = payload.get("results", [])
        if not results:
            raise SecretNotFoundError(f"SecretsGroup {group_name!r} not found or not visible to this token")
        return results[0]

    def _lookup_association(self, group_id: str, access_type: str, secret_type: str) -> Dict[str, Any]:
        payload = self._get_json(
            join_url(self._api_base, "extras/secrets-groups-associations"),
            params={
                "secrets_group": group_id,
                "access_type": access_type,
                "secret_type": secret_type,
            },
        )
        results = payload.get("results", [])
        if not results:
            raise SecretNotFoundError(
                f"No secret with access_type={access_type!r} secret_type={secret_type!r} " f"in SecretsGroup {group_id}"
            )
        return results[0]

    def _lookup_secret(self, secret_ref: Any) -> Dict[str, Any]:
        if isinstance(secret_ref, dict):
            secret_id = secret_ref.get("id")
        else:
            secret_id = secret_ref
        return self._get_json(join_url(self._api_base, "extras/secrets", str(secret_id)))

    # -- public surface ----------------------------------------------------

    def get(
        self,
        group: str,
        access_type: str = "Generic",
        secret_type: str = "password",  # noqa: S107 - type selector, not a credential
        obj: Any = None,
    ) -> str:
        """Resolve one secret value from *group* (SPEC 9 flow).

        Args:
            group: SecretsGroup name.
            access_type: Association access type (e.g. ``Generic``, ``SSH``,
                ``HTTP(S)``); matches Nautobot ``SecretsGroupAccessTypeChoices``.
            secret_type: Association secret type (e.g. ``password``,
                ``username``, ``token``, ``key``).
            obj: Optional context object (e.g. a device record fetched via
                the API) made available as ``obj`` when the secret's
                parameters contain Jinja2 templating.

        Returns:
            The resolved secret value. The value is held in memory only and
            is registered with the redactor before being returned.
        """
        group_record = self._lookup_group(group)
        association = self._lookup_association(group_record["id"], access_type, secret_type)
        secret = self._lookup_secret(association.get("secret"))

        provider_slug = secret.get("provider", "")
        parameters = secret.get("parameters") or {}
        rendered = _render_parameters(parameters, obj=obj)

        provider = self._registry.get(provider_slug)
        value = provider.resolve(rendered)

        redaction.register(value)
        logger.debug(
            "Resolved secret from group=%r access_type=%r secret_type=%r provider=%r",
            group,
            access_type,
            secret_type,
            provider_slug,
        )
        return value
