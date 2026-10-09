# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded admission and cancellation records for shared MCP tool calls."""

import threading
import time
from dataclasses import dataclass

from serena.util.tool_call import ToolCall, ToolCallBusyError


class DuplicateToolCallError(ValueError):
    """An already known operation ID, which must never execute a second time."""


class SharedMCPCalls:
    """Session-scoped call records retained through their replay window.

    Unknown cancellations leave tombstones so cancellation arriving before the POST
    still prevents execution. Results and project objects are never retained here.
    """

    _RETENTION_SECONDS = 60.0

    @dataclass(frozen=True)
    class _Entry:
        call: ToolCall
        expires_at: float

    def __init__(self, max_timeout: float, capacity: int = 4096) -> None:
        self._max_timeout = max_timeout
        self._capacity = capacity
        self._entries: dict[tuple[str, str], SharedMCPCalls._Entry] = {}
        self._lock = threading.Lock()

    def _make_room(self) -> None:
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now and not entry.call.running]
        for key in expired:
            del self._entries[key]
        if len(self._entries) >= self._capacity:
            raise ToolCallBusyError("Shared Serena call capacity exhausted; the operation was not submitted.")

    def register(self, session_id: str, request_id: str, deadline: float | None = None) -> ToolCall:
        """Admit an operation once, interpreting a wire deadline as UTC epoch seconds."""
        timeout = self._max_timeout if deadline is None else min(self._max_timeout, max(0.0, deadline - time.time()))
        call = ToolCall(timeout, request_id)
        call.check()
        key = (session_id, request_id)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                # report cancellation-before-dispatch as not-started rather than a generic duplicate
                existing.call.check()
                raise DuplicateToolCallError(f"Tool call {request_id} is already known; outcome={existing.call.outcome}. Not replaying it.")
            self._make_room()
            self._entries[key] = self._Entry(call, time.monotonic() + timeout + self._RETENTION_SECONDS)
        return call

    def cancel(self, session_id: str, request_id: str) -> str:
        """Cancel this session's operation, recording early delivery when necessary."""
        key = (session_id, request_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._make_room()
                entry = self._Entry(
                    ToolCall(self._max_timeout, request_id),
                    time.monotonic() + self._max_timeout + self._RETENTION_SECONDS,
                )
                self._entries[key] = entry
            entry.call.cancel()
            return entry.call.outcome
