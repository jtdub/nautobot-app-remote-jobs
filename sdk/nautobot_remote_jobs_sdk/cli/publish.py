"""``remote-jobs publish``: manifest -> JobDefinition upsert (SPEC section 14).

Flow:

1. Resolve the image tag to a digest via the OCI registry HTTP API v2
   (``HEAD /v2/{repo}/manifests/{tag}``, ``Docker-Content-Digest`` header),
   handling anonymous/basic/bearer auth including the Docker Hub token flow.
   ``--digest`` skips resolution entirely.
2. Read the ``remote-job.yaml`` manifest.
3. Upsert the JobDefinition through the app REST API
   (``/api/plugins/remote-jobs/job-definitions/``): find by name, then
   PATCH the existing record or POST a new one.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

from ..http import build_session, join_url

#: A valid pinned image digest (SPEC 4.1 validation rule).
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Accept header covering docker v2 schema 2, manifest lists, and OCI types.
MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

#: Manifest keys copied verbatim into the JobDefinition payload when present.
_PASSTHROUGH_FIELDS = (
    "description",
    "zone_policy",
    "capabilities",
    "timeout_seconds",
    "grace_seconds",
    "dryrun_supported",
    "requires_zone_local",
)


class PublishError(RuntimeError):
    """A fatal error in the publish flow, reported to the user."""


# ---------------------------------------------------------------------------
# Image reference parsing
# ---------------------------------------------------------------------------


def parse_image_ref(image: str) -> Tuple[str, str, str, Optional[str]]:
    """Split an image reference into ``(registry, repository, tag, digest)``.

    Follows docker reference conventions: the first path component is a
    registry host only if it contains ``.`` or ``:`` or is ``localhost``;
    bare Docker Hub images gain the ``library/`` namespace.

    >>> parse_image_ref("registry.example.com/jobs/rotate-admin:1.4.0")
    ('registry.example.com', 'jobs/rotate-admin', '1.4.0', None)
    >>> parse_image_ref("alpine")
    ('docker.io', 'library/alpine', 'latest', None)
    """
    digest: Optional[str] = None
    if "@" in image:
        image, digest = image.rsplit("@", 1)

    parts = image.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry = parts[0]
        remainder = "/".join(parts[1:])
    else:
        registry = "docker.io"
        remainder = image
        if "/" not in remainder:
            remainder = f"library/{remainder}"

    if ":" in remainder.rsplit("/", 1)[-1]:
        repository, tag = remainder.rsplit(":", 1)
    else:
        repository, tag = remainder, "latest"
    return registry, repository, tag, digest


# ---------------------------------------------------------------------------
# Digest resolution (registry API v2)
# ---------------------------------------------------------------------------


def _parse_www_authenticate(header: str) -> Dict[str, str]:
    """Parse a ``WWW-Authenticate: Bearer realm="...",service="..."`` header."""
    fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
    fields["scheme"] = header.split(" ", 1)[0].strip().lower()
    return fields


def _fetch_bearer_token(
    challenge: Dict[str, str],
    repository: str,
    auth: Optional[Tuple[str, str]],
    session: requests.Session,
) -> str:
    """Fetch a bearer token per the challenge (anonymous Docker Hub flow included)."""
    realm = challenge.get("realm")
    if not realm:
        raise PublishError("Registry bearer challenge is missing the realm")
    params: Dict[str, str] = {"scope": challenge.get("scope") or f"repository:{repository}:pull"}
    if challenge.get("service"):
        params["service"] = challenge["service"]
    response = session.get(realm, params=params, auth=auth, timeout=30)
    if response.status_code != 200:
        raise PublishError(f"Registry token request failed: HTTP {response.status_code}")
    payload = response.json()
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise PublishError("Registry token endpoint returned no token")
    return str(token)


def resolve_digest(
    image: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> str:
    """Resolve an image tag to its ``sha256:...`` manifest digest.

    Sends ``HEAD /v2/{repository}/manifests/{tag}`` and reads the
    ``Docker-Content-Digest`` response header. On a 401 bearer challenge,
    obtains a token first (with basic credentials when provided, anonymously
    otherwise -- the Docker Hub public flow).
    """
    registry, repository, tag, digest = parse_image_ref(image)
    if digest:
        if not DIGEST_RE.match(digest):
            raise PublishError(f"Image reference carries an invalid digest: {digest!r}")
        return digest

    host = "registry-1.docker.io" if registry in ("docker.io", "index.docker.io") else registry
    manifest_url = f"https://{host}/v2/{repository}/manifests/{tag}"
    basic_auth = (username, password) if username and password else None
    http = session if session is not None else requests.Session()
    headers = {"Accept": MANIFEST_ACCEPT}

    response = http.head(manifest_url, headers=headers, auth=basic_auth, timeout=30)
    if response.status_code == 401:
        challenge = _parse_www_authenticate(response.headers.get("WWW-Authenticate", ""))
        if challenge.get("scheme") == "bearer":
            token = _fetch_bearer_token(challenge, repository, basic_auth, http)
            headers["Authorization"] = f"Bearer {token}"
            response = http.head(manifest_url, headers=headers, timeout=30)
        elif basic_auth is None:
            raise PublishError(f"Registry {host} requires credentials " "(--registry-username/--registry-password)")
    if response.status_code != 200:
        raise PublishError(
            f"Could not resolve manifest for {repository}:{tag} on {host}: " f"HTTP {response.status_code}"
        )

    resolved = response.headers.get("Docker-Content-Digest", "")
    if not DIGEST_RE.match(resolved):
        raise PublishError(
            f"Registry returned no usable Docker-Content-Digest for {repository}:{tag} "
            f"(got {resolved!r}); pass --digest explicitly"
        )
    return resolved


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------


def load_manifest(path: str) -> Dict[str, Any]:
    """Load and minimally validate a ``remote-job.yaml`` manifest."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = yaml.safe_load(handle)
    except OSError as exc:
        raise PublishError(f"Cannot read manifest {path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PublishError(f"Manifest {path!r} is not valid YAML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PublishError(f"Manifest {path!r} must be a YAML mapping")
    if not manifest.get("name"):
        raise PublishError(f"Manifest {path!r} is missing the required 'name' field")
    return manifest


def build_payload(manifest: Dict[str, Any], image: str, digest: str) -> Dict[str, Any]:
    """Map a manifest onto a JobDefinition REST payload.

    The manifest's ``inputs`` key (JSON Schema) becomes ``input_schema``;
    ``secrets_groups`` names become natural-key references.
    """
    if not DIGEST_RE.match(digest):
        raise PublishError(f"Invalid image digest {digest!r} (expected sha256:<64 hex chars>)")

    # Strip any tag/digest from the stored image reference (SPEC 4.1: the
    # `image` field is the OCI reference; dispatch always sends image@digest).
    image_ref = image.rsplit("@", 1)[0]

    payload: Dict[str, Any] = {
        "name": manifest["name"],
        "image": image_ref,
        "image_digest": digest,
    }
    for field in _PASSTHROUGH_FIELDS:
        if field in manifest:
            payload[field] = manifest[field]
    if "inputs" in manifest:
        payload["input_schema"] = manifest["inputs"]
    if "secrets_groups" in manifest:
        payload["secrets_groups"] = [{"name": name} for name in manifest["secrets_groups"]]
    return payload


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def upsert_job_definition(
    session: requests.Session, nautobot_url: str, payload: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Create or update the JobDefinition; returns ``("created"|"updated", record)``."""
    endpoint = join_url(nautobot_url, "api/plugins/remote-jobs/job-definitions")

    response = session.get(endpoint, params={"name": payload["name"]}, timeout=30)
    response.raise_for_status()
    results = response.json().get("results", [])

    if results:
        existing_id = results[0]["id"]
        response = session.patch(join_url(endpoint, str(existing_id)), json=payload, timeout=30)
        response.raise_for_status()
        return "updated", response.json()

    response = session.post(endpoint, json=payload, timeout=30)
    response.raise_for_status()
    return "created", response.json()


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def add_parser(subparsers: "argparse._SubParsersAction") -> argparse.ArgumentParser:
    """Register the ``publish`` subcommand."""
    parser = subparsers.add_parser(
        "publish",
        help="Resolve an image digest and upsert a JobDefinition from remote-job.yaml",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Image reference incl. tag, e.g. registry.example.com/jobs/rotate-admin:1.4.0",
    )
    parser.add_argument(
        "--manifest",
        default="remote-job.yaml",
        help="Path to the job manifest (default: %(default)s)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Nautobot URL (default: $NAUTOBOT_URL)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Nautobot API token (default: $NAUTOBOT_TOKEN)",
    )
    parser.add_argument(
        "--digest",
        default=None,
        help="Skip registry resolution and use this sha256:... digest",
    )
    parser.add_argument(
        "--registry-username",
        default=os.environ.get("REGISTRY_USERNAME"),
        help="Registry username (default: $REGISTRY_USERNAME)",
    )
    parser.add_argument(
        "--registry-password",
        default=os.environ.get("REGISTRY_PASSWORD"),
        help="Registry password/token (default: $REGISTRY_PASSWORD)",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the publish subcommand."""
    nautobot_url = args.url or os.environ.get("NAUTOBOT_URL")
    token = args.token or os.environ.get("NAUTOBOT_TOKEN")
    if not nautobot_url:
        raise PublishError("No Nautobot URL: pass --url or set NAUTOBOT_URL")
    if not token:
        raise PublishError("No API token: pass --token or set NAUTOBOT_TOKEN")

    manifest = load_manifest(args.manifest)

    if args.digest:
        digest = args.digest
        if not DIGEST_RE.match(digest):
            raise PublishError(f"--digest {digest!r} does not match sha256:<64 hex chars>")
    else:
        digest = resolve_digest(args.image, username=args.registry_username, password=args.registry_password)
        print(f"Resolved {args.image} -> {digest}")

    payload = build_payload(manifest, args.image, digest)
    session = build_session(token=token)
    try:
        action, record = upsert_job_definition(session, nautobot_url, payload)
    except requests.HTTPError as exc:
        detail = ""
        if exc.response is not None:
            detail = f": {exc.response.text[:500]}"
        raise PublishError(f"JobDefinition upsert failed ({exc}){detail}") from exc

    print(f"JobDefinition {payload['name']!r} {action} (id={record.get('id', 'unknown')})")
    return 0
