"""Run submission: input validation, zone resolution, fan-out, and enqueue (SPEC 6.4)."""

import logging

import jsonschema
from django.db import transaction
from django.utils import timezone
from nautobot.core.events import publish_event
from nautobot.extras.choices import JobResultStatusChoices, LogLevelChoices
from nautobot.extras.models import JobLogEntry, JobResult

from nautobot_remote_jobs.choices import FailoverPolicyChoices, RunStateChoices, ZonePolicyChoices
from nautobot_remote_jobs.constants import EVENT_PREFIX, REDACTED_PLACEHOLDER, TARGET_ANNOTATION
from nautobot_remote_jobs.dispatch import notify, zones
from nautobot_remote_jobs.models import RemoteJobRun

logger = logging.getLogger(__name__)


class SubmissionError(Exception):
    """Raised when a run cannot be submitted at all (validation, disabled definition)."""


def validate_inputs(definition, inputs):
    """Validate inputs against the definition's JSON Schema (SPEC 4.1)."""
    if not definition.input_schema:
        return
    try:
        jsonschema.validate(instance=inputs, schema=definition.input_schema)
    except jsonschema.ValidationError as exc:
        raise SubmissionError(f"Input validation failed: {exc.message}") from exc


def redact_inputs(schema, inputs):
    """Return a copy of inputs with writeOnly keys replaced for storage (SPEC 4.6)."""
    redacted = dict(inputs)
    for key, subschema in (schema or {}).get("properties", {}).items():
        if isinstance(subschema, dict) and subschema.get("writeOnly") and key in redacted:
            redacted[key] = REDACTED_PLACEHOLDER
    return redacted


def target_device_field(schema):
    """Name of the schema property annotated x-remote-jobs-target: device, if any (SPEC 6.4)."""
    for key, subschema in (schema or {}).get("properties", {}).items():
        if isinstance(subschema, dict) and subschema.get(TARGET_ANNOTATION) == "device":
            return key
    return None


def extract_target_devices(definition, inputs):
    """Resolve the device-bearing input field to Device objects."""
    from nautobot.dcim.models import Device

    field = target_device_field(definition.input_schema)
    if field is None:
        raise SubmissionError(
            f"zone_policy={definition.zone_policy} requires an input field annotated " f"'{TARGET_ANNOTATION}: device'."
        )
    device_ids = inputs.get(field) or []
    if not isinstance(device_ids, list):
        device_ids = [device_ids]
    from django.conf import settings

    from nautobot_remote_jobs.constants import DEFAULT_FANOUT_MAX_DEVICES

    max_devices = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get(
        "fanout_max_devices", DEFAULT_FANOUT_MAX_DEVICES
    )
    if len(device_ids) > max_devices:
        # Bound per-request row creation: a per_device/fan_out submission expands
        # into one RemoteJobRun + JobResult per device inside one transaction.
        raise SubmissionError(
            f"Too many target devices in '{field}': {len(device_ids)} exceeds the limit of {max_devices}."
        )
    devices = list(Device.objects.filter(pk__in=device_ids))
    if len(devices) != len(set(device_ids)):
        found = {str(device.pk) for device in devices}
        missing = [str(pk) for pk in device_ids if str(pk) not in found]
        raise SubmissionError(f"Unknown devices in '{field}': {', '.join(missing)}")
    return field, devices


def _create_job_result(definition, user):
    return JobResult.objects.create(
        name=f"[remote] {definition.name}",
        user=user,
        job_model=None,
        status=JobResultStatusChoices.STATUS_PENDING,
    )


def log_to_result(job_result, message, level=LogLevelChoices.LOG_INFO, grouping="dispatch"):
    """Write a dispatch log line into the core JobLogEntry stream."""
    JobLogEntry.objects.create(job_result=job_result, log_level=level, grouping=grouping, message=message)


def _resolve_submission_zone(definition, capabilities):
    """Zone for pinned/any policies, applying failover semantics (SPEC 6.4)."""
    if definition.zone_policy == ZonePolicyChoices.PINNED:
        return definition.default_zone, None
    # any: cheapest zone with capacity, else the default zone's none/wait semantics.
    zone = zones.cheapest_zone_with_capacity(capabilities)
    if zone is not None:
        return zone, None
    fallback = definition.default_zone
    if fallback is not None and fallback.failover_policy == FailoverPolicyChoices.WAIT:
        return fallback, None
    return None, "No zone with online capacity and policy does not allow waiting."


def _apply_failover(run, zone):
    """When the resolved zone has no capacity, apply its failover policy. Returns (zone, error)."""
    definition = run.job_definition
    capabilities = definition.capabilities or []
    if zones.zone_has_online_capacity(zone, capabilities):
        return zone, None
    policy = zone.failover_policy
    if policy == FailoverPolicyChoices.NONE:
        return None, f"Zone '{zone}' has no online capacity and failover policy is 'none'."
    if policy == FailoverPolicyChoices.WAIT:
        return zone, None  # stays PENDING; wait timeout enforced by the reaper
    # failover: walk targets in order (SPEC 6.4)
    if definition.requires_zone_local:
        return None, (
            f"Zone '{zone}' has no online capacity; definition requires zone-local execution, " "failover refused."
        )
    for target in zone.ordered_failover_zones:
        if target.enabled and zones.zone_has_online_capacity(target, capabilities):
            return target, None
    return zone, None  # queue in the original zone and wait out max_wait_seconds


@transaction.atomic
def submit_run(definition, user, inputs, dryrun=False, approved=True):  # pylint: disable=unused-argument
    """Create the RemoteJobRun (and children for per_device/fan_out) and notify workers.

    Returns the parent/only RemoteJobRun. Dispatch-level failures produce a run
    in FAILED_DISPATCH with an explanatory JobLogEntry rather than raising.
    """
    if not definition.enabled:
        raise SubmissionError(f"Job definition '{definition}' is disabled.")
    if dryrun and not definition.dryrun_supported:
        raise SubmissionError(f"Job definition '{definition}' does not support dryrun.")
    validate_inputs(definition, inputs)

    now = timezone.now()
    stored_inputs = redact_inputs(definition.input_schema, inputs)
    job_result = _create_job_result(definition, user)
    run = RemoteJobRun.objects.create(
        job_definition=definition,
        job_result=job_result,
        inputs=stored_inputs,
        dryrun=dryrun,
        queued_at=now,
    )

    if (
        definition.singleton
        and definition.runs.filter(state__in=RunStateChoices.NON_TERMINAL_STATES).exclude(pk=run.pk).exists()
    ):
        log_to_result(job_result, "Singleton definition has an active run; queued behind it.")

    if definition.zone_policy in (ZonePolicyChoices.PER_DEVICE, ZonePolicyChoices.FAN_OUT):
        _submit_fanout(run, definition, user, inputs, stored_inputs, dryrun, now)
    else:
        zone, error = _resolve_submission_zone(definition, definition.capabilities or [])
        if zone is not None and error is None:
            zone, error = _apply_failover(run, zone)
        if zone is None:
            _fail_dispatch(run, error or "No zone resolved.")
            return run
        run.zone = zone
        run.save(update_fields=["zone"])
        transaction.on_commit(lambda: notify.publish_work_available(zone))

    _publish_run_event(run, "queued")
    return run


def _submit_fanout(parent, definition, user, inputs, stored_inputs, dryrun, now):
    """Create child runs for per_device / fan_out policies (SPEC 6.4)."""
    try:
        field, devices = extract_target_devices(definition, inputs)
    except SubmissionError as exc:
        _fail_dispatch(parent, str(exc))
        return

    if not devices:
        _fail_dispatch(parent, f"No target devices in input field '{field}'.")
        return

    resolution = [(device, zones.resolve_zone(device)) for device in devices]
    unresolved = [device for device, zone in resolution if zone is None]
    if unresolved:
        names = ", ".join(device.name or str(device.pk) for device in unresolved)
        _fail_dispatch(parent, f"No execution zone resolved for devices: {names}.")
        return

    notified_zones = {}
    if definition.zone_policy == ZonePolicyChoices.PER_DEVICE:
        for device, zone in resolution:
            child_inputs = dict(stored_inputs)
            child_inputs[field] = [str(device.pk)]
            child = RemoteJobRun.objects.create(
                job_definition=definition,
                job_result=_create_job_result(definition, user),
                parent=parent,
                zone=zone,
                device=device,
                inputs=child_inputs,
                dryrun=dryrun,
                queued_at=now,
            )
            log_to_result(
                parent.job_result,
                f"Child run {child.pk} queued for device {device} in zone {zone}.",
            )
            notified_zones[zone.pk] = zone
    else:  # fan_out: one child per distinct zone with that zone's device subset
        by_zone = {}
        for device, zone in resolution:
            by_zone.setdefault(zone.pk, (zone, []))[1].append(device)
        for zone, zone_devices in by_zone.values():
            child_inputs = dict(stored_inputs)
            child_inputs[field] = [str(device.pk) for device in zone_devices]
            child = RemoteJobRun.objects.create(
                job_definition=definition,
                job_result=_create_job_result(definition, user),
                parent=parent,
                zone=zone,
                inputs=child_inputs,
                dryrun=dryrun,
                queued_at=now,
            )
            log_to_result(
                parent.job_result,
                f"Child run {child.pk} queued in zone {zone} for {len(zone_devices)} device(s).",
            )
            notified_zones[zone.pk] = zone

    # The parent aggregates; it is never offered to workers. Mark it RUNNING once
    # children exist so its JobResult shows activity.
    parent.state = RunStateChoices.RUNNING
    parent.started_at = now
    parent.save(update_fields=["state", "started_at"])
    parent.sync_job_result()

    def _notify_all():
        for zone in notified_zones.values():
            notify.publish_work_available(zone)

    transaction.on_commit(_notify_all)


def _fail_dispatch(run, message):
    log_to_result(run.job_result, message, level=LogLevelChoices.LOG_ERROR)
    run.state = RunStateChoices.FAILED_DISPATCH
    run.finished_at = timezone.now()
    run.save(update_fields=["state", "finished_at"])
    run.sync_job_result()
    logger.warning("Run %s failed dispatch: %s", run.pk, message)


def _publish_run_event(run, event):
    """Publish a lifecycle event (SPEC 16). Payload carries no inputs and no tokens."""
    try:
        publish_event(
            topic=f"{EVENT_PREFIX}.{event}",
            payload={
                "run_id": str(run.pk),
                "definition": run.job_definition.name,
                "zone": run.zone.name if run.zone else None,
                "worker": run.worker.name if run.worker else None,
                "state": run.state,
                "dryrun": run.dryrun,
            },
        )
    except Exception:  # noqa: BLE001 - event publication must never break dispatch
        logger.warning("Failed to publish %s event for run %s", event, run.pk, exc_info=True)
