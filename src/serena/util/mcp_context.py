# SPDX-License-Identifier: GPL-3.0-or-later
"""Stable connection identity for the SDK's request-scoped session proxies."""

import secrets
import threading
from weakref import WeakKeyDictionary

from mcp.server.connection import Connection
from mcp.server.mcpserver import Context


class MCPConnectionIdentity:
    """Keep legacy conversation state stable without retaining disconnected SDK connections."""

    _identities: WeakKeyDictionary[Connection, str] = WeakKeyDictionary()
    _lock = threading.Lock()

    @classmethod
    def session_id(cls, context: Context) -> str:
        """Return the identity of the connection, not its per-request session proxy."""
        # isolate this pinned SDK seam: its high-level Context has no public connection accessor
        connection = context.session._connection
        with cls._lock:
            identity = cls._identities.get(connection)
            if identity is None:
                identity = secrets.token_hex(16)
                cls._identities[connection] = identity
            return identity
