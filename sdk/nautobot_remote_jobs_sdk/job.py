"""The ``@job.main`` entry point decorator (SPEC section 13).

Usage inside a job container::

    from nautobot_remote_jobs_sdk import job

    @job.main
    def run(ctx):
        ctx.logger.info("Starting", grouping="setup")
        ...

The decorator:

1. parses the env contract and builds the :class:`~.context.Context`,
2. installs redacting proxies over ``sys.stdout``/``sys.stderr`` so plain
   ``print()`` output is masked too,
3. runs the wrapped function,
4. exits ``0`` on success and ``1`` on any exception (logged as a
   ``failure`` entry with traceback),
5. flushes all buffered logs before the process exits.

When the decorated function's module is executed as ``__main__`` (the normal
container entrypoint, ``python job.py``), the job runs immediately -- no
explicit call is needed. Set ``REMOTE_JOBS_SDK_NO_AUTORUN=1`` to suppress
this (useful for tooling that imports the entrypoint module).
"""

from __future__ import annotations

import functools
import os
import sys
import traceback
from typing import Any, Callable, NoReturn

from . import redaction
from .context import Context, ContextError

JobFunc = Callable[[Context], Any]

#: Environment variable that disables auto-execution under ``__main__``.
NO_AUTORUN_ENV = "REMOTE_JOBS_SDK_NO_AUTORUN"


def _execute(func: JobFunc) -> int:
    """Run *func* under the full lifecycle; return the process exit code."""
    try:
        ctx = Context.from_env()
    except ContextError as exc:
        print(f"remote-jobs-sdk: {exc}", file=sys.stderr)
        return 1

    restore_streams = redaction.install_stream_redaction()
    ctx._start()
    exit_code = 0
    try:
        func(ctx)
        ctx.logger.success("Job completed successfully", grouping="post_run")
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if exit_code != 0:
            ctx.logger.failure(f"Job exited with code {exit_code}", grouping="post_run")
    except BaseException as exc:  # noqa: BLE001 - anything else is a job failure
        exit_code = 1
        ctx.logger.failure(f"Job failed: {exc!r}", grouping="post_run")
        # Traceback goes to (redacted) stderr for the console capture.
        traceback.print_exc(file=sys.stderr)
    finally:
        try:
            ctx.close()  # final log flush
        finally:
            restore_streams()
    return exit_code


def main(func: JobFunc) -> Callable[[], NoReturn]:
    """Decorator wrapping a ``run(ctx)`` function into a job entry point.

    Returns a zero-argument callable that executes the job and calls
    :func:`sys.exit` with the resulting code. If the decorated function is
    defined in the ``__main__`` module, the job executes immediately at
    decoration time.
    """

    @functools.wraps(func)
    def entrypoint() -> NoReturn:
        sys.exit(_execute(func))

    entrypoint.__wrapped_job__ = func  # type: ignore[attr-defined]

    if func.__globals__.get("__name__") == "__main__" and not os.environ.get(NO_AUTORUN_ENV):
        entrypoint()

    return entrypoint
