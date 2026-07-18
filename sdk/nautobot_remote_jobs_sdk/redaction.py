"""Module-level secret redaction registry.

Every secret value resolved by :mod:`nautobot_remote_jobs_sdk.secrets` is
registered here. Any text emitted through :class:`JobLogger` or through the
stdout/stderr proxies installed by ``@job.main`` passes through
:func:`redact` first, so secret values never leave the job process in
plaintext (SPEC section 9, step 5).
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Iterable, List, Set, TextIO

MASK = "(redacted)"

#: Values shorter than this are never registered -- masking one- or
#: two-character strings would mangle almost any output.
MIN_LENGTH = 4

_lock = threading.Lock()
_values: Set[str] = set()


def register(value: Any) -> None:
    """Register a secret value with the redactor.

    Non-string values are coerced with ``str()``. Values shorter than
    :data:`MIN_LENGTH` characters are ignored.
    """
    if value is None:
        return
    text = value if isinstance(value, str) else str(value)
    if len(text) < MIN_LENGTH:
        return
    with _lock:
        _values.add(text)


def register_many(values: Iterable[Any]) -> None:
    """Register several secret values at once."""
    for value in values:
        register(value)


def clear() -> None:
    """Forget all registered values (primarily for tests)."""
    with _lock:
        _values.clear()


def registered_values() -> List[str]:
    """Return a snapshot of registered values, longest first.

    Longest-first ordering guarantees that when one secret is a substring
    of another, the longer one is masked as a unit.
    """
    with _lock:
        return sorted(_values, key=len, reverse=True)


def redact(text: str) -> str:
    """Return *text* with every registered secret value replaced by ``(redacted)``."""
    if not isinstance(text, str):
        text = str(text)
    for value in registered_values():
        if value in text:
            text = text.replace(value, MASK)
    return text


class RedactingStream:
    """A ``TextIO`` proxy that redacts registered secrets on ``write()``.

    Installed over ``sys.stdout`` / ``sys.stderr`` by ``@job.main`` so plain
    ``print()`` calls (captured as console output by the worker agent) are
    masked as well.
    """

    def __init__(self, underlying: TextIO) -> None:
        self._underlying = underlying

    def write(self, s: str) -> int:
        return self._underlying.write(redact(s))

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._underlying.flush()

    @property
    def wrapped(self) -> TextIO:
        """The original stream underneath this proxy."""
        return self._underlying

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (encoding, isatty, fileno, ...) untouched.
        return getattr(self._underlying, name)


def install_stream_redaction() -> Callable[[], None]:
    """Wrap ``sys.stdout`` and ``sys.stderr`` in :class:`RedactingStream`.

    Returns a zero-argument callable that restores the original streams.
    Idempotent: already-wrapped streams are not wrapped twice.
    """
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    if not isinstance(sys.stdout, RedactingStream):
        sys.stdout = RedactingStream(sys.stdout)
    if not isinstance(sys.stderr, RedactingStream):
        sys.stderr = RedactingStream(sys.stderr)

    def restore() -> None:
        sys.stdout = original_stdout
        sys.stderr = original_stderr

    return restore
