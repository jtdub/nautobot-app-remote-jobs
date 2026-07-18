"""Tests for log sink batching thresholds (SPEC 10.1).

Flush triggers: 2 seconds elapsed, 100 entries, or 64 KiB — whichever
comes first. client_sequence increases monotonically per run.
"""

import asyncio

from remote_worker.sinks.base import (
    FLUSH_INTERVAL_SECONDS,
    FLUSH_MAX_BYTES,
    FLUSH_MAX_ENTRIES,
    BatchingLogSink,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class RecordingSink(BatchingLogSink):
    def __init__(self, clock=None):
        super().__init__(clock=clock)
        self.batches: list[tuple[str, str, int, list]] = []

    async def _send(self, run_id, kind, sequence, entries):
        self.batches.append((run_id, kind, sequence, list(entries)))


def run(coro):
    return asyncio.run(coro)


def test_flush_on_entry_count():
    async def scenario():
        sink = RecordingSink()
        for i in range(FLUSH_MAX_ENTRIES - 1):
            await sink.emit_log("run-1", {"level": "info", "message": f"m{i}"})
        assert sink.batches == []  # threshold not reached yet
        await sink.emit_log("run-1", {"level": "info", "message": "last"})
        assert len(sink.batches) == 1
        run_id, kind, sequence, entries = sink.batches[0]
        assert (run_id, kind, sequence) == ("run-1", "logs", 1)
        assert len(entries) == FLUSH_MAX_ENTRIES

    run(scenario())


def test_flush_on_byte_size():
    async def scenario():
        sink = RecordingSink()
        big = "x" * 8192  # ~8 KiB per entry -> flush at the 8th entry
        count = 0
        while not sink.batches:
            await sink.emit_console("run-1", {"output_type": "stdout", "text": big})
            count += 1
            assert count <= (FLUSH_MAX_BYTES // 8192) + 2
        assert count < FLUSH_MAX_ENTRIES  # byte threshold fired first
        entries = sink.batches[0][3]
        assert len(entries) == count

    run(scenario())


def test_flush_on_elapsed_time():
    async def scenario():
        clock = FakeClock()
        sink = RecordingSink(clock=clock)
        await sink.emit_log("run-1", {"level": "info", "message": "hello"})
        await sink.maybe_flush_expired()
        assert sink.batches == []  # not yet 2s old
        clock.now += FLUSH_INTERVAL_SECONDS + 0.1
        await sink.maybe_flush_expired()
        assert len(sink.batches) == 1
        assert sink.batches[0][3] == [{"level": "info", "message": "hello"}]

    run(scenario())


def test_sequence_monotonic_per_run_across_kinds():
    async def scenario():
        sink = RecordingSink()
        await sink.emit_log("run-1", {"message": "a"})
        await sink.flush("run-1")
        await sink.emit_console("run-1", {"output_type": "stdout", "text": "b"})
        await sink.flush("run-1")
        await sink.emit_log("run-2", {"message": "c"})
        await sink.flush("run-2")
        sequences = [(b[0], b[2]) for b in sink.batches]
        assert sequences == [("run-1", 1), ("run-1", 2), ("run-2", 1)]

    run(scenario())


def test_explicit_flush_delivers_partial_buffers():
    async def scenario():
        sink = RecordingSink()
        await sink.emit_log("run-1", {"message": "only"})
        await sink.emit_console("run-2", {"output_type": "stderr", "text": "boom"})
        await sink.flush()
        assert len(sink.batches) == 2
        # Flushing again with empty buffers sends nothing.
        await sink.flush()
        assert len(sink.batches) == 2

    run(scenario())


def test_logs_and_console_buffered_independently():
    async def scenario():
        sink = RecordingSink()
        for i in range(FLUSH_MAX_ENTRIES):
            await sink.emit_log("run-1", {"message": f"log{i}"})
        # Only the logs buffer flushed; console untouched.
        assert len(sink.batches) == 1
        assert sink.batches[0][1] == "logs"

    run(scenario())


def test_kafka_sink_unavailable_without_aiokafka():
    import remote_worker.sinks.kafka as kafka_mod

    if kafka_mod.AIOKafkaProducer is not None:  # aiokafka installed: nothing to test
        return
    try:
        kafka_mod.KafkaLogSink(bootstrap_servers="localhost:9092")
    except kafka_mod.KafkaSinkUnavailableError as exc:
        assert "remote-worker[kafka]" in str(exc)
    else:
        raise AssertionError("expected KafkaSinkUnavailableError")
