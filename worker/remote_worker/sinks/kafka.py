"""Kafka log sink (SPEC 10.2) — optional, for scale.

Requires the ``kafka`` extra (``pip install "remote-worker[kafka]"``), which
installs :mod:`aiokafka`. Without it, constructing the sink raises
:class:`KafkaSinkUnavailableError` with installation instructions.

Topics: ``remote-jobs.logs`` and ``remote-jobs.console``; partition key is
``run_id`` (per-run ordering). Message value is JSON
``{run_id, sequence, entries}`` matching the HTTP batch schema. No tokens
and no secrets are ever written (redaction happens upstream of the sink).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

try:  # pragma: no cover - depends on optional extra
    from aiokafka import AIOKafkaProducer
except ImportError:  # pragma: no cover
    AIOKafkaProducer = None  # type: ignore[assignment]

from .base import KIND_CONSOLE, BatchingLogSink

logger = logging.getLogger(__name__)

TOPIC_LOGS = "remote-jobs.logs"
TOPIC_CONSOLE = "remote-jobs.console"

_INSTALL_HINT = (
    "the Kafka log sink requires the optional 'aiokafka' dependency; "
    'install the worker with the kafka extra: pip install "remote-worker[kafka]"'
)


class KafkaSinkUnavailableError(RuntimeError):
    """Raised when the Kafka sink is selected but aiokafka is not installed."""

    def __init__(self) -> None:
        super().__init__(_INSTALL_HINT)


class KafkaLogSink(BatchingLogSink):
    """Batched delivery to Kafka topics keyed by ``run_id``."""

    def __init__(
        self,
        bootstrap_servers: str,
        topic_logs: str = TOPIC_LOGS,
        topic_console: str = TOPIC_CONSOLE,
        clock: Callable[[], float] | None = None,
        **producer_kwargs: Any,
    ) -> None:
        if AIOKafkaProducer is None:
            raise KafkaSinkUnavailableError()
        super().__init__(clock=clock)
        self._topic_logs = topic_logs
        self._topic_console = topic_console
        self._producer = AIOKafkaProducer(
            bootstrap_servers=bootstrap_servers,
            acks="all",
            enable_idempotence=True,
            **producer_kwargs,
        )
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._producer.start()
            self._started = True
        await super().start()

    async def _send(self, run_id: str, kind: str, sequence: int, entries: list[dict[str, Any]]) -> None:
        if not self._started:
            await self._producer.start()
            self._started = True
        topic = self._topic_console if kind == KIND_CONSOLE else self._topic_logs
        value = json.dumps(
            {"run_id": run_id, "sequence": sequence, "entries": entries},
            default=str,
        ).encode("utf-8")
        await self._producer.send_and_wait(topic, value=value, key=run_id.encode("utf-8"))

    async def _close(self) -> None:
        if self._started:
            await self._producer.stop()
            self._started = False
