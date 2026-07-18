"""Tests for the @job.main decorator: env contract, exit codes, log flush."""

from __future__ import annotations

import json

import pytest
from conftest import FakeResponse
from nautobot_remote_jobs_sdk import job
from nautobot_remote_jobs_sdk.context import Context, ContextError, InputValidationError

RUN_ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("NAUTOBOT_URL", "https://nautobot.example.com")
    monkeypatch.setenv("NAUTOBOT_TOKEN", "scoped-token-value")
    monkeypatch.setenv("REMOTE_JOBS_RUN_ID", RUN_ID)
    monkeypatch.setenv("REMOTE_JOBS_ZONE", "dfw-dc1")
    monkeypatch.setenv("REMOTE_JOBS_DRYRUN", "false")
    monkeypatch.delenv("REMOTE_JOBS_INPUTS", raising=False)
    monkeypatch.delenv("REMOTE_JOBS_INPUT_SCHEMA", raising=False)


@pytest.fixture()
def wired_session(monkeypatch, fake_session):
    """Route all SDK HTTP through the fake session."""
    fake_session.add("POST", "/logs/", FakeResponse(json_data={"ok": True}))
    monkeypatch.setattr("nautobot_remote_jobs_sdk.http.build_session", lambda **kwargs: fake_session)
    return fake_session


# -- Context.from_env ----------------------------------------------------------


def test_from_env_happy_path(env, wired_session, monkeypatch):
    monkeypatch.setenv("REMOTE_JOBS_INPUTS", json.dumps({"devices": ["d1"], "commit": True}))
    ctx = Context.from_env()
    assert ctx.nautobot_url == "https://nautobot.example.com"
    assert ctx.run_id == RUN_ID
    assert ctx.zone == "dfw-dc1"
    assert ctx.dryrun is False
    assert ctx.inputs == {"devices": ["d1"], "commit": True}


def test_from_env_missing_required(env, wired_session, monkeypatch):
    monkeypatch.delenv("NAUTOBOT_TOKEN")
    with pytest.raises(ContextError, match="NAUTOBOT_TOKEN"):
        Context.from_env()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("YES", True), ("false", False), ("", False), ("0", False)],
)
def test_dryrun_parsing(env, wired_session, monkeypatch, raw, expected):
    monkeypatch.setenv("REMOTE_JOBS_DRYRUN", raw)
    assert Context.from_env().dryrun is expected


def test_inputs_from_file(env, wired_session, tmp_path):
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps({"devices": ["a", "b"]}))
    ctx = Context.from_env(inputs_file=str(inputs_file))
    assert ctx.inputs == {"devices": ["a", "b"]}


def test_env_inputs_win_over_file(env, wired_session, tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_JOBS_INPUTS", json.dumps({"source": "env"}))
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text(json.dumps({"source": "file"}))
    ctx = Context.from_env(inputs_file=str(inputs_file))
    assert ctx.inputs == {"source": "env"}


def test_invalid_inputs_json(env, wired_session, monkeypatch):
    monkeypatch.setenv("REMOTE_JOBS_INPUTS", "{not json")
    with pytest.raises(ContextError, match="not valid JSON"):
        Context.from_env()


def test_input_schema_validation_pass(env, wired_session, monkeypatch):
    monkeypatch.setenv("REMOTE_JOBS_INPUTS", json.dumps({"devices": ["u1"]}))
    monkeypatch.setenv(
        "REMOTE_JOBS_INPUT_SCHEMA",
        json.dumps(
            {
                "type": "object",
                "required": ["devices"],
                "properties": {"devices": {"type": "array"}},
            }
        ),
    )
    assert Context.from_env().inputs == {"devices": ["u1"]}


def test_input_schema_validation_failure(env, wired_session, monkeypatch):
    monkeypatch.setenv("REMOTE_JOBS_INPUTS", json.dumps({}))
    monkeypatch.setenv(
        "REMOTE_JOBS_INPUT_SCHEMA",
        json.dumps({"type": "object", "required": ["devices"]}),
    )
    with pytest.raises(InputValidationError):
        Context.from_env()


# -- @job.main exit codes --------------------------------------------------------


def test_main_exits_zero_on_success(env, wired_session):
    @job.main
    def run(ctx):
        ctx.logger.info("hello from the job")

    with pytest.raises(SystemExit) as excinfo:
        run()
    assert excinfo.value.code == 0

    # Final flush shipped the buffered entries with a client_sequence.
    posts = wired_session.calls_for("POST", "/logs/")
    assert posts, "expected at least one flushed log batch"
    messages = [entry["message"] for _, _, kwargs in posts for entry in kwargs["json"]["entries"]]
    assert "hello from the job" in messages


def test_main_exits_one_on_exception(env, wired_session, capsys):
    @job.main
    def run(ctx):
        raise RuntimeError("boom")

    with pytest.raises(SystemExit) as excinfo:
        run()
    assert excinfo.value.code == 1

    posts = wired_session.calls_for("POST", "/logs/")
    entries = [entry for _, _, kwargs in posts for entry in kwargs["json"]["entries"]]
    assert any(e["level"] == "failure" and "boom" in e["message"] for e in entries)
    assert "RuntimeError" in capsys.readouterr().err  # traceback on stderr


def test_main_exits_one_on_bad_environment(monkeypatch, capsys):
    for var in ("NAUTOBOT_URL", "NAUTOBOT_TOKEN", "REMOTE_JOBS_RUN_ID"):
        monkeypatch.delenv(var, raising=False)

    @job.main
    def run(ctx):
        pass  # pragma: no cover - never reached

    with pytest.raises(SystemExit) as excinfo:
        run()
    assert excinfo.value.code == 1
    assert "Missing required environment variables" in capsys.readouterr().err


def test_main_honors_explicit_system_exit(env, wired_session):
    @job.main
    def run(ctx):
        raise SystemExit(3)

    with pytest.raises(SystemExit) as excinfo:
        run()
    assert excinfo.value.code == 3


def test_main_exposes_wrapped_function(env):
    def run(ctx):
        pass

    wrapped = job.main(run)
    assert wrapped.__wrapped_job__ is run


def test_stdout_is_redacted_inside_job(env, wired_session, capsys):
    from nautobot_remote_jobs_sdk import redaction

    @job.main
    def run(ctx):
        redaction.register("printedsecret42")
        print("value: printedsecret42")

    with pytest.raises(SystemExit):
        run()
    out = capsys.readouterr().out
    assert "printedsecret42" not in out
    assert redaction.MASK in out
