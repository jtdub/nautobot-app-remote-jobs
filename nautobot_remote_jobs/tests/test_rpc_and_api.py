"""RPC handler routing, crypto, and worker-facing API tests."""

import time
import uuid

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from nautobot_remote_jobs.crypto import (
    compute_signature,
    derive_session_secret,
    verify_handshake,
    verify_session_secret,
)
from nautobot_remote_jobs.dispatch.submission import submit_run
from nautobot_remote_jobs.models import Worker, WorkerEnrollmentToken
from nautobot_remote_jobs.rpc.handlers import dispatch_rpc
from nautobot_remote_jobs.tests.helpers import make_definition, make_user, make_worker, make_zone


class CryptoTest(TestCase):
    def setUp(self):
        self.worker = make_worker(make_zone())

    def test_derivation_is_stable_and_rotates(self):
        first = derive_session_secret(self.worker)
        self.assertEqual(first, derive_session_secret(self.worker))
        self.worker.secret_generation += 1
        self.assertNotEqual(first, derive_session_secret(self.worker))

    def test_handshake_roundtrip(self):
        secret = derive_session_secret(self.worker)
        timestamp = str(time.time())
        nonce = uuid.uuid4().hex
        signature = compute_signature(secret, str(self.worker.pk), timestamp, nonce)
        self.assertTrue(verify_handshake(self.worker, str(self.worker.pk), timestamp, nonce, signature))
        self.assertFalse(verify_handshake(self.worker, str(self.worker.pk), timestamp, nonce, "bad"))

    def test_stale_timestamp_rejected(self):
        secret = derive_session_secret(self.worker)
        timestamp = str(time.time() - 3600)
        nonce = uuid.uuid4().hex
        signature = compute_signature(secret, str(self.worker.pk), timestamp, nonce)
        self.assertFalse(verify_handshake(self.worker, str(self.worker.pk), timestamp, nonce, signature))

    def test_session_secret_verification(self):
        secret = derive_session_secret(self.worker)
        self.assertTrue(verify_session_secret(self.worker, secret))
        self.assertFalse(verify_session_secret(self.worker, "wrong"))


class RPCDispatchTest(TestCase):
    def setUp(self):
        self.zone = make_zone()
        self.worker = make_worker(self.zone)
        self.user = make_user()
        self.definition = make_definition(zone=self.zone)

    def _frame(self, method, params=None, request_id="req-1"):
        frame = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            frame["id"] = request_id
        return frame

    def test_unknown_method(self):
        response = dispatch_rpc(str(self.worker.pk), self._frame("nope"))
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_worker(self):
        response = dispatch_rpc(str(uuid.uuid4()), self._frame("job.claim"))
        self.assertEqual(response["error"]["code"], -32001)

    def test_hello_returns_config(self):
        response = dispatch_rpc(
            str(self.worker.pk),
            self._frame("worker.hello", {"agent_version": "1.2.3", "in_flight": []}),
        )
        self.assertIn("lease_seconds", response["result"])
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.agent_version, "1.2.3")

    def test_claim_and_complete_flow(self):
        run = submit_run(self.definition, self.user, {})
        response = dispatch_rpc(str(self.worker.pk), self._frame("job.claim", {"max": 1}))
        offers = response["result"]["offers"]
        self.assertEqual(len(offers), 1)
        response = dispatch_rpc(
            str(self.worker.pk),
            self._frame("job.status", {"run_id": str(run.pk), "state": "RUNNING"}),
        )
        self.assertIn("lease_expires_at", response["result"])
        response = dispatch_rpc(
            str(self.worker.pk),
            self._frame("job.complete", {"run_id": str(run.pk), "state": "SUCCESS", "exit_code": 0}),
        )
        self.assertTrue(response["result"]["ok"])

    def test_status_unknown_run_error(self):
        response = dispatch_rpc(
            str(self.worker.pk),
            self._frame("job.status", {"run_id": str(uuid.uuid4()), "state": "RUNNING"}),
        )
        self.assertEqual(response["error"]["code"], -32002)

    def test_rotate_changes_secret(self):
        before = derive_session_secret(self.worker)
        response = dispatch_rpc(str(self.worker.pk), self._frame("worker.rotate"))
        self.worker.refresh_from_db()
        after = response["result"]["session_secret"]
        self.assertNotEqual(before, after)
        self.assertTrue(verify_session_secret(self.worker, after))

    def test_notification_returns_none(self):
        response = dispatch_rpc(str(self.worker.pk), self._frame("job.claim", {"max": 1}, request_id=None))
        self.assertIsNone(response)


class WorkerAPITest(TestCase):
    BASE = "/api/plugins/remote-jobs"

    def setUp(self):
        self.client = APIClient()
        self.zone = make_zone()
        self.user = make_user()

    def test_enroll_flow(self):
        _, plaintext = WorkerEnrollmentToken.generate(self.zone)
        response = self.client.post(
            f"{self.BASE}/enroll/",
            {"name": "edge-worker", "capabilities": ["ssh-access"], "capacity": 2},
            format="json",
            HTTP_AUTHORIZATION=f"Token {plaintext}",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        worker = Worker.objects.get(pk=body["worker_id"])
        self.assertEqual(worker.zone, self.zone)
        self.assertTrue(verify_session_secret(worker, body["session_secret"]))
        # Single-use token is now consumed.
        response = self.client.post(
            f"{self.BASE}/enroll/",
            {"name": "other"},
            format="json",
            HTTP_AUTHORIZATION=f"Token {plaintext}",
        )
        self.assertEqual(response.status_code, 401)

    def test_enroll_requires_token(self):
        response = self.client.post(f"{self.BASE}/enroll/", {"name": "x"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_enroll_token_hash_not_in_api_response(self):
        # Security: the enrollment token hash must not be serialized to clients.
        from nautobot.users.models import Token

        self.user.is_superuser = True
        self.user.save()
        token = Token.objects.create(user=self.user)
        enroll_token, _ = WorkerEnrollmentToken.generate(self.zone)
        response = self.client.get(
            f"{self.BASE}/enrollment-tokens/{enroll_token.pk}/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn("token_hash", response.json())

    def test_worker_fingerprint_not_in_api_response(self):
        # Security: identity_fingerprint / secret_generation are credential
        # material and must not appear in worker API responses.
        from nautobot.users.models import Token

        self.user.is_superuser = True
        self.user.save()
        token = Token.objects.create(user=self.user)
        worker = make_worker(self.zone)
        response = self.client.get(
            f"{self.BASE}/workers/{worker.pk}/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertNotIn("identity_fingerprint", body)
        self.assertNotIn("secret_generation", body)

    def test_worker_self_with_session_auth(self):
        worker = make_worker(self.zone)
        secret = derive_session_secret(worker)
        response = self.client.get(
            f"{self.BASE}/workers/self/",
            HTTP_AUTHORIZATION=f"Token {secret}",
            HTTP_X_REMOTEJOBS_WORKER_ID=str(worker.pk),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], worker.name)

    def test_logs_ingestion_with_session_auth(self):
        worker = make_worker(self.zone)
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        from nautobot_remote_jobs.dispatch import claims

        claims.claim_runs(worker)
        secret = derive_session_secret(worker)
        response = self.client.post(
            f"{self.BASE}/runs/{run.pk}/logs/",
            [{"level": "info", "message": "hello", "grouping": "setup"}],
            format="json",
            HTTP_AUTHORIZATION=f"Token {secret}",
            HTTP_X_REMOTEJOBS_WORKER_ID=str(worker.pk),
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["ingested"], 1)
        self.assertEqual(run.job_result.job_log_entries.filter(message="hello").count(), 1)

    def test_client_sequence_body_key_dedupes(self):
        # Regression (code review #9): clients send the sequence as the
        # 'client_sequence' body key; the server must read it so at-least-once
        # retries dedupe instead of duplicating rows.
        worker = make_worker(self.zone)
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        from nautobot_remote_jobs.dispatch import claims

        claims.claim_runs(worker)
        secret = derive_session_secret(worker)
        body = {"client_sequence": 1, "entries": [{"level": "info", "message": "once"}]}
        headers = {
            "HTTP_AUTHORIZATION": f"Token {secret}",
            "HTTP_X_REMOTEJOBS_WORKER_ID": str(worker.pk),
        }
        first = self.client.post(f"{self.BASE}/runs/{run.pk}/logs/", body, format="json", **headers)
        self.assertEqual(first.json()["ingested"], 1)
        # Replay of the same client_sequence is dropped.
        second = self.client.post(f"{self.BASE}/runs/{run.pk}/logs/", body, format="json", **headers)
        self.assertEqual(second.json()["ingested"], 0)
        self.assertEqual(run.job_result.job_log_entries.filter(message="once").count(), 1)

    def test_logs_rejected_for_foreign_run(self):
        worker = make_worker(self.zone)
        other_worker = make_worker(self.zone, name="worker-other")
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        from nautobot_remote_jobs.dispatch import claims

        claims.claim_runs(worker)
        secret = derive_session_secret(other_worker)
        response = self.client.post(
            f"{self.BASE}/runs/{run.pk}/logs/",
            [{"level": "info", "message": "spoof"}],
            format="json",
            HTTP_AUTHORIZATION=f"Token {secret}",
            HTTP_X_REMOTEJOBS_WORKER_ID=str(other_worker.pk),
        )
        self.assertEqual(response.status_code, 404)

    def test_console_ingestion_with_scoped_token(self):
        worker = make_worker(self.zone)
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        from nautobot_remote_jobs.dispatch import claims

        offer = claims.claim_runs(worker)[0]
        response = self.client.post(
            f"{self.BASE}/runs/{run.pk}/console/",
            [{"output_type": "stdout", "text": "container says hi"}],
            format="json",
            HTTP_AUTHORIZATION=f"Token {offer['token']}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(run.job_result.job_console_entries.count(), 1)

    def test_artifact_name_path_traversal_sanitized(self):
        # Security: a traversal-laden artifact name must not escape the run's
        # own artifact prefix in the storage path.
        worker = make_worker(self.zone)
        definition = make_definition(zone=self.zone)
        run = submit_run(definition, self.user, {})
        from nautobot_remote_jobs.dispatch import claims
        from nautobot_remote_jobs.models import RunArtifact

        offer = claims.claim_runs(worker)[0]
        response = self.client.post(
            f"{self.BASE}/runs/{run.pk}/artifacts/",
            {"name": "../../../../etc/evil", "content_type": "text/plain", "size_bytes": 3},
            format="json",
            HTTP_AUTHORIZATION=f"Token {offer['token']}",
        )
        self.assertEqual(response.status_code, 200, response.content)
        artifact = RunArtifact.objects.get(pk=response.json()["artifact_id"])
        self.assertNotIn("..", artifact.storage_path)
        self.assertTrue(artifact.storage_path.startswith(f"remote-jobs/artifacts/{run.pk}/"))

    @override_settings()
    def test_verify_session_endpoint(self):
        from django.conf import settings

        settings.PLUGINS_CONFIG["nautobot_remote_jobs"]["gateway_internal_token"] = "gw-secret"
        try:
            worker = make_worker(self.zone)
            secret = derive_session_secret(worker)
            timestamp = str(time.time())
            nonce = uuid.uuid4().hex
            signature = compute_signature(secret, str(worker.pk), timestamp, nonce)
            response = self.client.post(
                f"{self.BASE}/internal/verify-session/",
                {
                    "worker_id": str(worker.pk),
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "signature": signature,
                },
                format="json",
                HTTP_AUTHORIZATION="Token gw-secret",
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["valid"])
            # Wrong gateway token is rejected outright.
            response = self.client.post(
                f"{self.BASE}/internal/verify-session/",
                {"worker_id": str(worker.pk)},
                format="json",
                HTTP_AUTHORIZATION="Token wrong",
            )
            self.assertEqual(response.status_code, 401)
        finally:
            settings.PLUGINS_CONFIG["nautobot_remote_jobs"]["gateway_internal_token"] = ""
