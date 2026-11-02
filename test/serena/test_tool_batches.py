# SPDX-License-Identifier: GPL-3.0-or-later

"""Bulk MCP requests preserve ordered outcomes, edit safety, and backend contracts."""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import LanguageBackend, SerenaConfig
from serena.project import Project
from serena.repl.api.fs_api import FsApi
from serena.repl.api.lsp_api import LspApi
from serena.tools import (
    CreateTextFileTool,
    FindSymbolTool,
    GetSymbolsOverviewTool,
    MovePathTool,
    ReadMemoryTool,
    ReplaceSymbolBodyTool,
    SafeDeleteSymbol,
    WriteMemoryTool,
)
from serena.util.tool_call import ToolCall, ToolCallContext, ToolCallInterrupted
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_process import LanguageServerTerminatedException


@pytest.fixture
def batch_agent(tmp_path: Path) -> MagicMock:
    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.get_file_system_project.return_value = project
    agent.get_memory_manager.return_value = project.memory_manager
    agent.serena_config.default_max_tool_answer_chars = 10000
    return agent


@pytest.mark.parametrize(
    "invalid_request",
    [
        {"relative_path": "later.txt", "content": 42},
        {"relative_path": "later.txt", "content": "later", "overwrite": True},
        {"relative_path": "later.txt"},
    ],
)
def test_invalid_later_request_rejects_batch_before_overwriting_existing_file(batch_agent, tmp_path, invalid_request):
    target = tmp_path / "first.txt"
    target.write_text("original", encoding="utf-8")

    with pytest.raises(ValueError):
        CreateTextFileTool(batch_agent).get_apply_fn()(files=[{"relative_path": "first.txt", "content": "changed"}, invalid_request])

    assert target.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "later.txt").exists()


def test_response_metadata_budget_rejects_batch_before_writes(batch_agent, tmp_path):
    with pytest.raises(ValueError):
        CreateTextFileTool(batch_agent).get_apply_fn()(
            files=[
                {"relative_path": "first.txt", "content": "first"},
                {"relative_path": "a" * 6000, "content": "later"},
            ],
            max_answer_chars=4096,
        )

    assert not (tmp_path / "first.txt").exists()


def test_read_batch_keeps_successful_memories_on_both_sides_of_missing_item(batch_agent):
    writer = WriteMemoryTool(batch_agent)
    writer.apply("first", "# First\nKnown information")
    writer.apply("last", "# Last\nOther information")

    report = json.loads(
        ReadMemoryTool(batch_agent).get_apply_fn()(memories=[{"memory_name": "first"}, {"memory_name": "missing"}, {"memory_name": "last"}])
    )

    results = report["results"]
    assert [item["index"] for item in results] == [0, 1, 2]
    assert [item["status"] for item in results] == ["completed", "error", "completed"]
    assert results[0]["result"] == "# First\nKnown information"
    assert "missing" in results[1]["error"]
    assert results[2]["result"] == "# Last\nOther information"


def test_dependent_moves_observe_the_preceding_item_in_input_order(batch_agent, tmp_path):
    (tmp_path / "original.txt").write_text("preserved data", encoding="utf-8")

    report = json.loads(
        MovePathTool(batch_agent).get_apply_fn()(
            moves=[
                {"source_path": "original.txt", "destination_path": "intermediate.txt"},
                {"source_path": "intermediate.txt", "destination_path": "finished.txt"},
            ]
        )
    )

    assert [item["status"] for item in report["results"]] == ["completed", "completed"]
    assert (tmp_path / "finished.txt").read_text(encoding="utf-8") == "preserved data"
    assert not (tmp_path / "original.txt").exists()
    assert not (tmp_path / "intermediate.txt").exists()


def test_write_failure_reports_uncertain_item_and_does_not_start_later_writes(batch_agent, tmp_path, monkeypatch):
    def notify_after_write(self, *paths):
        if "uncertain.txt" in paths:
            raise RuntimeError("disk changed but synchronization failed")

    monkeypatch.setattr(FsApi, "_mark_file_system_dirty_if_project_path", notify_after_write)
    report = json.loads(
        CreateTextFileTool(batch_agent).get_apply_fn()(
            files=[
                {"relative_path": "first.txt", "content": "first"},
                {"relative_path": "uncertain.txt", "content": "possibly applied"},
                {"relative_path": "untouched.txt", "content": "must not run"},
            ]
        )
    )

    assert [item["status"] for item in report["results"]] == ["completed", "unknown", "not_applied"]
    assert "synchronization failed" in report["results"][1]["error"]
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
    assert (tmp_path / "uncertain.txt").read_text(encoding="utf-8") == "possibly applied"
    assert not (tmp_path / "untouched.txt").exists()


def test_cancellation_after_a_write_prevents_the_next_item(batch_agent, tmp_path, monkeypatch):
    tool_call = ToolCall(timeout=30)

    def cancel_after_write(self, *paths):
        tool_call.cancel()

    monkeypatch.setattr(FsApi, "_mark_file_system_dirty_if_project_path", cancel_after_write)
    with ToolCallContext(tool_call).activate(), pytest.raises(ToolCallInterrupted):
        CreateTextFileTool(batch_agent).get_apply_fn()(
            files=[
                {"relative_path": "first.txt", "content": "first"},
                {"relative_path": "untouched.txt", "content": "must not run"},
            ]
        )

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "untouched.txt").exists()


def test_output_budget_retains_every_outcome_and_explicitly_truncates_payloads(batch_agent):
    content = '"\\\n\U0001f642' * 2000
    writer = WriteMemoryTool(batch_agent)
    for index in range(7):
        writer.apply(f"long{index}", content, max_chars=100000)

    response = ReadMemoryTool(batch_agent).get_apply_fn()(
        memories=[{"memory_name": f"long{index}"} for index in range(7)] + [{"memory_name": "missing"}],
        max_answer_chars=4096,
    )
    results = json.loads(response)["results"]

    assert len(response) <= 4096
    assert [item["index"] for item in results] == list(range(8))
    assert [item["status"] for item in results] == ["completed"] * 7 + ["error"]
    assert all(item["truncated"] for item in results[:7])
    assert all(content.startswith(item["result"]) for item in results[:7])
    assert results[-1]["error"]


def test_safe_delete_refusal_is_a_completed_request_not_a_claimed_edit(batch_agent, tmp_path, monkeypatch):
    target = tmp_path / "module.py"
    source = "def used():\n    pass\n"
    target.write_text(source, encoding="utf-8")
    project = batch_agent.get_active_project_or_raise()
    monkeypatch.setattr(project, "ls_sync_file_system_changes", lambda: None)
    retriever = MagicMock()
    symbol = retriever.find_unique.return_value
    symbol.relative_path = "module.py"
    symbol.get_name_path.return_value = "used"
    symbol.line = 0
    symbol.column = 4
    retriever.get_language_server.return_value.request_references.return_value = [
        {"relativePath": "caller.py", "range": {"start": {"line": 2, "character": 0}}}
    ]
    monkeypatch.setattr(LspApi, "_create_symbol_retriever", lambda self: retriever)

    report = json.loads(SafeDeleteSymbol(batch_agent).get_apply_fn()(symbols=[{"relative_path": "module.py", "name_path_pattern": "used"}]))

    result = report["results"][0]
    assert result["status"] == "completed"
    assert "Cannot delete" in result["result"]
    assert "caller.py" in result["result"]
    assert target.read_text(encoding="utf-8") == source


def test_terminated_language_server_is_propagated_to_existing_read_recovery(batch_agent):
    terminated = SolidLSPException("language server died", LanguageServerTerminatedException("exited", LanguageServerId.PYTHON))

    with patch.object(LspApi, "find_symbol", side_effect=terminated), pytest.raises(SolidLSPException) as error:
        FindSymbolTool(batch_agent).get_apply_fn()(queries=[{"name_path_pattern": "Foo"}, {"name_path_pattern": "Bar"}])

    assert error.value is terminated


def test_jetbrains_bulk_overviews_preserve_external_identifiers_and_per_file_defaults(batch_agent):
    backend_tool = LanguageBackend.JETBRAINS.get_lsp_tool_class_replacements()[GetSymbolsOverviewTool]
    client = MagicMock()
    client.get_symbols_overview.side_effect = [
        {"symbols": [{"name_path": "RemoteClass", "type": "class", "relative_path": "<ext>/Library.java"}]},
        {"symbols": [{"name_path": "local_function", "type": "function", "relative_path": "module.py"}]},
    ]
    with patch("serena.repl.api.jb_api.JetBrainsPluginClient.from_project") as connect:
        connect.return_value.__enter__.return_value = client
        report = json.loads(
            backend_tool(batch_agent).get_apply_fn()(files=[{"relative_path": "&lt;ext&gt;/Library.java"}, {"relative_path": "module.py"}])
        )

    assert [item["status"] for item in report["results"]] == ["completed", "completed"]
    assert "RemoteClass" in report["results"][0]["result"]
    assert "local_function" in report["results"][1]["result"]
    assert client.get_symbols_overview.call_args_list == [
        call(relative_path="<ext>/Library.java", depth=1, include_file_documentation=False),
        call(relative_path="module.py", depth=0, include_file_documentation=False),
    ]


@pytest.mark.python
def test_live_python_batches_read_edit_and_refresh_symbols_across_files(tmp_path: Path):
    ty_path = shutil.which("ty")
    assert ty_path is not None, "Install the locked dev dependencies to provide the ty language server"
    project_root = tmp_path / "project"
    config_dir = project_root / ".serena"
    config_dir.mkdir(parents=True)
    (config_dir / "project.yml").write_text("project_name: bulk-symbol-editing\nlanguage_servers: [python_ty]\n", encoding="utf-8")
    symbols = ["alpha", "beta"]
    for index, name in enumerate(symbols, start=1):
        (project_root / f"{name}.py").write_text(f"def {name}() -> int:\n    return {index}\n", encoding="utf-8")
    config = SerenaConfig(ls_specific_settings={"python_ty": {"ls_path": ty_path}}).with_headless_mode_overrides()
    agent = SerenaAgent(project=str(project_root), serena_config=config)
    try:
        agent.execute_task(lambda: None)
        overview = json.loads(
            agent.get_tool(GetSymbolsOverviewTool).apply_ex(
                log_call=False, catch_exceptions=False, files=[{"relative_path": f"{name}.py"} for name in symbols]
            )
        )
        assert [row["status"] for row in overview["results"]] == ["completed", "completed"]
        assert all(name in row["result"] for name, row in zip(symbols, overview["results"], strict=True))

        queries = [{"name_path_pattern": name, "relative_path": f"{name}.py", "include_body": True} for name in symbols]
        finder = agent.get_tool(FindSymbolTool)
        before = json.loads(finder.apply_ex(log_call=False, catch_exceptions=False, queries=queries))
        assert [row["status"] for row in before["results"]] == ["completed", "completed"]
        for index, row in enumerate(before["results"], start=1):
            assert f"return {index}" in json.loads(row["result"])[0]["body"]

        replacements = [
            {"name_path": name, "relative_path": f"{name}.py", "body": f"def {name}() -> int:\n    return {index * 10}"}
            for index, name in enumerate(symbols, start=1)
        ]
        edited = json.loads(
            agent.get_tool(ReplaceSymbolBodyTool).apply_ex(log_call=False, catch_exceptions=False, replacements=replacements)
        )
        assert [row["status"] for row in edited["results"]] == ["completed", "completed"]
        for replacement in replacements:
            assert (project_root / replacement["relative_path"]).read_text(encoding="utf-8").rstrip("\n") == replacement["body"]

        refreshed = json.loads(finder.apply_ex(log_call=False, catch_exceptions=False, queries=queries))
        assert [row["status"] for row in refreshed["results"]] == ["completed", "completed"]
        for replacement, row in zip(replacements, refreshed["results"], strict=True):
            assert json.loads(row["result"])[0]["body"].strip() == replacement["body"]
    finally:
        agent.on_shutdown(timeout=5)
