"""Worker-facing authentication classes (SPEC 5).

Two classes:
- EnrollmentTokenAuthentication: the plaintext enrollment token, valid only for
  POST /enroll/.
- WorkerSessionAuthentication: the worker session credential presented as
  ``Authorization: Token <session_secret>`` plus ``X-RemoteJobs-Worker-ID``.
  Secrets are derived server-side (see nautobot_remote_jobs.crypto), verified
  in constant time, and only ever transit TLS.
"""

import logging

from django.utils import timezone
from rest_framework import authentication, exceptions

from nautobot_remote_jobs.crypto import verify_session_secret
from nautobot_remote_jobs.models import Worker, WorkerEnrollmentToken

logger = logging.getLogger(__name__)

WORKER_ID_HEADER = "X-RemoteJobs-Worker-ID"


def _token_from_request(request):
    header = authentication.get_authorization_header(request).decode()
    if not header.lower().startswith("token "):
        return None
    return header.split(" ", 1)[1].strip()


class EnrollmentTokenAuthentication(authentication.BaseAuthentication):
    """Authenticates the enroll exchange only (SPEC 4.5, 7.1)."""

    def authenticate_header(self, request):
        return "Token"

    def authenticate(self, request):
        plaintext = _token_from_request(request)
        if not plaintext:
            return None
        token_hash = WorkerEnrollmentToken.hash_token(plaintext)
        try:
            enrollment_token = WorkerEnrollmentToken.objects.select_related("zone").get(token_hash=token_hash)
        except WorkerEnrollmentToken.DoesNotExist:
            return None
        if not enrollment_token.is_valid:
            raise exceptions.AuthenticationFailed("Enrollment token expired or already used.")
        # No user identity: enrollment grants nothing except the exchange.
        from django.contrib.auth.models import AnonymousUser

        request.enrollment_token = enrollment_token
        return (AnonymousUser(), enrollment_token)


class WorkerSessionAuthentication(authentication.BaseAuthentication):
    """Authenticates worker endpoints with the session credential (SPEC 7.2)."""

    def authenticate_header(self, request):
        return "Token"

    def authenticate(self, request):
        secret = _token_from_request(request)
        worker_id = request.headers.get(WORKER_ID_HEADER)
        if not secret or not worker_id:
            return None
        try:
            worker = Worker.objects.select_related("zone").get(pk=worker_id)
        except (Worker.DoesNotExist, ValueError):
            raise exceptions.AuthenticationFailed("Unknown worker.")
        if not verify_session_secret(worker, secret):
            raise exceptions.AuthenticationFailed("Invalid session credential.")
        if not worker.enabled:
            raise exceptions.AuthenticationFailed("Worker is disabled.")
        from django.contrib.auth.models import AnonymousUser

        request.worker = worker
        worker.touch()
        return (AnonymousUser(), worker)


class ScopedRunTokenAuthentication(authentication.BaseAuthentication):
    """Authenticates a job container using its per-run scoped token (SPEC 7.3).

    Used by the log/console/artifact endpoints so the SDK inside the container
    can ship logs directly with the only credential it holds. The view still
    verifies the token belongs to the addressed run.
    """

    def authenticate(self, request):
        key = _token_from_request(request)
        if not key:
            return None
        from nautobot.users.models import Token

        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist:
            return None
        if token.expires and token.expires < timezone.now():
            raise exceptions.AuthenticationFailed("Token expired.")
        request.scoped_token = token
        return (token.user, token)
