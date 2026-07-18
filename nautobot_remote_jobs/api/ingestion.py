"""Server-side ingestion of worker log/console batches into core models (SPEC 5, 10).

Shared by the HTTP endpoints and the Kafka consumer. Dedupe is by
(run_id, client_sequence): each batch carries a monotonically increasing
sequence; replays of an already-ingested sequence are dropped (at-least-once
transport + dedupe = effectively exactly-once).
"""

import logging

from django.core.cache import cache
from django.utils.dateparse import parse_datetime
from nautobot.extras.choices import JobConsoleEntryOutputTypeChoices, LogLevelChoices
from nautobot.extras.models import JobConsoleEntry, JobLogEntry

from nautobot_remote_jobs.models import RemoteJobRun

logger = logging.getLogger(__name__)

SEQUENCE_DEDUPE_TTL = 24 * 3600
VALID_LOG_LEVELS = set(LogLevelChoices.values())
VALID_OUTPUT_TYPES = set(JobConsoleEntryOutputTypeChoices.values())


class UnknownRunError(Exception):
    """Raised when the referenced run does not exist."""


def validate_batch_limits(entries):
    """Enforce max 500 entries / 256 KiB per batch (SPEC 5). Returns an error string or None."""
    import json

    from nautobot_remote_jobs.constants import LOG_BATCH_MAX_BYTES, LOG_BATCH_MAX_ENTRIES

    if not isinstance(entries, list):
        return "Body must be a list of entries (or {'entries': [...], 'sequence': N})."
    if len(entries) > LOG_BATCH_MAX_ENTRIES:
        return f"Batch exceeds {LOG_BATCH_MAX_ENTRIES} entries."
    if len(json.dumps(entries)) > LOG_BATCH_MAX_BYTES:
        return f"Batch exceeds {LOG_BATCH_MAX_BYTES} bytes."
    return None


def _dedupe_key(run_id, kind, client_sequence):
    return f"remote-jobs:ingest:{kind}:{run_id}:{client_sequence}"


def _already_ingested(run_id, kind, client_sequence):
    """True when this (run, kind, sequence) batch was already written."""
    if client_sequence is None:
        return False
    return cache.get(_dedupe_key(run_id, kind, client_sequence)) is not None


def _mark_ingested(run_id, kind, client_sequence):
    """Record a (run, kind, sequence) batch as written.

    Marked only *after* the DB write commits: marking before the write would
    turn a mid-write failure into silent data loss, because the client's
    at-least-once retry of the same sequence would then be dropped as a
    duplicate. The tiny check-then-mark window can at worst duplicate a batch
    under concurrent identical retries, which is strictly safer than losing it.
    """
    if client_sequence is None:
        return
    cache.set(_dedupe_key(run_id, kind, client_sequence), 1, timeout=SEQUENCE_DEDUPE_TTL)


def get_run(run_id):
    """Fetch the run or raise UnknownRunError."""
    try:
        return RemoteJobRun.objects.select_related("job_result").get(pk=run_id)
    except (RemoteJobRun.DoesNotExist, ValueError) as exc:
        raise UnknownRunError(str(run_id)) from exc


def write_log_batch(run_id, entries, client_sequence=None):
    """Bulk-create JobLogEntry rows from a structured log batch (SPEC 10)."""
    run = get_run(run_id)
    if _already_ingested(run_id, "logs", client_sequence):
        return 0
    rows = []
    for entry in entries:
        level = entry.get("level", LogLevelChoices.LOG_INFO)
        if level not in VALID_LOG_LEVELS:
            level = LogLevelChoices.LOG_INFO
        row = JobLogEntry(
            job_result=run.job_result,
            log_level=level,
            grouping=(entry.get("grouping") or "main")[:100],
            message=str(entry.get("message", "")),
        )
        timestamp = entry.get("timestamp")
        if timestamp:
            parsed = parse_datetime(timestamp)
            if parsed:
                row.created = parsed
        rows.append(row)
    JobLogEntry.objects.bulk_create(rows)
    _mark_ingested(run_id, "logs", client_sequence)
    return len(rows)


def write_console_batch(run_id, entries, client_sequence=None):
    """Bulk-create JobConsoleEntry rows from a console output batch (SPEC 10)."""
    run = get_run(run_id)
    if _already_ingested(run_id, "console", client_sequence):
        return 0
    rows = []
    for entry in entries:
        output_type = entry.get("output_type", JobConsoleEntryOutputTypeChoices.TYPE_OUTPUT)
        if output_type not in VALID_OUTPUT_TYPES:
            output_type = JobConsoleEntryOutputTypeChoices.TYPE_OUTPUT
        rows.append(
            JobConsoleEntry(
                job_result=run.job_result,
                output_type=output_type,
                text=str(entry.get("text", "")),
            )
        )
    # timestamp is auto_now_add on JobConsoleEntry; client timestamps are advisory only.
    JobConsoleEntry.objects.bulk_create(rows)
    _mark_ingested(run_id, "console", client_sequence)
    return len(rows)
