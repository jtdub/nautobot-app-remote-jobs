"""Management command: Kafka log consumer (SPEC 10.2).

Nautobot core cannot consume Kafka (its events framework is publish-only), so
this app ships the consumer. Deploy it as its own long-running container, the
same operational pattern as ChatOps workers. Requires the `kafka` extra
(confluent-kafka).
"""

import json
import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

TOPIC_LOGS = "remote-jobs.logs"
TOPIC_CONSOLE = "remote-jobs.console"


class Command(BaseCommand):
    """Consume remote-jobs log topics and bulk-write core log models via ORM."""

    help = "Run the remote-jobs Kafka log consumer (topics: remote-jobs.logs, remote-jobs.console)."

    def add_arguments(self, parser):
        parser.add_argument("--bootstrap-servers", required=True, help="Kafka bootstrap servers.")
        parser.add_argument("--group-id", default="remote-jobs-log-consumer")
        parser.add_argument("--poll-timeout", type=float, default=1.0)
        parser.add_argument("--batch-size", type=int, default=500)

    def handle(self, *args, **options):
        try:
            from confluent_kafka import Consumer, KafkaError
        except ImportError as exc:
            raise CommandError("confluent-kafka is required: pip install nautobot-app-remote-jobs[kafka]") from exc

        consumer = Consumer(
            {
                "bootstrap.servers": options["bootstrap_servers"],
                "group.id": options["group_id"],
                # Offsets commit only after the DB commit (at-least-once +
                # sequence dedupe = effectively exactly-once, SPEC 10.2).
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe([TOPIC_LOGS, TOPIC_CONSOLE])
        self.stdout.write(self.style.SUCCESS("remote-jobs Kafka log consumer started."))
        try:
            self._consume_loop(consumer, KafkaError, options)
        except KeyboardInterrupt:
            pass
        finally:
            consumer.close()

    def _consume_loop(self, consumer, kafka_error_cls, options):
        from nautobot_remote_jobs.api.ingestion import write_console_batch, write_log_batch

        while True:
            messages = consumer.consume(options["batch_size"], timeout=options["poll_timeout"])
            if not messages:
                continue
            processed = []
            for message in messages:
                if message.error():
                    if message.error().code() != kafka_error_cls._PARTITION_EOF:  # pylint: disable=protected-access
                        logger.error("Kafka error: %s", message.error())
                    continue
                try:
                    payload = json.loads(message.value())
                    run_id = payload["run_id"]
                    sequence = payload.get("sequence")
                    entries = payload["entries"]
                except (ValueError, KeyError, TypeError):
                    logger.warning("Malformed Kafka message on %s; skipping", message.topic())
                    processed.append(message)
                    continue
                try:
                    if message.topic() == TOPIC_LOGS:
                        write_log_batch(run_id, entries, client_sequence=sequence)
                    else:
                        write_console_batch(run_id, entries, client_sequence=sequence)
                except Exception:  # noqa: BLE001 - one bad run must not stall the partition
                    logger.exception("Failed to ingest batch for run %s", run_id)
                processed.append(message)
            if processed:
                consumer.commit(asynchronous=False)
