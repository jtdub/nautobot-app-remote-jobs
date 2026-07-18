"""Forms for nautobot_remote_jobs, including the dynamic run form (SPEC 14, 15)."""

import json

from django import forms
from nautobot.apps.forms import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    NautobotBulkEditForm,
    NautobotFilterForm,
    NautobotModelForm,
    TagsBulkEditFormMixin,
)
from nautobot.dcim.models import Device, Location
from nautobot.extras.models import DynamicGroup, Role, SecretsGroup
from nautobot.ipam.models import Prefix

from nautobot_remote_jobs import models
from nautobot_remote_jobs.choices import RunStateChoices, ZonePolicyChoices
from nautobot_remote_jobs.constants import TARGET_ANNOTATION


class JobDefinitionForm(NautobotModelForm):
    """JobDefinition create/edit form."""

    default_zone = DynamicModelChoiceField(queryset=models.ExecutionZone.objects.all(), required=False)
    secrets_groups = DynamicModelMultipleChoiceField(queryset=SecretsGroup.objects.all(), required=False)

    class Meta:
        model = models.JobDefinition
        fields = [
            "name",
            "description",
            "enabled",
            "image",
            "image_digest",
            "input_schema",
            "zone_policy",
            "default_zone",
            "secrets_groups",
            "capabilities",
            "timeout_seconds",
            "grace_seconds",
            "retry_max",
            "singleton",
            "dryrun_supported",
            "requires_zone_local",
            "tags",
        ]


class JobDefinitionFilterForm(NautobotFilterForm):
    """JobDefinition list filtering."""

    model = models.JobDefinition
    q = forms.CharField(required=False, label="Search")
    enabled = forms.NullBooleanField(required=False)
    zone_policy = forms.MultipleChoiceField(choices=ZonePolicyChoices, required=False)


class JobDefinitionBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):
    """JobDefinition bulk edit."""

    pk = forms.ModelMultipleChoiceField(queryset=models.JobDefinition.objects.all(), widget=forms.MultipleHiddenInput)
    enabled = forms.NullBooleanField(required=False)

    class Meta:
        nullable_fields = []


class ExecutionZoneForm(NautobotModelForm):
    """ExecutionZone create/edit form."""

    class Meta:
        model = models.ExecutionZone
        fields = [
            "name",
            "description",
            "enabled",
            "priority",
            "failover_policy",
            "max_wait_seconds",
            "tags",
        ]


class ExecutionZoneFilterForm(NautobotFilterForm):
    """ExecutionZone list filtering."""

    model = models.ExecutionZone
    q = forms.CharField(required=False, label="Search")
    enabled = forms.NullBooleanField(required=False)


class ExecutionZoneBulkEditForm(TagsBulkEditFormMixin, NautobotBulkEditForm):
    """ExecutionZone bulk edit."""

    pk = forms.ModelMultipleChoiceField(queryset=models.ExecutionZone.objects.all(), widget=forms.MultipleHiddenInput)
    enabled = forms.NullBooleanField(required=False)
    priority = forms.IntegerField(required=False)

    class Meta:
        nullable_fields = []


class ZoneMembershipRuleForm(NautobotModelForm):
    """ZoneMembershipRule create/edit form."""

    zone = DynamicModelChoiceField(queryset=models.ExecutionZone.objects.all())
    locations = DynamicModelMultipleChoiceField(queryset=Location.objects.all(), required=False)
    roles = DynamicModelMultipleChoiceField(queryset=Role.objects.all(), required=False)
    prefixes = DynamicModelMultipleChoiceField(queryset=Prefix.objects.all(), required=False)
    dynamic_group = DynamicModelChoiceField(queryset=DynamicGroup.objects.all(), required=False)

    class Meta:
        model = models.ZoneMembershipRule
        fields = [
            "zone",
            "locations",
            "include_descendant_locations",
            "roles",
            "prefixes",
            "dynamic_group",
            "weight",
        ]


class WorkerForm(NautobotModelForm):
    """Worker edit form (admin fields only; identity fields are enrollment-managed)."""

    zone = DynamicModelChoiceField(queryset=models.ExecutionZone.objects.all())

    class Meta:
        model = models.Worker
        fields = ["name", "zone", "enabled", "draining", "capacity", "tags"]


class WorkerFilterForm(NautobotFilterForm):
    """Worker list filtering."""

    model = models.Worker
    q = forms.CharField(required=False, label="Search")
    zone = DynamicModelMultipleChoiceField(queryset=models.ExecutionZone.objects.all(), required=False)
    enabled = forms.NullBooleanField(required=False)


class WorkerEnrollmentTokenForm(NautobotModelForm):
    """Enrollment token creation; the plaintext is displayed once after save."""

    zone = DynamicModelChoiceField(queryset=models.ExecutionZone.objects.all())

    class Meta:
        model = models.WorkerEnrollmentToken
        fields = ["zone", "single_use"]

    def save(self, commit=True):
        """Generate the token via the model helper; stash the plaintext for one-time display."""
        instance, plaintext = models.WorkerEnrollmentToken.generate(
            zone=self.cleaned_data["zone"],
            single_use=self.cleaned_data.get("single_use", True),
        )
        instance.plaintext_token = plaintext  # transient, never stored
        self.instance = instance
        return instance


class RemoteJobRunFilterForm(NautobotFilterForm):
    """Run list filtering (SPEC 15)."""

    model = models.RemoteJobRun
    q = forms.CharField(required=False, label="Search")
    job_definition = DynamicModelMultipleChoiceField(queryset=models.JobDefinition.objects.all(), required=False)
    state = forms.MultipleChoiceField(choices=RunStateChoices, required=False)
    zone = DynamicModelMultipleChoiceField(queryset=models.ExecutionZone.objects.all(), required=False)
    worker = DynamicModelMultipleChoiceField(queryset=models.Worker.objects.all(), required=False)


class RemoteJobScheduleForm(NautobotModelForm):
    """Schedule create/edit form."""

    job_definition = DynamicModelChoiceField(queryset=models.JobDefinition.objects.all())

    class Meta:
        model = models.RemoteJobSchedule
        fields = [
            "name",
            "job_definition",
            "enabled",
            "interval",
            "crontab",
            "start_time",
            "user",
            "inputs",
            "tags",
        ]


class RemoteJobScheduleFilterForm(NautobotFilterForm):
    """Schedule list filtering."""

    model = models.RemoteJobSchedule
    q = forms.CharField(required=False, label="Search")
    enabled = forms.NullBooleanField(required=False)


def build_run_form(definition, data=None):
    """Server-side dynamic form generation from input_schema (SPEC 14).

    Common types render native fields: string, integer/number, boolean, enum,
    and uuid-arrays with object pickers for x-remote-jobs-target fields; a JSON
    textarea is the fallback for everything else.
    """
    schema = definition.input_schema or {}
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields = {}

    for key, subschema in properties.items():
        if not isinstance(subschema, dict):
            continue
        label = subschema.get("title", key.replace("_", " ").capitalize())
        help_text = subschema.get("description", "")
        is_required = key in required
        kwargs = {"label": label, "help_text": help_text, "required": is_required}
        json_type = subschema.get("type")
        if subschema.get(TARGET_ANNOTATION) == "device":
            fields[key] = DynamicModelMultipleChoiceField(queryset=Device.objects.all(), **kwargs)
        elif "enum" in subschema:
            fields[key] = forms.ChoiceField(choices=[(value, value) for value in subschema["enum"]], **kwargs)
        elif json_type == "boolean":
            kwargs["required"] = False
            kwargs["initial"] = subschema.get("default", False)
            fields[key] = forms.BooleanField(**kwargs)
        elif json_type == "integer":
            kwargs["initial"] = subschema.get("default")
            fields[key] = forms.IntegerField(**kwargs)
        elif json_type == "number":
            kwargs["initial"] = subschema.get("default")
            fields[key] = forms.FloatField(**kwargs)
        elif json_type == "string":
            kwargs["initial"] = subschema.get("default")
            widget = forms.PasswordInput if subschema.get("writeOnly") else forms.TextInput
            fields[key] = forms.CharField(widget=widget, **kwargs)
        else:
            kwargs["initial"] = json.dumps(subschema.get("default")) if "default" in subschema else None
            fields[key] = forms.JSONField(**kwargs)

    if definition.dryrun_supported:
        fields["_dryrun"] = forms.BooleanField(
            required=False, label="Dry run", help_text="Execute without making changes."
        )

    form_class = type("RemoteJobRunInputForm", (forms.Form,), fields)
    return form_class(data=data)


def run_form_to_inputs(definition, form):
    """Convert cleaned dynamic-form data back into an inputs dict for submission."""
    inputs = {}
    for key, value in form.cleaned_data.items():
        if key == "_dryrun":
            continue
        if value in (None, "", []):
            continue
        if hasattr(value, "values_list"):  # model multiple choice -> uuid list
            inputs[key] = [str(pk) for pk in value.values_list("pk", flat=True)]
        elif hasattr(value, "pk"):
            inputs[key] = str(value.pk)
        else:
            inputs[key] = value
    dryrun = bool(form.cleaned_data.get("_dryrun"))
    return inputs, dryrun
