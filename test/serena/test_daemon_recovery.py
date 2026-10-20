# SPDX-License-Identifier: GPL-3.0-or-later

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from serena.shared_mcp_recovery import (
    DaemonRecovery,
    DaemonRecoveryRefusedError,
    RecoveryEvidence,
    RecoveryScope,
)


class EvidenceSource:
    def __init__(self):
        self.evidence = RecoveryEvidence("blocked-call", "find_symbol", True, True, 31.0)

    def get_recovery_evidence(self, scope):
        return self.evidence


@pytest.fixture
def recovery(monkeypatch):
    source = EvidenceSource()
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("serena.shared_mcp_recovery.time", SimpleNamespace(monotonic=lambda: clock.now))
    return DaemonRecovery(source), source, clock, RecoveryScope("/repo", "codex", "chat")


@pytest.mark.parametrize("changes", [
    {"running": False}, {"overdue_seconds": 0}, {"overdue_seconds": 29.9},
    {"read_only": False}, {"tool_name": "replace_symbol_body"},
    {"tool_name": "execute_shell_command"}, {"tool_name": "serena_repl"},
    {"tool_name": "unknown_tool"},
])
def test_unproven_or_modifying_work_cannot_authorize_restart(recovery, changes):
    controller, source, _, scope = recovery
    source.evidence = replace(source.evidence, **changes)
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.observe(scope)
    with controller.admit():
        pass


def test_second_observation_is_required_and_admission_stays_closed_until_release(recovery):
    controller, _, clock, scope = recovery
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.claim(scope, "blocked-call")
    controller.observe(scope)
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.claim(scope, "blocked-call")
    controller.observe(scope)
    clock.now += 2.1
    token = controller.claim(scope, "blocked-call")
    with pytest.raises(DaemonRecoveryRefusedError):
        with controller.admit():
            pytest.fail("Work was admitted during recovery")
    controller.release(scope, token)
    with controller.admit():
        pass


@pytest.mark.parametrize("change", ["completed", "different-call", "edit"])
def test_changed_evidence_cancels_restart_authorization(recovery, change):
    controller, source, clock, scope = recovery
    controller.observe(scope)
    clock.now += 3
    if change == "completed":
        source.evidence = replace(source.evidence, running=False)
    elif change == "different-call":
        source.evidence = replace(source.evidence, request_id="new-call")
    else:
        source.evidence = replace(source.evidence, read_only=False)
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.claim(scope, "blocked-call")
    with controller.admit():
        pass


def test_other_session_cannot_claim_or_release_recovery(recovery):
    controller, _, clock, scope = recovery
    other = replace(scope, session_id="other")
    controller.observe(scope)
    clock.now += 3
    with pytest.raises(DaemonRecoveryRefusedError):
        controller.claim(other, "blocked-call")
    controller.observe(scope)
    clock.now += 3
    token = controller.claim(scope, "blocked-call")
    controller.release(other, token)
    controller.release(scope, "wrong-token")
    with pytest.raises(DaemonRecoveryRefusedError):
        with controller.admit():
            pass
    controller.release(scope, token)
    with controller.admit():
        pass


def test_lost_coordinator_lease_expires_and_admits_requests_again(recovery):
    controller, _, clock, scope = recovery
    controller.observe(scope)
    clock.now += 3
    controller.claim(scope, "blocked-call")
    clock.now += 61
    with controller.admit():
        pass


def test_admitted_request_wins_race_against_recovery_claim(recovery):
    controller, _, clock, scope = recovery
    controller.observe(scope)
    clock.now += 3
    entered, finish = threading.Event(), threading.Event()

    def request():
        with controller.admit():
            entered.set()
            assert finish.wait(3)

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(1)
    try:
        with pytest.raises(DaemonRecoveryRefusedError):
            controller.claim(scope, "blocked-call")
    finally:
        finish.set()
        worker.join(3)
    with controller.admit():
        pass
