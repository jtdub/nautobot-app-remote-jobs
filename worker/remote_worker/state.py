"""Persistent worker identity state.

After enrollment (SPEC 7.1) the agent persists ``{worker_id, session_secret}``
on the state volume so restarts do not require a fresh enrollment token.
The file is written atomically with mode 0600.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    """The credentials obtained at enrollment."""

    worker_id: str
    session_secret: str


class StateStore:
    """Loads and saves :class:`WorkerState` as JSON with restrictive perms."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """Location of the state file."""
        return self._path

    def load(self) -> WorkerState | None:
        """Return the persisted state, or ``None`` if not yet enrolled."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
            return WorkerState(
                worker_id=str(data["worker_id"]),
                session_secret=str(data["session_secret"]),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"corrupt worker state file {self._path}: {exc}. "
                "Remove it and re-enroll with a fresh enrollment token."
            ) from exc

    def save(self, state: WorkerState) -> None:
        """Atomically persist *state* with mode 0600."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"worker_id": state.worker_id, "session_secret": state.session_secret},
            indent=2,
        )
        fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".state-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        logger.info("persisted worker state to %s", self._path)
