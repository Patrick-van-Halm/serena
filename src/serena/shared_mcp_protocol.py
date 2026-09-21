"""Shared MCP daemon/bridge protocol identity and runtime build fingerprint."""

# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
from functools import lru_cache
from pathlib import Path

SHARED_MCP_PROTOCOL_VERSION = 2

# Files whose implementation materially affects a long-lived shared daemon runtime.
# A bridge from a newer uvx archive must not silently reuse an older daemon that has
# different memory/correctness behaviour.
_RUNTIME_FILES = (
    "serena/project_server.py",
    "serena/agent.py",
    "serena/ls_manager.py",
    "serena/session.py",
    "serena/task_executor.py",
    "solidlsp/ls.py",
    "solidlsp/ls_process.py",
    "solidlsp/language_servers/typescript_language_server.py",
)


@lru_cache(maxsize=1)
def shared_mcp_build_id() -> str:
    src_root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for relative_path in _RUNTIME_FILES:
        path = src_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:20]
