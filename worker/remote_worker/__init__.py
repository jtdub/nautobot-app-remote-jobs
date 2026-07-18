"""remote-worker: async worker agent for nautobot-app-remote-jobs.

The agent connects outbound to the control-plane gateway over TLS WebSocket,
speaks JSON-RPC 2.0, claims job offers, and executes them in hardened
containers via a pluggable container runtime (Docker via aiodocker in v1).
"""

__version__ = "0.1.0"

#: Value reported as ``agent_version`` at enrollment and in ``worker.hello``.
AGENT_VERSION = f"remote-worker/{__version__}"
