"""In-flight run journal.

Every accepted job offer is journaled to the state volume before the
container is created and removed only after ``job.complete`` has been
acknowledged. After a restart the journal drives two things:

- the ``in_flight`` run-id list sent in ``worker.hello`` so the server can
  reconcile runs that survived the restart (SPEC 8.1), and
- local reconciliation: re-attaching to still-running containers or
  reporting runs whose containers are gone.

One JSON file per run, mode 0600 (the offer contains the scoped API token).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class JournalEntry:
    """A journaled in-flight run."""

    run_id: str
    offer: dict[str, Any]
    started_at: float


class RunJournal:
    """Directory-backed journal of in-flight runs."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        # run ids are UUIDs; sanitize defensively anyway.
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.json"

    def add(self, offer: dict[str, Any], started_at: float | None = None) -> JournalEntry:
        """Journal *offer*; idempotent (an existing entry is returned as-is)."""
        run_id = str(offer["run_id"])
        existing = self.get(run_id)
        if existing is not None:
            return existing
        entry = JournalEntry(
            run_id=run_id,
            offer=offer,
            started_at=started_at if started_at is not None else time.time(),
        )
        payload = json.dumps(
            {"run_id": entry.run_id, "offer": entry.offer, "started_at": entry.started_at},
            default=str,
        )
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), prefix=".run-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path(run_id))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return entry

    def get(self, run_id: str) -> JournalEntry | None:
        """Load one entry, or ``None``."""
        path = self._path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("unreadable journal entry %s: %s", path, exc)
            return None
        return self._entry_from(data)

    @staticmethod
    def _entry_from(data: dict[str, Any]) -> JournalEntry | None:
        try:
            return JournalEntry(
                run_id=str(data["run_id"]),
                offer=dict(data["offer"]),
                started_at=float(data.get("started_at", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def load_all(self) -> list[JournalEntry]:
        """All journaled runs, oldest first. Corrupt files are skipped."""
        entries: list[JournalEntry] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("skipping corrupt journal entry %s: %s", path, exc)
                continue
            entry = self._entry_from(data)
            if entry is None:
                logger.warning("skipping malformed journal entry %s", path)
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: entry.started_at)
        return entries

    def run_ids(self) -> list[str]:
        """Run ids of all journaled runs."""
        return [entry.run_id for entry in self.load_all()]

    def remove(self, run_id: str) -> None:
        """Drop the journal entry for *run_id* (no-op when absent)."""
        try:
            self._path(run_id).unlink()
        except FileNotFoundError:
            pass
