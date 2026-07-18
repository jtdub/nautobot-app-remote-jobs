"""Tests for the module-level redactor and stream proxies."""

from __future__ import annotations

import io
import sys

from nautobot_remote_jobs_sdk import redaction


def test_redact_masks_registered_values():
    redaction.register("hunter2secret")
    assert redaction.redact("the password is hunter2secret!") == (
        f"the password is {redaction.MASK}!"
    )


def test_redact_ignores_unregistered_text():
    assert redaction.redact("nothing to hide") == "nothing to hide"


def test_short_values_not_registered():
    redaction.register("ab")
    assert redaction.redact("ab") == "ab"


def test_none_ignored():
    redaction.register(None)
    assert redaction.redact("None") == "None"


def test_non_string_values_coerced():
    redaction.register(123456789)
    assert redaction.redact("pin is 123456789 ok") == f"pin is {redaction.MASK} ok"


def test_longest_value_masked_first():
    redaction.register("secretvalue")
    redaction.register("secretvalue-extended")
    out = redaction.redact("x secretvalue-extended y")
    assert out == f"x {redaction.MASK} y"


def test_multiple_occurrences_and_values():
    redaction.register("alphaalpha")
    redaction.register("betabeta")
    text = "alphaalpha betabeta alphaalpha"
    assert redaction.redact(text) == f"{redaction.MASK} {redaction.MASK} {redaction.MASK}"


def test_redacting_stream_masks_writes():
    buffer = io.StringIO()
    stream = redaction.RedactingStream(buffer)
    redaction.register("tokenvalue123")
    print("my token is tokenvalue123", file=stream)
    assert buffer.getvalue() == f"my token is {redaction.MASK}\n"


def test_redacting_stream_delegates_attrs():
    buffer = io.StringIO()
    stream = redaction.RedactingStream(buffer)
    stream.flush()  # must not raise
    assert stream.wrapped is buffer


def test_install_stream_redaction_and_restore(capsys):
    original_stdout = sys.stdout
    restore = redaction.install_stream_redaction()
    try:
        assert isinstance(sys.stdout, redaction.RedactingStream)
        assert isinstance(sys.stderr, redaction.RedactingStream)
        # Idempotent: installing again must not double-wrap.
        redaction.install_stream_redaction()
        assert not isinstance(sys.stdout.wrapped, redaction.RedactingStream)
        redaction.register("supersecretvalue")
        print("value: supersecretvalue")
    finally:
        restore()
    assert sys.stdout is original_stdout
    captured = capsys.readouterr()
    assert "supersecretvalue" not in captured.out
    assert redaction.MASK in captured.out
