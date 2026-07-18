"""Tests for state persistence and digest-pinned image validation."""

import pytest

from remote_worker.runtime.base import ImageReferenceError, ensure_digest_reference
from remote_worker.state import StateStore, WorkerState


def test_state_roundtrip_and_permissions(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.load() is None
    store.save(WorkerState(worker_id="w-1", session_secret="s3cret"))
    loaded = store.load()
    assert loaded == WorkerState(worker_id="w-1", session_secret="s3cret")
    assert ((tmp_path / "state.json").stat().st_mode & 0o777) == 0o600


def test_state_corrupt_file_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError):
        StateStore(path).load()


def test_digest_reference_accepted():
    digest = "sha256:" + "ab" * 32
    ref = f"registry.example.com/jobs/rotate-admin@{digest}"
    assert ensure_digest_reference(ref) == digest


@pytest.mark.parametrize(
    "ref",
    [
        "registry.example.com/jobs/rotate-admin:1.4.0",
        "registry.example.com/jobs/rotate-admin",
        "registry.example.com/jobs/rotate-admin@sha256:short",
        "registry.example.com/jobs/rotate-admin@sha512:" + "ab" * 32,
        "registry.example.com/jobs/rotate-admin@sha256:" + "XY" * 32,
    ],
)
def test_non_digest_references_rejected(ref):
    with pytest.raises(ImageReferenceError):
        ensure_digest_reference(ref)
