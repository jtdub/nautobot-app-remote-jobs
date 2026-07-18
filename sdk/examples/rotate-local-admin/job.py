"""Example remote job: rotate local admin passwords on network devices.

Published with:

    remote-jobs publish --image registry.example.com/jobs/rotate-admin:1.4.0

The container entrypoint is ``python job.py``; ``@job.main`` handles env
parsing, exit codes, and the final log flush.
"""

import secrets as pysecrets
import string

from nautobot_remote_jobs_sdk import job

DEVICE_QUERY = """
query ($device_ids: [String]) {
  devices(filters: {id: $device_ids}) {
    id
    name
    primary_ip4 { address }
    platform { network_driver }
  }
}
"""


def generate_password(length: int = 24) -> str:
    """Generate a random local-admin password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


@job.main
def run(ctx):
    """Rotate the local admin password on each target device."""
    device_ids = ctx.inputs["devices"]
    commit = ctx.inputs.get("commit", False)

    ctx.logger.info(f"Rotating local admin on {len(device_ids)} device(s)", grouping="setup")

    # Bulk read via GraphQL (preferred for >~100 objects).
    data = ctx.graphql(DEVICE_QUERY, variables={"device_ids": device_ids})
    devices = data["devices"]

    # TACACS admin credentials, resolved locally in this container from the
    # provider referenced by the "tacacs-prod" SecretsGroup. The values are
    # auto-registered with the redactor: printing them yields "(redacted)".
    admin_user = ctx.secrets.get("tacacs-prod", access_type="Generic", secret_type="username")
    admin_pass = ctx.secrets.get("tacacs-prod", access_type="Generic", secret_type="password")

    report_lines = []
    for device in devices:
        name = device["name"]
        if ctx.dryrun or not commit:
            ctx.logger.info(f"[dryrun] Would rotate local admin on {name}", grouping=name)
            report_lines.append(f"{name}: skipped (dryrun/commit=false)")
            continue

        new_password = generate_password()
        # A real implementation connects with admin_user/admin_pass (e.g.
        # netmiko/scrapli against device["primary_ip4"]["address"]) and sets
        # new_password on the local admin account.
        _ = (admin_user, admin_pass, new_password)
        ctx.logger.success(f"Rotated local admin on {name}", grouping=name)
        report_lines.append(f"{name}: rotated")

    report_path = "/tmp/rotation-report.txt"
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(report_lines) + "\n")
    artifact_id = ctx.artifacts.upload(report_path, name="rotation-report.txt")
    ctx.logger.info(f"Uploaded rotation report as artifact {artifact_id}", grouping="post_run")
