"""Log sinks: batching interface plus HTTP (default) and Kafka backends.

Sink selection comes from ``log_sink_config`` in the ``worker.hello`` result
(SPEC 10); a local override in the agent config wins over the server value.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from .base import BatchingLogSink, LogSink

logger = logging.getLogger(__name__)

__all__ = ["LogSink", "BatchingLogSink", "create_sink"]


def create_sink(
    sink_config: dict[str, Any] | None,
    *,
    nautobot_url: str,
    worker_id: str,
    secret_provider: Callable[[], str],
    tls_verify: bool = True,
) -> LogSink:
    """Instantiate a sink from an effective ``log_sink_config`` mapping.

    ``{"type": "http", "url": ...?}`` or
    ``{"type": "kafka", "bootstrap_servers": ..., "topic_logs": ...?,
    "topic_console": ...?}``. ``None`` falls back to the HTTP sink.
    """
    config = dict(sink_config or {"type": "http"})
    sink_type = str(config.get("type", "http")).lower()
    if sink_type == "http":
        from .http import HttpLogSink

        return HttpLogSink(
            base_url=str(config.get("url") or nautobot_url),
            worker_id=worker_id,
            secret_provider=secret_provider,
            tls_verify=tls_verify,
        )
    if sink_type == "kafka":
        from .kafka import KafkaLogSink

        bootstrap = config.get("bootstrap_servers")
        if not bootstrap:
            raise ValueError("kafka log sink requires 'bootstrap_servers'")
        kwargs: dict[str, Any] = {}
        if config.get("topic_logs"):
            kwargs["topic_logs"] = str(config["topic_logs"])
        if config.get("topic_console"):
            kwargs["topic_console"] = str(config["topic_console"])
        return KafkaLogSink(bootstrap_servers=str(bootstrap), **kwargs)
    raise ValueError(f"unknown log sink type: {sink_type!r}")
