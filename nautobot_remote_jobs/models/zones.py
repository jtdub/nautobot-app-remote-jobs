"""ExecutionZone, ZoneFailover, and ZoneMembershipRule models (SPEC 4.2-4.3)."""

from django.core.exceptions import ValidationError
from django.db import models
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.constants import CHARFIELD_MAX_LENGTH

from nautobot_remote_jobs.choices import FailoverPolicyChoices


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class ExecutionZone(PrimaryModel):
    """A named pool of workers that devices map to via membership rules."""

    name = models.CharField(max_length=CHARFIELD_MAX_LENGTH, unique=True)
    description = models.CharField(max_length=CHARFIELD_MAX_LENGTH, blank=True)
    enabled = models.BooleanField(default=True)
    priority = models.PositiveIntegerField(
        default=100,
        help_text="Lower wins when a device matches multiple zones. Ties broken by name.",
    )
    failover_policy = models.CharField(
        max_length=CHARFIELD_MAX_LENGTH,
        choices=FailoverPolicyChoices,
        default=FailoverPolicyChoices.NONE,
    )
    max_wait_seconds = models.PositiveIntegerField(
        default=300,
        help_text="Queue wait for 'wait' policy; per-hop wait for 'failover'.",
    )
    failover_zones = models.ManyToManyField(
        to="self",
        through="nautobot_remote_jobs.ZoneFailover",
        through_fields=("zone", "target"),
        symmetrical=False,
        related_name="failover_sources",
        blank=True,
    )

    class Meta:
        ordering = ["priority", "name"]
        verbose_name = "Execution Zone"
        verbose_name_plural = "Execution Zones"

    def __str__(self):
        return self.name

    @property
    def online_worker_count(self):
        """Number of workers currently considered ONLINE by heartbeat TTL."""
        from nautobot_remote_jobs.choices import WorkerStatusChoices  # avoid circular import

        return sum(1 for worker in self.workers.all() if worker.status == WorkerStatusChoices.ONLINE)

    @property
    def worker_count(self):
        """Total enrolled workers in this zone."""
        return self.workers.count()

    @property
    def ordered_failover_zones(self):
        """Failover targets in configured order."""
        return [zf.target for zf in self.failover_targets.select_related("target").order_by("order")]


class ZoneFailover(BaseModel):
    """Ordered through model for ExecutionZone.failover_zones."""

    zone = models.ForeignKey(to=ExecutionZone, on_delete=models.CASCADE, related_name="failover_targets")
    target = models.ForeignKey(to=ExecutionZone, on_delete=models.CASCADE, related_name="failover_sources_through")
    order = models.PositiveIntegerField(default=100)

    class Meta:
        ordering = ["zone", "order"]
        unique_together = [["zone", "target"]]

    def __str__(self):
        return f"{self.zone} -> {self.target} ({self.order})"

    def clean(self):
        super().clean()
        if self.zone_id and self.target_id and self.zone_id == self.target_id:
            raise ValidationError("A zone cannot fail over to itself.")


class ZoneMembershipRule(BaseModel):
    """Maps devices to a zone. Rules per zone are OR-ed; criteria within a rule are AND-ed (SPEC 4.3)."""

    zone = models.ForeignKey(to=ExecutionZone, on_delete=models.CASCADE, related_name="membership_rules")
    locations = models.ManyToManyField(to="dcim.Location", related_name="+", blank=True)
    include_descendant_locations = models.BooleanField(
        default=True, help_text="Match devices in descendant locations too."
    )
    roles = models.ManyToManyField(to="extras.Role", related_name="+", blank=True)
    prefixes = models.ManyToManyField(
        to="ipam.Prefix",
        related_name="+",
        blank=True,
        help_text="Matched by device primary IP containment (v4 or v6).",
    )
    dynamic_group = models.ForeignKey(
        to="extras.DynamicGroup",
        on_delete=models.PROTECT,
        related_name="+",
        blank=True,
        null=True,
        help_text="Escape hatch for arbitrary combinations.",
    )
    weight = models.PositiveIntegerField(
        default=100, help_text="Rule evaluation order within the zone (cosmetic; rules are OR-ed)."
    )

    class Meta:
        ordering = ["zone", "weight"]
        verbose_name = "Zone Membership Rule"
        verbose_name_plural = "Zone Membership Rules"

    def __str__(self):
        return f"Rule for {self.zone} (weight {self.weight})"

    def clean(self):
        """At least one criterion or dynamic_group must be set (SPEC 4.3)."""
        super().clean()
        # M2M contents can only be checked once the instance has a primary key.
        if self.present_in_database:
            if (
                not self.dynamic_group
                and not self.locations.exists()
                and not self.roles.exists()
                and not self.prefixes.exists()
            ):
                raise ValidationError("At least one of locations, roles, prefixes, or dynamic_group must be set.")

    def matches_device(self, device):
        """Return True if the given device satisfies every configured criterion (AND semantics)."""
        if self.dynamic_group and not self.dynamic_group.members.filter(pk=device.pk).exists():
            return False
        location_ids = list(self.locations.values_list("pk", flat=True))
        if location_ids:
            if device.location_id is None:
                return False
            if self.include_descendant_locations:
                ancestors = {loc.pk for loc in device.location.ancestors(include_self=True)}
                if not ancestors.intersection(location_ids):
                    return False
            elif device.location_id not in location_ids:
                return False
        role_ids = list(self.roles.values_list("pk", flat=True))
        if role_ids and device.role_id not in role_ids:
            return False
        prefixes = list(self.prefixes.all())
        if prefixes:
            primary_ip = device.primary_ip
            if primary_ip is None:
                return False
            if not any(primary_ip.address in prefix.prefix for prefix in prefixes):
                return False
        if not self.dynamic_group and not location_ids and not role_ids and not prefixes:
            # Defensive: an empty rule must never act as a global wildcard.
            return False
        return True
