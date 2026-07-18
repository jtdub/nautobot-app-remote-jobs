"""REST API serializers for nautobot_remote_jobs."""

from nautobot.apps.api import NautobotModelSerializer
from rest_framework import serializers

from nautobot_remote_jobs import models


class JobDefinitionSerializer(NautobotModelSerializer):
    """JobDefinition serializer."""

    class Meta:
        model = models.JobDefinition
        fields = "__all__"


class ExecutionZoneSerializer(NautobotModelSerializer):
    """ExecutionZone serializer with live worker counts (SPEC 4.2)."""

    worker_count = serializers.IntegerField(read_only=True)
    online_worker_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = models.ExecutionZone
        fields = "__all__"


class ZoneMembershipRuleSerializer(NautobotModelSerializer):
    """ZoneMembershipRule serializer."""

    class Meta:
        model = models.ZoneMembershipRule
        fields = "__all__"


class WorkerSerializer(NautobotModelSerializer):
    """Worker serializer; status is computed from heartbeat TTL."""

    status = serializers.CharField(read_only=True)

    class Meta:
        model = models.Worker
        fields = "__all__"
        # Session-credential material must never appear in API responses:
        # identity_fingerprint is sha256(session_secret) and secret_generation
        # is the rotation counter; write_only keeps them out of GET output.
        extra_kwargs = {
            "identity_fingerprint": {"write_only": True, "required": False},
            "secret_generation": {"write_only": True, "required": False},
        }


class WorkerEnrollmentTokenSerializer(NautobotModelSerializer):
    """Enrollment token serializer. The plaintext is only ever in the create response."""

    token = serializers.CharField(read_only=True, help_text="Plaintext token; shown once.")

    class Meta:
        model = models.WorkerEnrollmentToken
        fields = "__all__"
        extra_kwargs = {
            # write_only (not read_only) keeps the token hash out of API
            # responses; read_only fields are still serialized on output.
            "token_hash": {"write_only": True, "required": False},
            "used_at": {"read_only": True},
            "worker": {"read_only": True},
            "created_by": {"read_only": True},
            "expires": {"required": False},
        }

    def create(self, validated_data):
        request = self.context.get("request")
        instance, plaintext = models.WorkerEnrollmentToken.generate(
            zone=validated_data["zone"],
            created_by=getattr(request, "user", None),
            single_use=validated_data.get("single_use", True),
        )
        if validated_data.get("expires"):
            instance.expires = validated_data["expires"]
            instance.save(update_fields=["expires"])
        instance.token = plaintext  # transient; serialized once, never stored
        return instance


class RemoteJobRunSerializer(NautobotModelSerializer):
    """RemoteJobRun serializer."""

    class Meta:
        model = models.RemoteJobRun
        fields = "__all__"
        extra_kwargs = {
            "scoped_token": {"read_only": True},
            "state": {"read_only": True},
            "job_result": {"read_only": True},
        }


class RunArtifactSerializer(NautobotModelSerializer):
    """RunArtifact serializer."""

    class Meta:
        model = models.RunArtifact
        fields = "__all__"


class RemoteJobScheduleSerializer(NautobotModelSerializer):
    """RemoteJobSchedule serializer."""

    class Meta:
        model = models.RemoteJobSchedule
        fields = "__all__"


class RunRequestSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """Body of POST /job-definitions/{id}/run/ (SPEC 5)."""

    inputs = serializers.JSONField(required=False, default=dict)
    dryrun = serializers.BooleanField(required=False, default=False)
