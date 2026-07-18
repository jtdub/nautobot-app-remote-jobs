"""Worker-facing REST endpoints (SPEC 5) and the gateway internal verify endpoint."""

import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from nautobot_remote_jobs.api.authentication import (
    EnrollmentTokenAuthentication,
    ScopedRunTokenAuthentication,
    WorkerSessionAuthentication,
)
from nautobot_remote_jobs.api.ingestion import (
    UnknownRunError,
    validate_batch_limits,
    write_console_batch,
    write_log_batch,
)
from nautobot_remote_jobs.crypto import derive_session_secret, verify_handshake
from nautobot_remote_jobs.models import RemoteJobRun, RunArtifact, Worker

logger = logging.getLogger(__name__)


class EnrollView(APIView):
    """POST /enroll/: exchange an enrollment token for a worker identity (SPEC 7.1)."""

    authentication_classes = [EnrollmentTokenAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        enrollment_token = getattr(request, "enrollment_token", None)
        if enrollment_token is None:
            return Response({"detail": "Enrollment token required."}, status=status.HTTP_401_UNAUTHORIZED)
        name = request.data.get("name")
        if not name:
            return Response({"detail": "'name' is required."}, status=status.HTTP_400_BAD_REQUEST)
        if Worker.objects.filter(name=name).exists():
            return Response({"detail": f"Worker '{name}' already exists."}, status=status.HTTP_409_CONFLICT)
        worker = Worker(
            name=name,
            zone=enrollment_token.zone,
            capabilities=request.data.get("capabilities") or [],
            capacity=int(request.data.get("capacity") or 4),
            agent_version=request.data.get("agent_version") or "",
            last_seen=timezone.now(),
        )
        worker.save()
        session_secret = derive_session_secret(worker)
        worker.identity_fingerprint = Worker.fingerprint(session_secret)
        worker.save(update_fields=["identity_fingerprint"])
        enrollment_token.used_at = timezone.now()
        enrollment_token.worker = worker
        enrollment_token.save(update_fields=["used_at", "worker"])
        logger.info("Worker %s enrolled into zone %s", worker.name, worker.zone)
        return Response(
            {"worker_id": str(worker.pk), "session_secret": session_secret},
            status=status.HTTP_201_CREATED,
        )


class VerifySessionView(APIView):
    """POST /internal/verify-session/: gateway handshake validation (SPEC 7.2, 8.4).

    Authenticated with the shared gateway internal token configured in
    PLUGINS_CONFIG["nautobot_remote_jobs"]["gateway_internal_token"].
    """

    authentication_classes = []  # custom shared-token check below
    permission_classes = [AllowAny]

    def post(self, request):
        configured = settings.PLUGINS_CONFIG.get("nautobot_remote_jobs", {}).get("gateway_internal_token")
        header = request.headers.get("Authorization", "")
        presented = header.split(" ", 1)[1].strip() if header.lower().startswith("token ") else ""
        import hmac as hmac_mod

        if not configured or not hmac_mod.compare_digest(str(configured), presented):
            return Response({"detail": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)
        worker_id = request.data.get("worker_id")
        try:
            worker = Worker.objects.select_related("zone").get(pk=worker_id)
        except (Worker.DoesNotExist, ValueError):
            return Response({"valid": False})
        valid = worker.enabled and verify_handshake(
            worker,
            worker_id,
            request.data.get("timestamp"),
            request.data.get("nonce"),
            request.data.get("signature"),
        )
        if valid:
            worker.touch()
        return Response({"valid": bool(valid), "worker_id": str(worker.pk), "zone_id": str(worker.zone_id)})


class WorkerRunScopedView(APIView):
    """Base for run-scoped ingestion endpoints.

    Accepts either the worker session credential (agent) or the run's scoped
    token (SDK inside the job container); in both cases the caller may only
    touch the addressed run (SPEC 5).
    """

    authentication_classes = [WorkerSessionAuthentication, ScopedRunTokenAuthentication]
    permission_classes = [AllowAny]

    def get_authorized_run(self, request, run_id):
        """Return the run when the caller is authorized for it, else None."""
        try:
            run = RemoteJobRun.objects.select_related("job_result", "worker").get(pk=run_id)
        except (RemoteJobRun.DoesNotExist, ValueError):
            return None
        worker = getattr(request, "worker", None)
        if worker is not None:
            return run if run.worker_id == worker.pk else None
        scoped_token = getattr(request, "scoped_token", None)
        if scoped_token is not None and run.scoped_token_id == scoped_token.pk:
            return run
        return None


class _BatchIngestView(WorkerRunScopedView):
    """Shared body for the log and console ingestion endpoints (SPEC 5, 10).

    Subclasses implement ``_write`` with write_log_batch or write_console_batch.
    """

    def _write(self, run_id, entries, client_sequence):
        raise NotImplementedError

    def post(self, request, pk):
        run = self.get_authorized_run(request, pk)
        if run is None:
            return Response({"detail": "Unknown run or not yours."}, status=status.HTTP_404_NOT_FOUND)
        entries = request.data if isinstance(request.data, list) else request.data.get("entries")
        error = validate_batch_limits(entries)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)
        sequence = self._batch_sequence(request)
        try:
            count = self._write(run.pk, entries, sequence)
        except UnknownRunError:
            return Response({"detail": "Unknown run."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"ingested": count})

    @staticmethod
    def _batch_sequence(request):
        """Read the client sequence from the header or either body key.

        The worker sink and SDK LogClient send the batch sequence as the
        ``client_sequence`` body key; the header and ``sequence`` key are
        accepted too so every producer's dedupe engages (SPEC 10).
        """
        header = request.headers.get("X-RemoteJobs-Sequence")
        if header is not None:
            return header
        if isinstance(request.data, dict):
            return request.data.get("sequence") or request.data.get("client_sequence")
        return None


class RunLogsView(_BatchIngestView):
    """POST /runs/{id}/logs/: batched structured log entries -> JobLogEntry (SPEC 5)."""

    def _write(self, run_id, entries, client_sequence):
        return write_log_batch(run_id, entries, client_sequence=client_sequence)


class RunConsoleView(_BatchIngestView):
    """POST /runs/{id}/console/: batched console output -> JobConsoleEntry (SPEC 5)."""

    def _write(self, run_id, entries, client_sequence):
        return write_console_batch(run_id, entries, client_sequence=client_sequence)


class RunArtifactsView(WorkerRunScopedView):
    """POST /runs/{id}/artifacts/: request an upload slot (SPEC 5, 4.8).

    Storage backends without presigned URL support fall back to direct upload
    through PUT /runs/{id}/artifacts/{artifact_id}/content/.
    """

    def post(self, request, pk):
        run = self.get_authorized_run(request, pk)
        if run is None:
            return Response({"detail": "Unknown run or not yours."}, status=status.HTTP_404_NOT_FOUND)
        name = request.data.get("name")
        if not name:
            return Response({"detail": "'name' is required."}, status=status.HTTP_400_BAD_REQUEST)
        artifact, _ = RunArtifact.objects.get_or_create(
            run=run,
            name=name,
            defaults={
                "content_type": request.data.get("content_type", ""),
                "size_bytes": int(request.data.get("size_bytes") or 0),
                "storage_path": f"remote-jobs/artifacts/{run.pk}/{name}",
            },
        )
        upload_url = request.build_absolute_uri(
            f"/api/plugins/remote-jobs/runs/{run.pk}/artifacts/{artifact.pk}/content/"
        )
        # requires_auth tells the client whether to send the worker/scoped-token
        # credential on the PUT. This app-owned endpoint always requires it; a
        # future presigned-URL backend would return the external URL with
        # requires_auth=False so the client omits the Authorization header (which
        # would otherwise break a presigned signature).
        return Response({"upload_url": upload_url, "artifact_id": str(artifact.pk), "requires_auth": True})


class RunArtifactContentView(WorkerRunScopedView):
    """PUT raw artifact bytes into the configured Django storage backend."""

    def put(self, request, pk, artifact_id):
        run = self.get_authorized_run(request, pk)
        if run is None:
            return Response({"detail": "Unknown run or not yours."}, status=status.HTTP_404_NOT_FOUND)
        try:
            artifact = RunArtifact.objects.get(pk=artifact_id, run=run)
        except RunArtifact.DoesNotExist:
            return Response({"detail": "Unknown artifact."}, status=status.HTTP_404_NOT_FOUND)
        from django.core.files.base import ContentFile
        from django.core.files.storage import storages

        data = request.body or b""
        storage = storages["default"]
        saved_path = storage.save(artifact.storage_path, ContentFile(data))
        artifact.storage_path = saved_path
        artifact.size_bytes = len(data)
        artifact.save(update_fields=["storage_path", "size_bytes"])
        return Response({"stored": saved_path})


class RunArtifactCompleteView(WorkerRunScopedView):
    """PUT /runs/{id}/artifacts/{artifact_id}/complete/: record the upload (SPEC 5)."""

    def put(self, request, pk, artifact_id):
        run = self.get_authorized_run(request, pk)
        if run is None:
            return Response({"detail": "Unknown run or not yours."}, status=status.HTTP_404_NOT_FOUND)
        try:
            artifact = RunArtifact.objects.get(pk=artifact_id, run=run)
        except RunArtifact.DoesNotExist:
            return Response({"detail": "Unknown artifact."}, status=status.HTTP_404_NOT_FOUND)
        artifact.sha256 = request.data.get("sha256", "")
        artifact.uploaded = True
        artifact.save(update_fields=["sha256", "uploaded"])
        return Response({"ok": True})


class WorkerSelfView(APIView):
    """GET /workers/self/: the worker's own record (SPEC 5)."""

    authentication_classes = [WorkerSessionAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        worker = getattr(request, "worker", None)
        if worker is None:
            return Response({"detail": "Worker session required."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(
            {
                "worker_id": str(worker.pk),
                "name": worker.name,
                "zone": {"id": str(worker.zone_id), "name": worker.zone.name},
                "enabled": worker.enabled,
                "draining": worker.draining,
                "capabilities": worker.capabilities,
                "capacity": worker.capacity,
                "status": worker.status,
            }
        )
