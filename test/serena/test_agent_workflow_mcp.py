# SPDX-License-Identifier: GPL-3.0-or-later

"""Installed application checks for batched directory listing over MCP transports."""

import json
import subprocess
import sys
import time

import anyio
import pytest
import requests
import yaml
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .test_mcp_sdk_v2 import BOOTSTRAP, isolated_project as _isolated_project, text_of

isolated_project = _isolated_project


@pytest.mark.parametrize("bridge", [False, True], ids=["server", "bridge"])
@pytest.mark.parametrize("excluded", [False, True], ids=["enabled", "excluded"])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_list_dirs_transport_and_policy(isolated_project, bridge: bool, excluded: bool, mode: str):
    fixture = isolated_project
    (fixture.project / "first").mkdir()
    (fixture.project / "second").mkdir()
    (fixture.project / "first/a.txt").write_text("a", encoding="utf-8")
    (fixture.project / "second/b.txt").write_text("b", encoding="utf-8")
    if excluded:
        context_path = fixture.home / "contexts/codex.yml"
        context = yaml.safe_load(context_path.read_text(encoding="utf-8"))
        context["excluded_tools"] = ["list_dir"]
        context_path.write_text(yaml.safe_dump(context), encoding="utf-8")

    daemon = None
    with (fixture.home / "directory-daemon.log").open("w+", encoding="utf-8") as log:
        try:
            if bridge:
                daemon = subprocess.Popen(
                    [sys.executable, "-c", BOOTSTRAP, "start-project-server"],
                    env=fixture.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )
                deadline = time.monotonic() + 30
                while True:
                    try:
                        response = requests.get(
                            f"http://127.0.0.1:{fixture.port}/heartbeat",
                            headers={"Authorization": "Bearer sdk-integration-test-secret"},
                            timeout=0.3,
                        )
                        if response.ok:
                            break
                    except requests.RequestException:
                        pass
                    if daemon.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(f"Daemon did not become ready: {log.read()}")
                    time.sleep(0.05)

            command = "start-mcp-bridge" if bridge else "start-mcp-server"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-c", BOOTSTRAP, command, "--project", str(fixture.project), "--context", "codex"],
                env=fixture.environment,
            )

            async def exercise():
                with anyio.fail_after(45):
                    async with Client(parameters, mode=mode, read_timeout_seconds=20) as client:
                        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                        assert "list_dirs" in tools
                        assert tools["list_dirs"].annotations.read_only_hint
                        result = await client.call_tool(
                            "list_dirs",
                            {
                                "directories": [
                                    {"relative_path": "first"},
                                    {"relative_path": "missing"},
                                    {"relative_path": "second", "recursive": True},
                                ]
                            },
                        )
                        if excluded:
                            assert result.is_error and "list_dir to be enabled" in text_of(result)
                        else:
                            assert not result.is_error, text_of(result)
                            rows = json.loads(text_of(result))["results"]
                            assert rows[0]["files"] == ["first/a.txt"]
                            assert "FileNotFoundError" in rows[1]["error"]
                            assert rows[2]["files"] == ["second/b.txt"]
                        following = await client.call_tool("read_file", {"relative_path": "hello.txt"})
                        assert not following.is_error and "SDK migration works" in text_of(following)

            anyio.run(exercise)
        finally:
            if daemon is not None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)
