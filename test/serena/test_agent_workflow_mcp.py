# SPDX-License-Identifier: GPL-3.0-or-later

"""Installed application checks for batch directory and editing tools over MCP."""

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

from .test_mcp_sdk_v2 import BOOTSTRAP, text_of
from .test_mcp_sdk_v2 import isolated_project as _isolated_project

isolated_project = _isolated_project


@pytest.mark.parametrize("bridge", [False, True], ids=["server", "bridge"])
@pytest.mark.parametrize("policy", ["enabled", "excluded_legacy", "excluded_bulk", "fixed_legacy", "read_only"])
@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_batch_transport_and_policy(isolated_project, bridge: bool, policy: str, mode: str):
    fixture = isolated_project
    excluded = policy.startswith("excluded")
    editing_blocked = excluded or policy == "read_only"
    (fixture.project / "first").mkdir()
    (fixture.project / "second").mkdir()
    (fixture.project / "first/a.txt").write_text("a", encoding="utf-8")
    (fixture.project / "second/b.txt").write_text("b", encoding="utf-8")
    (fixture.project / "escaped.txt").write_text('"\\\t\n😀' * 2000, encoding="utf-8")
    if excluded or policy == "fixed_legacy":
        context_path = fixture.home / "contexts/codex.yml"
        context = yaml.safe_load(context_path.read_text(encoding="utf-8"))
        if policy == "fixed_legacy":
            context["fixed_tools"] = ["read_file", "list_dir", "replace_content", "initial_instructions", "read_file_raw"]
        else:
            context["excluded_tools"] = (
                ["list_dir", "replace_content", "read_file"]
                if policy == "excluded_legacy"
                else ["list_dirs", "replace_contents", "read_files"]
            )
        context_path.write_text(yaml.safe_dump(context), encoding="utf-8")

    if policy == "read_only":
        project_config_path = fixture.project / ".serena/project.yml"
        project_config = yaml.safe_load(project_config_path.read_text(encoding="utf-8"))
        project_config["read_only"] = True
        project_config_path.write_text(yaml.safe_dump(project_config), encoding="utf-8")

    daemon = None
    with (fixture.home / "batch-daemon.log").open("w+", encoding="utf-8") as log:
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
                        if policy == "fixed_legacy":
                            assert set(tools) == {
                                "read_files",
                                "list_dirs",
                                "replace_contents",
                                "initial_instructions",
                                "read_file_raw",
                            } | ({"restart_daemon"} if bridge else set())
                        if not excluded:
                            assert tools["list_dirs"].annotations.read_only_hint
                            assert tools["read_files"].annotations.read_only_hint
                        batch = await client.call_tool(
                            "read_files",
                            {"files": [{"relative_path": "first/a.txt"}, {"relative_path": "missing"}, {"relative_path": "second/b.txt"}]},
                        )
                        if excluded:
                            assert batch.is_error
                        else:
                            assert not batch.is_error, text_of(batch)
                            batch_rows = json.loads(text_of(batch))["results"]
                            assert batch_rows[0]["content"] == "a"
                            assert "FileNotFoundError" in batch_rows[1]["error"]
                            assert batch_rows[2]["content"] == "b"
                            ranged = await client.call_tool(
                                "read_files", {"files": [{"relative_path": "hello.txt", "start_line": 0, "end_line": 0}]}
                            )
                            assert not ranged.is_error, text_of(ranged)
                            assert json.loads(text_of(ranged))["results"][0]["content"] == "SDK migration works"
                            limited = await client.call_tool(
                                "read_files", {"files": [{"relative_path": "escaped.txt"}], "max_total_chars": 2048}
                            )
                            assert not limited.is_error, text_of(limited)
                            assert len(text_of(limited)) <= 2048
                            assert json.loads(text_of(limited))["results"][0]["truncated"]
                            invalid = await client.call_tool("read_files", {"files": [{"relative_path": "hello.txt", "start_line": True}]})
                            assert invalid.is_error
                            oversized = await client.call_tool(
                                "read_files", {"files": [{"relative_path": "hello.txt"}], "max_bytes_per_file": 1}
                            )
                            assert not oversized.is_error, text_of(oversized)
                            assert "max_bytes" in json.loads(text_of(oversized))["results"][0]["error"]
                        if not editing_blocked:
                            assert not tools["replace_contents"].annotations.read_only_hint
                        result = await client.call_tool(
                            "list_dirs",
                            {"directories": [{"relative_path": "first"}, {"relative_path": "missing"}, {"relative_path": "second"}]},
                        )
                        if excluded:
                            assert result.is_error
                        else:
                            assert not result.is_error, text_of(result)
                            rows = json.loads(text_of(result))["results"]
                            assert rows[0]["files"] == ["first/a.txt"]
                            assert "FileNotFoundError" in rows[1]["error"]
                            assert rows[2]["files"] == ["second/b.txt"]

                        edits = [
                            {"relative_path": "first/a.txt", "needle": "a", "repl": "x", "mode": "literal"},
                            {"relative_path": "first/a.txt", "needle": "x", "repl": "final", "mode": "literal"},
                            {"relative_path": "second/b.txt", "needle": "(b)", "repl": "$!1$!1", "mode": "regex"},
                        ]
                        preview = await client.call_tool("replace_contents", {"replacements": edits, "dry_run": True})
                        if editing_blocked:
                            assert preview.is_error
                        else:
                            assert not preview.is_error, text_of(preview)
                            assert (fixture.project / "first/a.txt").read_text() == "a"
                            failure = await client.call_tool(
                                "replace_contents",
                                {"replacements": [edits[0], {**edits[2], "needle": "missing"}]},
                            )
                            assert failure.is_error and '"status":"rejected"' in text_of(failure)
                            assert (fixture.project / "first/a.txt").read_text() == "a"
                            hashes = {row["relative_path"]: row["original_content_sha256"] for row in json.loads(text_of(preview))["files"]}
                            for edit in edits:
                                edit["expected_content_sha256"] = hashes[edit["relative_path"]]
                            applied = await client.call_tool("replace_contents", {"replacements": edits})
                            assert not applied.is_error, text_of(applied)
                            assert (fixture.project / "first/a.txt").read_text() == "final"
                            assert (fixture.project / "second/b.txt").read_text() == "bb"
                        if not excluded:
                            single_listing = await client.call_tool("list_dirs", {"directories": [{"relative_path": "first"}]})
                            assert not single_listing.is_error, text_of(single_listing)
                            assert json.loads(text_of(single_listing))["results"][0]["files"] == ["first/a.txt"]
                        if not editing_blocked:
                            single_edit = await client.call_tool(
                                "replace_contents",
                                {
                                    "replacements": [
                                        {
                                            "relative_path": "hello.txt",
                                            "needle": "works",
                                            "repl": "works singly",
                                            "mode": "literal",
                                            "expected_count": 1,
                                        }
                                    ]
                                },
                            )
                            assert not single_edit.is_error, text_of(single_edit)
                            assert (fixture.project / "hello.txt").read_text() == "SDK migration works singly\n"
                        if policy == "read_only":
                            assert (fixture.project / "first/a.txt").read_text() == "a"
                            assert (fixture.project / "second/b.txt").read_text() == "b"
                        if excluded:
                            following = await client.call_tool("initial_instructions", {})
                            assert not following.is_error, text_of(following)
                        else:
                            following = await client.call_tool("read_files", {"files": [{"relative_path": "hello.txt"}]})
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
