"""Entrypoint for the ``remote-worker`` console script."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from . import AGENT_VERSION
from .agent import WorkerAgent
from .config import ConfigError, WorkerConfig
from .enroll import EnrollmentError, enroll
from .journal import RunJournal
from .runtime.docker import DockerRuntime
from .state import StateStore

logger = logging.getLogger("remote_worker")


def _setup_logging() -> None:
    level_name = os.environ.get("REMOTE_WORKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _async_main() -> int:
    config = WorkerConfig.load()
    config.validate()

    store = StateStore(config.state_file)
    state = store.load()
    if state is None:
        # First boot: exchange the enrollment token for a session identity
        # (SPEC 7.1), persist it, and never look at the token again.
        state = await enroll(config)
        store.save(state)
        config.enroll_token = None

    journal = RunJournal(config.journal_dir)
    runtime = DockerRuntime(os.environ.get("DOCKER_HOST") or None)
    agent = WorkerAgent(config, state, store, journal, runtime)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platforms
            pass

    logger.info("starting %s as worker %s", AGENT_VERSION, state.worker_id)
    agent_task = asyncio.create_task(agent.run(), name="agent")
    stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
    done, _pending = await asyncio.wait(
        {agent_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        logger.info("termination signal received")
    stop_task.cancel()
    await agent.shutdown()
    agent_task.cancel()
    try:
        await agent_task
    except (asyncio.CancelledError, Exception):
        pass
    return 0


def main() -> int:
    """Console-script entrypoint."""
    _setup_logging()
    try:
        return asyncio.run(_async_main())
    except KeyboardInterrupt:
        return 130
    except (ConfigError, EnrollmentError) as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
