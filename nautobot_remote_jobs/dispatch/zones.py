"""Zone resolution (SPEC 4.3, 6.4)."""

import logging

from nautobot_remote_jobs.choices import WorkerStatusChoices
from nautobot_remote_jobs.models import ExecutionZone

logger = logging.getLogger(__name__)


def resolve_zone(device):
    """Resolve the execution zone for a device.

    Evaluates all enabled zones' membership rules (rules OR-ed per zone) and
    returns the matching zone with the lowest priority value; ties broken by
    name for determinism. Returns None when no zone matches (SPEC 4.3).
    """
    matches = matching_zones(device)
    if not matches:
        return None
    return sorted(matches, key=lambda zone: (zone.priority, zone.name))[0]


def matching_zones(device):
    """All enabled zones whose membership rules match the device."""
    matches = []
    zones = ExecutionZone.objects.filter(enabled=True).prefetch_related(
        "membership_rules__locations",
        "membership_rules__roles",
        "membership_rules__prefixes",
        "membership_rules__dynamic_group",
    )
    for zone in zones:
        for rule in zone.membership_rules.all():
            if rule.matches_device(device):
                matches.append(zone)
                break
    return matches


def zone_has_online_capacity(zone, capabilities=None):
    """True when the zone has at least one ONLINE worker with free capacity and required capabilities."""
    required = set(capabilities or [])
    for worker in zone.workers.filter(enabled=True, draining=False):
        if worker.status != WorkerStatusChoices.ONLINE:
            continue
        if required and not required.issubset(set(worker.capabilities or [])):
            continue
        if worker.free_capacity() > 0:
            return True
    return False


def cheapest_zone_with_capacity(capabilities=None):
    """Lowest-priority enabled zone that currently has online capacity (SPEC 6.4 'any')."""
    for zone in ExecutionZone.objects.filter(enabled=True).order_by("priority", "name"):
        if zone_has_online_capacity(zone, capabilities):
            return zone
    return None


def zone_coverage_report(devices):
    """Compute the Zone Coverage report data (SPEC 4.3).

    Returns a dict with:
    - uncovered: devices matching no zone
    - ambiguous: [(device, matched_zones, winner)] for devices matching multiple zones
    - empty_zones: enabled zones with zero online workers
    """
    uncovered = []
    ambiguous = []
    for device in devices:
        matches = matching_zones(device)
        if not matches:
            uncovered.append(device)
        elif len(matches) > 1:
            winner = sorted(matches, key=lambda zone: (zone.priority, zone.name))[0]
            ambiguous.append((device, matches, winner))
    empty_zones = [zone for zone in ExecutionZone.objects.filter(enabled=True) if zone.online_worker_count == 0]
    return {"uncovered": uncovered, "ambiguous": ambiguous, "empty_zones": empty_zones}
