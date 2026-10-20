"""Fail-closed evidence and admission control for agent-requested daemon recovery."""

# SPDX-License-Identifier: GPL-3.0-or-later

import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Protocol


class DaemonRecoveryRefusedError(RuntimeError):
    """Recovery cannot be authorized from the available execution evidence."""


@dataclass(frozen=True)
class RecoveryScope:
    project_root: str
    context: str
    session_id: str


@dataclass(frozen=True)
class RecoveryEvidence:
    request_id: str
    tool_name: str
    read_only: bool
    running: bool
    overdue_seconds: float


class RecoveryEvidenceSource(Protocol):
    def get_recovery_evidence(self, scope: RecoveryScope) -> RecoveryEvidence:
        """Inspect the caller's worker and reject any unrelated or pending work."""
        ...


@dataclass(frozen=True)
class _Observation:
    scope: RecoveryScope
    request_id: str
    first_seen: float


@dataclass(frozen=True)
class _Lease:
    scope: RecoveryScope
    token: str
    expires_at: float


class DaemonRecovery:
    """Authorize only persistently overdue retrievals, excluding concurrent HTTP work.

    Observation, claim, and admission use the same lock. A successful claim stops
    new daemon requests until restart or lease expiry; a failed coordinator therefore
    cannot leave the daemon permanently closed to work. No caller can lower the
    evidence thresholds or bypass them with a force flag.
    """

    OVERDUE_SECONDS = 30.0
    OBSERVATION_SECONDS = 2.0
    LEASE_SECONDS = 60.0
    _SAFE_READS = frozenset(
        {
            "get_symbols_overview",
            "find_symbol",
            "find_referencing_symbols",
            "find_declaration",
            "find_implementations",
            "find_file",
            "list_dir",
            "read_file",
            "search_for_pattern",
        }
    )

    def __init__(self, source: RecoveryEvidenceSource) -> None:
        self._source = source
        self._lock = threading.Lock()
        self._active_requests = 0
        self._observation: _Observation | None = None
        self._lease: _Lease | None = None

    def _has_lease(self) -> bool:
        if self._lease is not None and time.monotonic() >= self._lease.expires_at:
            self._lease = None
        return self._lease is not None

    @contextmanager
    def admit(self) -> Iterator[None]:
        """Admit a daemon request unless an authorized restart is pending."""
        with self._lock:
            if self._has_lease():
                raise DaemonRecoveryRefusedError("Daemon recovery is in progress; this operation was not admitted.")
            self._active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_requests -= 1

    def _evidence(self, scope: RecoveryScope) -> RecoveryEvidence:
        # require server-owned execution evidence, not client timeout reports or tool hints alone
        if self._has_lease():
            raise DaemonRecoveryRefusedError("Another daemon recovery is already in progress.")
        if self._active_requests:
            raise DaemonRecoveryRefusedError("Daemon requests are still active; a blocked, otherwise idle daemon is not established.")
        evidence = self._source.get_recovery_evidence(scope)
        if (
            not evidence.running
            or evidence.overdue_seconds < self.OVERDUE_SECONDS
            or not evidence.read_only
            or evidence.tool_name not in self._SAFE_READS
        ):
            raise DaemonRecoveryRefusedError(
                "Recovery requires a retrieval-only worker still running at least "
                f"{self.OVERDUE_SECONDS:g}s beyond its deadline. Slow calls, edits, shell commands, and REPL work do not qualify."
            )
        return evidence

    def observe(self, scope: RecoveryScope) -> RecoveryEvidence:
        """Record the first positive observation of the caller's overdue retrieval."""
        with self._lock:
            try:
                evidence = self._evidence(scope)
            except DaemonRecoveryRefusedError:
                self._observation = None
                raise
            previous = self._observation
            if previous is None or (previous.scope, previous.request_id) != (scope, evidence.request_id):
                self._observation = _Observation(scope, evidence.request_id, time.monotonic())
            return evidence

    def claim(self, scope: RecoveryScope, request_id: str) -> str:
        """Recheck the same overdue worker and atomically close admission for recovery."""
        with self._lock:
            try:
                evidence = self._evidence(scope)
                observed = self._observation
                if (
                    observed is None
                    or (observed.scope, observed.request_id) != (scope, request_id)
                    or evidence.request_id != request_id
                    or time.monotonic() - observed.first_seen < self.OBSERVATION_SECONDS
                ):
                    raise DaemonRecoveryRefusedError("The same blocked call must be observed twice, at least two seconds apart.")
            except DaemonRecoveryRefusedError:
                self._observation = None
                raise
            token = secrets.token_hex(16)
            self._lease = _Lease(scope, token, time.monotonic() + self.LEASE_SECONDS)
            self._observation = None
            return token

    def release(self, scope: RecoveryScope, token: str) -> None:
        """Release only the matching caller's recovery lease."""
        with self._lock:
            if self._lease is not None and self._lease.scope == scope and secrets.compare_digest(self._lease.token, token):
                self._lease = None
