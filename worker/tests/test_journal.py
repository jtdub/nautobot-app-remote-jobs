"""Tests for the in-flight run journal."""

import json

from remote_worker.journal import RunJournal


OFFER = {
    "run_id": "11111111-2222-4333-8444-555555555555",
    "definition": "rotate-local-admin",
    "image": "registry.example.com/jobs/rotate-admin@sha256:" + "ab" * 32,
    "inputs": {"devices": ["d1"], "dryrun": False},
    "timeout_seconds": 1800,
    "grace_seconds": 30,
    "token": "scoped-token",
    "env": {"REMOTE_JOBS_ZONE": "dfw-dc1"},
}


def test_add_get_roundtrip(tmp_path):
    journal = RunJournal(tmp_path / "journal")
    entry = journal.add(OFFER, started_at=1000.0)
    assert entry.run_id == OFFER["run_id"]
    loaded = journal.get(OFFER["run_id"])
    assert loaded is not None
    assert loaded.offer == OFFER
    assert loaded.started_at == 1000.0


def test_add_is_idempotent(tmp_path):
    journal = RunJournal(tmp_path)
    first = journal.add(OFFER, started_at=1.0)
    second = journal.add({**OFFER, "timeout_seconds": 99}, started_at=2.0)
    # Second add returns the original entry untouched.
    assert second.started_at == first.started_at
    assert second.offer["timeout_seconds"] == 1800


def test_survives_new_instance(tmp_path):
    RunJournal(tmp_path).add(OFFER)
    reopened = RunJournal(tmp_path)
    assert reopened.run_ids() == [OFFER["run_id"]]
    assert reopened.get(OFFER["run_id"]).offer["token"] == "scoped-token"


def test_remove(tmp_path):
    journal = RunJournal(tmp_path)
    journal.add(OFFER)
    journal.remove(OFFER["run_id"])
    assert journal.get(OFFER["run_id"]) is None
    assert journal.run_ids() == []
    journal.remove(OFFER["run_id"])  # no-op, no error


def test_load_all_sorted_and_skips_corrupt(tmp_path):
    journal = RunJournal(tmp_path)
    newer = {**OFFER, "run_id": "aaaaaaaa-0000-4000-8000-000000000002"}
    older = {**OFFER, "run_id": "bbbbbbbb-0000-4000-8000-000000000001"}
    journal.add(newer, started_at=200.0)
    journal.add(older, started_at=100.0)
    (tmp_path / "corrupt.json").write_text("{nope", encoding="utf-8")
    (tmp_path / "malformed.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")
    entries = journal.load_all()
    assert [e.run_id for e in entries] == [older["run_id"], newer["run_id"]]


def test_journal_file_permissions(tmp_path):
    journal = RunJournal(tmp_path)
    journal.add(OFFER)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert (files[0].stat().st_mode & 0o777) == 0o600
