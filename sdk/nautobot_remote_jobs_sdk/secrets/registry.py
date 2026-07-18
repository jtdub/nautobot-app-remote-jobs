"""Local secret provider registry, keyed by Nautobot provider slug.

Providers are pluggable through the ``nautobot_remote_jobs_sdk.secrets_providers``
entry point group: an entry point named after the provider slug pointing at a
class with a ``resolve(parameters)`` method (see :class:`SecretProvider`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

try:  # Python 3.10+ importlib.metadata with the group= keyword
    from importlib.metadata import entry_points
except ImportError:  # pragma: no cover
    entry_points = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "nautobot_remote_jobs_sdk.secrets_providers"


class SecretsError(Exception):
    """Base class for secret resolution failures."""


class UnknownProviderError(SecretsError):
    """No provider is registered for the requested slug."""


class SecretResolutionError(SecretsError):
    """A provider failed to produce a value (missing env var, file, key...)."""


class SecretProvider:
    """Base class / interface for local secret providers.

    Subclasses set :attr:`slug` to the Nautobot provider slug they implement
    and override :meth:`resolve`.
    """

    #: Nautobot ``Secret.provider`` slug this provider serves.
    slug: str = ""

    def resolve(self, parameters: Dict[str, Any]) -> str:
        """Resolve the (already Jinja-rendered) *parameters* to a secret value.

        Raises:
            SecretResolutionError: when the value cannot be produced.
        """
        raise NotImplementedError


class ProviderRegistry:
    """Maps provider slugs to :class:`SecretProvider` instances.

    Lookup order: explicitly registered providers first, then providers
    discovered from the entry point group (loaded lazily, once).
    """

    def __init__(self) -> None:
        self._providers: Dict[str, SecretProvider] = {}
        self._entry_points_loaded = False

    def register(self, provider: SecretProvider) -> None:
        """Register *provider* under its slug, replacing any previous one."""
        if not provider.slug:
            raise ValueError(f"{provider!r} has no slug")
        self._providers[provider.slug] = provider

    def get(self, slug: str) -> SecretProvider:
        """Return the provider for *slug* or raise :class:`UnknownProviderError`."""
        if slug not in self._providers:
            self._load_entry_points()
        try:
            return self._providers[slug]
        except KeyError:
            raise UnknownProviderError(
                f"No local secrets provider registered for slug {slug!r}. "
                f"Known slugs: {sorted(self._providers)}"
            ) from None

    def _load_entry_points(self) -> None:
        if self._entry_points_loaded or entry_points is None:
            return
        self._entry_points_loaded = True
        try:
            discovered = entry_points(group=ENTRY_POINT_GROUP)
        except TypeError:  # pragma: no cover - very old importlib.metadata
            discovered = entry_points().get(ENTRY_POINT_GROUP, [])  # type: ignore[call-arg]
        for entry_point in discovered:
            if entry_point.name in self._providers:
                continue  # explicit registrations win
            try:
                provider_class = entry_point.load()
                provider = provider_class()
                if not getattr(provider, "slug", ""):
                    provider.slug = entry_point.name
                self._providers[provider.slug] = provider
            except Exception as exc:  # noqa: BLE001 - a broken plugin must not kill the job
                logger.warning("Failed to load secrets provider %r: %s", entry_point.name, exc)


#: Process-wide default registry; built-in providers register themselves here
#: (see :mod:`nautobot_remote_jobs_sdk.secrets.providers`).
default_registry = ProviderRegistry()
