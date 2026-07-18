"""remote-jobs-gateway: stateless WebSocket <-> Redis pub/sub bridge for nautobot-app-remote-jobs.

The gateway terminates worker TLS WebSockets, authenticates each session against
the Nautobot app's internal verify-session endpoint, and bridges JSON-RPC 2.0
frames to Redis pub/sub channels. It holds no business logic, no database
access, and no secrets other than the internal gateway auth token.
"""

__version__ = "0.1.0"
