"""Dispatch tests: submission, claims, leases, reaper, cancel."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from nautobot.users.models import Token

from nautobot_remote_jobs.cancel import cancel_run
from nautobot_remote_jobs.choices import RunStateChoices
from nautobot_remote_jobs.constants import REDACTED_PLACEHOLDER
from nautobot_remote_jobs.dispatch import claims
from nautobot_remote_jobs.dispatch.reaper import reap_expired
from nautobot_remote_jobs.dispatch.submission import (
    SubmissionError,
    redact_inputs,
    submit_run,
)
from nautobot_remote_jobs.models import RemoteJobRun
from nautobot_remote_jobs.tests.helpers import (
    make_definition,
    make_user,
    make_worker,
    make_zone,
)


class SubmissionTest(TestCase):
    def setUp(self):
        self.zone = make_zone()
        self.user = make_user()

    def test_disabled_definition_rejected(self):
        definition = make_definition(zone=self.zone, enabled=False)
        with self.assertRaises(SubmissionError):
            submit_run(definition, self.user, {})

    def test_dryrun_unsupported_rejected(self):
        definition = make_definition(zone=self.zone)
        with self.assertRaises(SubmissionError):
            submit_run(definition, self.user, {}, dryrun=True)

    def test_input_validation(self):
        definition = make_definition(
            zone=self.zone,
            input_schema={
                "type": "object",
                "required": ["count"],
                "properties": {"count": {"type": "integer"}},
            },
        )
        with self.assertRaises(SubmissionError):
            submit_run(definition, self.user, {"count": "not-an-int"})
        with self.assertRaises(SubmissionError):
            submit_run(definition, self.user, {})

    def test_pinned_submission_with_worker(self):
        make_worker(self.zone)
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        self.assertEqual(run.state, RunStateChoices.PENDING)
        self.assertEqual(run.zone, self.zone)
        self.assertEqual(run.job_result.name, f"[remote] {definition.name}")

    def test_no_capacity_policy_none_fails_dispatch(self):
        definition = make_definition(zone=self.zone)  # zone has no workers
        run = submit_run(definition, self.user, {})
        self.assertEqual(run.state, RunStateChoices.FAILED_DISPATCH)

    def test_writeonly_inputs_redacted(self):
        make_worker(self.zone)
        definition = make_definition(
            zone=self.zone,
            input_schema={
                "type": "object",
                "properties": {"password": {"type": "string", "writeOnly": True}},
            },
        )
        run = submit_run(definition, self.user, {"password": "hunter2"})
        self.assertEqual(run.inputs["password"], REDACTED_PLACEHOLDER)

    def test_redact_inputs_helper(self):
        schema = {"properties": {"a": {"writeOnly": True}, "b": {}}}
        out = redact_inputs(schema, {"a": "x", "b": "y"})
        self.assertEqual(out, {"a": REDACTED_PLACEHOLDER, "b": "y"})


class ClaimTest(TestCase):
    def setUp(self):
        self.zone = make_zone()
        self.user = make_user()
        self.worker = make_worker(self.zone)
        self.definition = make_definition(zone=self.zone)

    def _submit(self, **kwargs):
        return submit_run(self.definition, self.user, {}, **kwargs)

    def test_claim_delivers_offer_with_token(self):
        run = self._submit()
        offers = claims.claim_runs(self.worker, max_offers=1)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["run_id"], str(run.pk))
        self.assertIn("@sha256:", offer["image"])
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.CLAIMED)
        self.assertEqual(run.worker, self.worker)
        self.assertIsNotNone(run.lease_expires_at)
        token = Token.objects.get(key=offer["token"])
        self.assertEqual(token.user, self.user)
        self.assertIsNotNone(token.expires)

    def test_disabled_worker_claims_nothing(self):
        self._submit()
        self.worker.enabled = False
        self.assertEqual(claims.claim_runs(self.worker), [])

    def test_capability_mismatch_not_claimed(self):
        from nautobot_remote_jobs.tests.helpers import make_run

        self.definition.capabilities = ["ssh-access"]
        self.definition.save()
        make_run(self.definition, user=self.user)  # PENDING, bypassing submission capacity gate
        self.assertEqual(claims.claim_runs(self.worker), [])
        self.worker.capabilities = ["ssh-access"]
        self.worker.save()
        self.assertEqual(len(claims.claim_runs(self.worker)), 1)

    def test_singleton_queues_behind_active(self):
        self.definition.singleton = True
        self.definition.save()
        self._submit()
        self._submit()
        offers = claims.claim_runs(self.worker, max_offers=2)
        self.assertEqual(len(offers), 1)
        states = sorted(RemoteJobRun.objects.values_list("state", flat=True))
        self.assertEqual(states, [RunStateChoices.CLAIMED, RunStateChoices.PENDING])

    def test_status_renews_lease_and_starts(self):
        run = self._submit()
        claims.claim_runs(self.worker)
        result = claims.report_status(self.worker, str(run.pk), state=RunStateChoices.RUNNING)
        self.assertFalse(result["cancel_requested"])
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.RUNNING)

    def test_complete_success_deletes_token(self):
        run = self._submit()
        offer = claims.claim_runs(self.worker)[0]
        claims.report_status(self.worker, str(run.pk), state=RunStateChoices.RUNNING)
        result = claims.complete_run(
            self.worker,
            str(run.pk),
            RunStateChoices.SUCCESS,
            exit_code=0,
            image_digest_executed=self.definition.image_digest,
        )
        self.assertTrue(result["ok"])
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.SUCCESS)
        self.assertIsNone(run.scoped_token)
        self.assertFalse(Token.objects.filter(key=offer["token"]).exists())

    def test_complete_is_idempotent(self):
        run = self._submit()
        claims.claim_runs(self.worker)
        claims.complete_run(self.worker, str(run.pk), RunStateChoices.SUCCESS)
        result = claims.complete_run(self.worker, str(run.pk), RunStateChoices.SUCCESS)
        self.assertTrue(result["ok"])

    def test_foreign_worker_rejected(self):
        run = self._submit()
        claims.claim_runs(self.worker)
        other = make_worker(self.zone, name="worker-2")
        with self.assertRaises(claims.ClaimError):
            claims.report_status(other, str(run.pk), state=RunStateChoices.RUNNING)


class ReaperTest(TestCase):
    def setUp(self):
        self.zone = make_zone()
        self.user = make_user()
        self.worker = make_worker(self.zone)
        self.definition = make_definition(zone=self.zone)

    def test_expired_lease_abandoned(self):
        run = submit_run(self.definition, self.user, {})
        claims.claim_runs(self.worker)
        RemoteJobRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        reaped = reap_expired()
        self.assertEqual(reaped, 1)
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.ABANDONED)
        self.assertIsNone(run.scoped_token)

    def test_abandoned_retries_when_configured(self):
        self.definition.retry_max = 1
        self.definition.save()
        run = submit_run(self.definition, self.user, {})
        claims.claim_runs(self.worker)
        RemoteJobRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        reap_expired()
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.PENDING)
        self.assertEqual(run.attempt, 2)
        self.assertIsNone(run.worker)


class CancelTest(TestCase):
    def setUp(self):
        self.zone = make_zone()
        self.user = make_user()
        self.worker = make_worker(self.zone)
        self.definition = make_definition(zone=self.zone)

    def test_cancel_pending(self):
        run = submit_run(self.definition, self.user, {})
        outcome = cancel_run(run, user=self.user)
        self.assertIn("before any worker", outcome)
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.CANCELLED)

    def test_cancel_running_publishes(self):
        run = submit_run(self.definition, self.user, {})
        claims.claim_runs(self.worker)
        claims.report_status(self.worker, str(run.pk), state=RunStateChoices.RUNNING)
        with mock.patch("nautobot_remote_jobs.dispatch.notify.publish_cancel") as publish:
            outcome = cancel_run(run, user=self.user)
        publish.assert_called_once()
        self.assertIn("job.cancel", outcome)
        # Cancel flag is now visible to the job.status poll.
        result = claims.report_status(self.worker, str(run.pk), state=RunStateChoices.RUNNING)
        self.assertTrue(result["cancel_requested"])

    def test_cancel_with_offline_worker_reaps(self):
        run = submit_run(self.definition, self.user, {})
        claims.claim_runs(self.worker)
        self.worker.last_seen = timezone.now() - timedelta(seconds=600)
        self.worker.save()
        outcome = cancel_run(run, user=self.user)
        self.assertIn("ABANDONED", outcome)
        run.refresh_from_db()
        self.assertEqual(run.state, RunStateChoices.ABANDONED)

    def test_cancel_parent_cancels_children_not_abandon(self):
        # A fan-out parent (worker=None) must cancel its children, not be
        # abandoned/re-queued into a zombie (regression: code review #5).
        from nautobot_remote_jobs.tests.helpers import make_run

        self.definition.retry_max = 2  # would trigger the zombie re-queue if abandoned
        self.definition.save()
        parent = make_run(self.definition, user=self.user)
        parent.state = RunStateChoices.RUNNING
        parent.save(update_fields=["state"])
        child = make_run(self.definition, user=self.user, parent=parent)
        claims.claim_runs(self.worker)  # child -> CLAIMED
        child.refresh_from_db()
        self.assertEqual(child.state, RunStateChoices.CLAIMED)

        with mock.patch("nautobot_remote_jobs.dispatch.notify.publish_cancel") as publish:
            outcome = cancel_run(parent, user=self.user)
        self.assertIn("child run", outcome)
        parent.refresh_from_db()
        # Parent is terminal (not re-queued PENDING) and the child was signalled.
        self.assertEqual(parent.state, RunStateChoices.TERMINATED)
        publish.assert_called_once()

    def test_offer_carries_inputs_and_schema(self):
        # build_job_offer must include inputs + input_schema so the worker can
        # inject them into the container (regression: code review #1).
        self.definition.input_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        self.definition.save()
        submit_run(self.definition, self.user, {"n": 5})
        offers = claims.claim_runs(self.worker)
        self.assertEqual(offers[0]["inputs"], {"n": 5})
        self.assertEqual(offers[0]["input_schema"], self.definition.input_schema)
