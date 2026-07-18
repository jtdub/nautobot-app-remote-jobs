"""Management command: run the Redis RPC bridge consumer (SPEC 8.4)."""

from django.core.management.base import BaseCommand

from nautobot_remote_jobs.rpc.bridge import RPCBridgeConsumer


class Command(BaseCommand):
    """Long-running consumer answering worker JSON-RPC requests relayed by the gateway."""

    help = "Run the remote-jobs JSON-RPC bridge consumer (gateway <-> app)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workers",
            type=int,
            default=4,
            help="Handler thread pool size (default: 4).",
        )

    def handle(self, *args, **options):
        consumer = RPCBridgeConsumer(workers=options["workers"])
        self.stdout.write(self.style.SUCCESS("Starting remote-jobs RPC consumer..."))
        consumer.run()
