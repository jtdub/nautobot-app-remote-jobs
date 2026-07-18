"""Model tests: validation, state machine, JobResult sync."""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from nautobot.extras.choices import JobResultStatusChoices

from nautobot_remote_jobs.choices import RunStateChoices, WorkerStatusChoices, ZonePolicyChoices
from nautobot_remote_jobs.models import WorkerEnrollmentToken
from nautobot_remote_jobs.tests.helpers import (
    DIGEST,
    make_definition,
    make_run,
    make_worker,
    make_zone,
)


class JobDefinitionValidationTest(TestCase):
    def test_bad_digest_rejected(self):
        zone = make_zone()
        definition = make_definition(zone=zone, image_digest="sha256:short")
        with self.assertRaises(ValidationError) as ctx:
            definition.full_clean()
        self.assertIn("image_digest", ctx.exception.message_dict)

    def test_pinned_requires_default_zone(self):
        definition = make_definition(zone_policy=ZonePolicyChoices.PINNED)
        with self.assertRaises(ValidationError) as ctx:
            definition.full_clean()
        self.assertIn("default_zone", ctx.exception.message_dict)

    def test_invalid_input_schema_rejected(self):
        zone = make_zone()
        definition = make_definition(zone=zone, input_schema={"type": 42})
        with self.assertRaises(ValidationError) as ctx:
            definition.full_clean()
        self.assertIn("input_schema", ctx.exception.message_dict)

    def test_valid_definition_passes(self):
        zone = make_zone()
        definition = make_definition(
            zone=zone,
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        definition.full_clean()

    def test_image_with_digest(self):
        zone = make_zone()
        definition = make_definition(zone=zone)
        self.assertEqual(definition.image_with_digest, f"registry.example.com/jobs/rotate-admin@{DIGEST}")


class WorkerStatusTest(TestCase):
    def test_online_within_ttl(self):
        worker = make_worker(make_zone())
        self.assertEqual(worker.status, WorkerStatusChoices.ONLINE)

    def test_offline_past_ttl(self):
        worker = make_worker(make_zone())
        worker.last_seen = timezone.now() - timedelta(seconds=600)
        self.assertEqual(worker.status, WorkerStatusChoices.OFFLINE)

    def test_disabled_is_offline(self):
        worker = make_worker(make_zone(), enabled=False)
        self.assertEqual(worker.status, WorkerStatusChoices.OFFLINE)

    def test_draining(self):
        worker = make_worker(make_zone(), draining=True)
        self.assertEqual(worker.status, WorkerStatusChoices.DRAINING)


class EnrollmentTokenTest(TestCase):
    def test_generate_and_validate(self):
        zone = make_zone()
        token, plaintext = WorkerEnrollmentToken.generate(zone)
        self.assertTrue(token.is_valid)
        self.assertEqual(token.token_hash, WorkerEnrollmentToken.hash_token(plaintext))
        self.assertNotIn(plaintext, token.token_hash)

    def test_single_use_consumed(self):
        zone = make_zone()
        token, _ = WorkerEnrollmentToken.generate(zone)
        token.used_at = timezone.now()
        self.assertFalse(token.is_valid)

    def test_expired(self):
        zone = make_zone()
        token, _ = WorkerEnrollmentToken.generate(zone)
        token.expires = timezone.now() - timedelta(hours=1)
        self.assertFalse(token.is_valid)


class RunStateMachineTest(TestCase):
    def test_legal_path_to_success(self):
        zone = make_zone()
        run = make_run(make_definition(zone=zone))
        run.transition(RunStateChoices.CLAIMED)
        run.transition(RunStateChoices.RUNNING)
        run.transition(RunStateChoices.SUCCESS)
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.SUCCESS)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.job_result.status, JobResultStatusChoices.STATUS_SUCCESS)

    def test_illegal_transition_raises(self):
        zone = make_zone()
        run = make_run(make_definition(zone=zone))
        with self.assertRaises(ValueError):
            run.transition(RunStateChoices.SUCCESS)

    def test_terminated_sets_revocation(self):
        zone = make_zone()
        run = make_run(make_definition(zone=zone))
        run.transition(RunStateChoices.CLAIMED)
        run.transition(RunStateChoices.RUNNING)
        run.transition(RunStateChoices.TERMINATED)
        job_result = run.job_result
        job_result.refresh_from_db()
        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_REVOKED)
        self.assertEqual(job_result.revocation_type, "terminated")

    def test_running_syncs_started(self):
        zone = make_zone()
        run = make_run(make_definition(zone=zone))
        run.transition(RunStateChoices.CLAIMED)
        run.transition(RunStateChoices.RUNNING)
        job_result = run.job_result
        job_result.refresh_from_db()
        self.assertEqual(job_result.status, JobResultStatusChoices.STATUS_STARTED)
        self.assertIsNotNone(job_result.date_started)

    def test_parent_state_derivation(self):
        zone = make_zone()
        definition = make_definition(zone=zone)
        parent = make_run(definition)
        parent.state = RunStateChoices.RUNNING
        parent.save()
        child1 = make_run(definition, parent=parent)
        child2 = make_run(definition, parent=parent)
        for child in (child1, child2):
            child.transition(RunStateChoices.CLAIMED)
            child.transition(RunStateChoices.RUNNING)
            child.transition(RunStateChoices.SUCCESS)
            child.refresh_parent_state()
        parent.refresh_from_db()
        self.assertEqual(parent.state, RunStateChoices.SUCCESS)

    def test_parent_failure_if_any_child_fails(self):
        zone = make_zone()
        definition = make_definition(zone=zone)
        parent = make_run(definition)
        parent.state = RunStateChoices.RUNNING
        parent.save()
        ok_child = make_run(definition, parent=parent)
        bad_child = make_run(definition, parent=parent)
        for child, final in ((ok_child, RunStateChoices.SUCCESS), (bad_child, RunStateChoices.FAILURE)):
            child.transition(RunStateChoices.CLAIMED)
            child.transition(RunStateChoices.RUNNING)
            child.transition(final)
            child.refresh_parent_state()
        parent.refresh_from_db()
        self.assertEqual(parent.state, RunStateChoices.FAILURE)
