"""Tests for the buffered log client and JobLogger."""

from __future__ import annotations

import io

import pytest

from conftest import FakeResponse, FakeSession
from nautobot_remote_jobs_sdk import redaction
from nautobot_remote_jobs_sdk.logging import LOG_LEVELS, JobLogger, LogClient

RUN_ID = "33333333-3333-3333-3333-333333333333"
LOGS_PATH = f"/api/plugins/remote-jobs/runs/{RUN_ID}/logs/"


def make_client(session: FakeSession, **kwargs) -> LogClient:
    session.add("POST", LOGS_PATH, FakeResponse(json_data={"ok": True}))
    return LogClient(
        session,
        "https://nautobot.example.com",
        RUN_ID,
        flush_interval=3600,  # keep time out of the picture
        **kwargs,
    )


def test_entry_count_threshold_triggers_flush(fake_session):
    client = make_client(fake_session, max_entries=3)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    logger.info("one")
    logger.info("two")
    assert fake_session.calls_for("POST", LOGS_PATH) == []
    logger.info("three")
    posts = fake_session.calls_for("POST", LOGS_PATH)
    assert len(posts) == 1
    payload = posts[0][2]["json"]
    assert payload["client_sequence"] == 1
    assert [e["message"] for e in payload["entries"]] == ["one", "two", "three"]


def test_byte_threshold_triggers_flush(fake_session):
    client = make_client(fake_session, max_bytes=200)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    logger.info("x" * 300)
    assert len(fake_session.calls_for("POST", LOGS_PATH)) == 1


def test_client_sequence_increments_per_batch(fake_session):
    client = make_client(fake_session, max_entries=1)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    logger.info("a")
    logger.info("b")
    sequences = [
        call[2]["json"]["client_sequence"] for call in fake_session.calls_for("POST", LOGS_PATH)
    ]
    assert sequences == [1, 2]


def test_explicit_flush_and_empty_flush(fake_session):
    client = make_client(fake_session)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    logger.warning("buffered", grouping="setup")
    client.flush()
    client.flush()  # empty flush must not POST
    posts = fake_session.calls_for("POST", LOGS_PATH)
    assert len(posts) == 1
    entry = posts[0][2]["json"]["entries"][0]
    assert entry["level"] == "warning"
    assert entry["grouping"] == "setup"
    assert "timestamp" in entry


def test_all_level_methods_and_echo(fake_session):
    client = make_client(fake_session)
    echo = io.StringIO()
    logger = JobLogger(client=client, echo_stream=echo)
    for level in LOG_LEVELS:
        getattr(logger, level)(f"msg-{level}", grouping="g")
    client.flush()
    entries = fake_session.calls_for("POST", LOGS_PATH)[0][2]["json"]["entries"]
    assert [e["level"] for e in entries] == list(LOG_LEVELS)
    out = echo.getvalue()
    for level in LOG_LEVELS:
        assert f"[{level.upper()}] g: msg-{level}" in out


def test_unknown_level_rejected():
    with pytest.raises(ValueError):
        JobLogger(echo_stream=io.StringIO()).log("verbose", "nope")


def test_logger_redacts_messages(fake_session):
    redaction.register("plaintext-password")
    client = make_client(fake_session)
    echo = io.StringIO()
    logger = JobLogger(client=client, echo_stream=echo)
    logger.error("the value is plaintext-password")
    client.flush()
    entries = fake_session.calls_for("POST", LOGS_PATH)[0][2]["json"]["entries"]
    assert entries[0]["message"] == f"the value is {redaction.MASK}"
    assert "plaintext-password" not in echo.getvalue()


def test_shipping_failure_does_not_raise(fake_session):
    fake_session.add("POST", LOGS_PATH, FakeResponse(status_code=500, json_data={}))
    client = LogClient(fake_session, "https://nautobot.example.com", RUN_ID, flush_interval=3600)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    logger.info("hello")
    client.flush()  # must swallow the HTTP 500


def test_start_stop_background_flusher(fake_session):
    client = make_client(fake_session)
    logger = JobLogger(client=client, echo_stream=io.StringIO())
    client.start()
    client.start()  # idempotent
    logger.info("late entry")
    client.stop()  # joins the thread and performs the final flush
    assert len(fake_session.calls_for("POST", LOGS_PATH)) == 1
