"""ChoiceSets for nautobot_remote_jobs."""

from nautobot.apps.choices import ChoiceSet


class ZonePolicyChoices(ChoiceSet):
    """How a JobDefinition resolves its execution zone (SPEC 4.1)."""

    PINNED = "pinned"
    ANY = "any"
    PER_DEVICE = "per_device"
    FAN_OUT = "fan_out"

    CHOICES = (
        (PINNED, "Pinned to default zone"),
        (ANY, "Any zone with capacity"),
        (PER_DEVICE, "Per target device"),
        (FAN_OUT, "Fan out per zone"),
    )


class FailoverPolicyChoices(ChoiceSet):
    """Zone behavior when no online capacity exists (SPEC 4.2)."""

    NONE = "none"
    WAIT = "wait"
    FAILOVER = "failover"

    CHOICES = (
        (NONE, "Fail fast"),
        (WAIT, "Wait for capacity"),
        (FAILOVER, "Fail over to other zones"),
    )


class RunStateChoices(ChoiceSet):
    """RemoteJobRun state machine (SPEC 6.1)."""

    PENDING = "PENDING"
    OFFERED = "OFFERED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    TERMINATED = "TERMINATED"
    ABANDONED = "ABANDONED"
    CANCELLED = "CANCELLED"
    FAILED_DISPATCH = "FAILED_DISPATCH"

    CHOICES = (
        (PENDING, "Pending"),
        (OFFERED, "Offered"),
        (CLAIMED, "Claimed"),
        (RUNNING, "Running"),
        (SUCCESS, "Success"),
        (FAILURE, "Failure"),
        (TERMINATED, "Terminated"),
        (ABANDONED, "Abandoned"),
        (CANCELLED, "Cancelled"),
        (FAILED_DISPATCH, "Failed dispatch"),
    )

    TERMINAL_STATES = (SUCCESS, FAILURE, TERMINATED, CANCELLED, FAILED_DISPATCH)
    # ABANDONED is terminal unless retry policy re-queues it (SPEC 6.1).
    NON_TERMINAL_STATES = (PENDING, OFFERED, CLAIMED, RUNNING)


class WorkerStatusChoices(ChoiceSet):
    """Computed worker status (SPEC 4.4)."""

    ONLINE = "online"
    DRAINING = "draining"
    OFFLINE = "offline"

    CHOICES = (
        (ONLINE, "Online"),
        (DRAINING, "Draining"),
        (OFFLINE, "Offline"),
    )


class ScheduleIntervalChoices(ChoiceSet):
    """RemoteJobSchedule intervals (SPEC 4.7)."""

    ONCE = "once"
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    CUSTOM = "custom"

    CHOICES = (
        (ONCE, "Once"),
        (HOURLY, "Hourly"),
        (DAILY, "Daily"),
        (WEEKLY, "Weekly"),
        (CUSTOM, "Custom (crontab)"),
    )


class CancelModeChoices(ChoiceSet):
    """job.cancel modes (SPEC 8.2)."""

    GRACEFUL = "graceful"
    KILL = "kill"

    CHOICES = (
        (GRACEFUL, "Graceful"),
        (KILL, "Kill"),
    )
