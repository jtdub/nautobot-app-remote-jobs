"""nautobot-remote-jobs-sdk: the library remote job code imports.

Typical usage::

    from nautobot_remote_jobs_sdk import job

    @job.main
    def run(ctx):
        ctx.logger.info("Starting", grouping="setup")
        password = ctx.secrets.get("tacacs-prod", access_type="Generic", secret_type="password")
        nb = ctx.api
        ...
"""

from . import job
from ._version import __version__
from .context import Context, ContextError, InputValidationError

__all__ = ["job", "Context", "ContextError", "InputValidationError", "__version__"]
