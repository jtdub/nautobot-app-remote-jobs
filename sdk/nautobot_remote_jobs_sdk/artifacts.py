"""Artifact upload client (``ctx.artifacts``).

Upload protocol (SPEC 5 / 4.8):

1. ``POST /api/plugins/remote-jobs/runs/{id}/artifacts/`` with
   ``{name, content_type, size_bytes}`` -> ``{upload_url, artifact_id}``.
2. ``PUT`` the file bytes to ``upload_url`` (presigned; no Nautobot auth).
3. ``PUT /api/plugins/remote-jobs/runs/{id}/artifacts/{artifact_id}/complete/``
   with the file's SHA-256.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Optional

import requests

from .http import join_url

_CHUNK_SIZE = 1024 * 1024


def _sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactsClient:
    """Uploads run artifacts through the app's presigned-URL flow."""

    def __init__(self, session: requests.Session, nautobot_url: str, run_id: str) -> None:
        self._session = session
        self._base = join_url(nautobot_url, "api/plugins/remote-jobs/runs", run_id, "artifacts")

    def upload(
        self,
        path: str,
        name: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> str:
        """Upload the file at *path* and return the created ``artifact_id``.

        Args:
            path: Local filesystem path of the artifact.
            name: Artifact name; defaults to the file's basename.
            content_type: MIME type; guessed from the filename when omitted.
        """
        name = name or os.path.basename(path)
        content_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        size_bytes = os.path.getsize(path)
        sha256 = _sha256_of_file(path)

        response = self._session.post(
            self._base,
            json={"name": name, "content_type": content_type, "size_bytes": size_bytes},
            timeout=30,
        )
        response.raise_for_status()
        info = response.json()
        upload_url = info["upload_url"]
        artifact_id = info["artifact_id"]

        # The upload URL is presigned storage access -- deliberately NOT the
        # authenticated session (an Authorization header breaks S3-style
        # presigned PUTs).
        with open(path, "rb") as handle:
            put_response = requests.put(
                upload_url,
                data=handle,
                headers={"Content-Type": content_type},
                timeout=600,
            )
        put_response.raise_for_status()

        complete_url = join_url(self._base, str(artifact_id), "complete")
        complete = self._session.put(complete_url, json={"sha256": sha256}, timeout=30)
        complete.raise_for_status()
        return str(artifact_id)
