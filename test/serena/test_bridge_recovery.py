# SPDX-License-Identifier: GPL-3.0-or-later

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from serena.mcp_bridge import SerenaMCPBridge
from serena.shared_mcp_recovery import DaemonRecoveryRefusedError, RecoveryScope
from serena.util.tool_call import AsyncToolCallRunner


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def bridge():
    instance = SerenaMCPBridge.__new__(SerenaMCPBridge)
    instance.project_root = "/repo"
    instance.context = SimpleNamespace(name="codex")
    instance.session_id = "chat"
    instance.client = object()
    instance._recovery_lock = threading.Lock()
    instance._daemon = Mock()
    return instance


@pytest.mark.anyio
async def test_bridge_recovery_bypasses_saturated_tool_workers_and_keeps_loop_responsive(bridge, monkeypatch):
    # saturate the ordinary adapters; recovery has its own single control worker
    monkeypatch.setattr(AsyncToolCallRunner, "_slots", threading.BoundedSemaphore(0))
    entered, release = threading.Event(), threading.Event()
    client = SimpleNamespace(pid=1234)

    def recover(scope):
        assert scope == RecoveryScope("/repo", "codex", "chat")
        entered.set()
        assert release.wait(3)
        return client

    bridge._daemon.recover_blocked.side_effect = recover
    results = []

    async def run():
        results.append(await bridge.restart_daemon())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run)
        with anyio.fail_after(2):
            while not entered.is_set():
                await anyio.sleep(0.01)
        with pytest.raises(ToolError, match="already in progress"):
            await bridge.restart_daemon()
        release.set()
    assert bridge.client is client
    assert "pid=1234" in results[0]


@pytest.mark.anyio
async def test_bridge_refusal_preserves_client_and_reports_blocker(bridge):
    original = bridge.client
    bridge._daemon.recover_blocked.side_effect = DaemonRecoveryRefusedError("No confirmed blockage")
    with pytest.raises(ToolError, match="No confirmed blockage"):
        await bridge.restart_daemon()
    assert bridge.client is original
