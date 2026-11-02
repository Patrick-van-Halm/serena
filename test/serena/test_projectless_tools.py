# SPDX-License-Identifier: GPL-3.0-or-later

"""File and shell operations without a configured Serena project."""

import base64
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp_types import CallToolResult, EmbeddedResource

from serena.agent import SerenaAgent
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import LanguageBackend, SerenaConfig
from serena.repl.api.edit_api import EditApi
from serena.repl.api.fs_api import FsApi
from serena.tools import ToolCallError


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[SerenaAgent]:
    root = tmp_path / "workspace"
    root.mkdir()
    startup = getattr(request, "param", "cwd")
    explicit_directory = startup == "directory"
    monkeypatch.chdir(tmp_path if explicit_directory else root)
    config = SerenaConfig(
        language_backend=LanguageBackend.JETBRAINS if startup == "jetbrains" else LanguageBackend.LSP,
        default_modes=["editing"],
        included_optional_tools=["serena_repl"],
        ignored_paths=["ignored.txt"],
    ).with_headless_mode_overrides()
    instance = SerenaAgent(
        project=str(root) if explicit_directory else None,
        serena_config=config,
        context=SerenaAgentContext.from_name("desktop-app"),
    )
    try:
        yield instance
    finally:
        instance.on_shutdown(timeout=5)


@pytest.mark.parametrize("agent", ["cwd", "directory", "jetbrains"], indirect=True)
def test_edit_notes_and_run_commands_without_creating_a_project(
    agent: SerenaAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # keep relative file paths and shell cwd tied to the selected directory
    root = tmp_path / "workspace"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    agent.get_tool_by_name("create_text_file").apply_ex(
        relative_path="notes/readme.txt", content="draft note\nsecond line\n", catch_exceptions=False
    )
    agent.get_tool_by_name("replace_content").apply_ex(
        relative_path="notes/readme.txt", needle="draft", repl="final", mode="literal", catch_exceptions=False
    )
    assert (
        agent.get_tool_by_name("read_file").apply_ex(relative_path="notes/readme.txt", start_line=0, end_line=0, catch_exceptions=False)
        == "final note"
    )

    # line edits use the local text editor without starting a language backend
    assert (
        agent.get_tool_by_name("read_file").apply_ex(relative_path="notes/readme.txt", start_line=1, end_line=1, catch_exceptions=False)
        == "second line"
    )
    EditApi(agent).replace_lines("notes/readme.txt", 1, 1, "edited line")

    # discover the edited file through ordinary filesystem tools
    listing = json.loads(agent.get_tool_by_name("list_dir").apply_ex(relative_path="notes", recursive=False, catch_exceptions=False))
    assert [Path(path).as_posix() for path in listing["files"]] == ["notes/readme.txt"]
    found = json.loads(agent.get_tool_by_name("find_file").apply_ex(file_mask="*.txt", relative_path="notes", catch_exceptions=False))
    assert [Path(path).as_posix() for path in found["files"]] == ["notes/readme.txt"]
    matches = json.loads(agent.get_tool_by_name("search_for_pattern").apply_ex(substring_pattern="final note", catch_exceptions=False))
    assert [Path(path).as_posix() for path in matches] == ["notes/readme.txt"]

    # batch tools and original-byte reads use the same workspace
    replaced = json.loads(
        agent.get_tool_by_name("replace_contents").apply_ex(
            replacements=[{"relative_path": "notes/readme.txt", "needle": "final", "repl": "ready", "mode": "literal"}],
            catch_exceptions=False,
        )
    )
    assert replaced["status"] == "applied"
    read = json.loads(agent.get_tool_by_name("read_files").apply_ex(files=[{"relative_path": "notes/readme.txt"}], catch_exceptions=False))
    assert read["results"][0]["content"] == "ready note\nedited line\n"
    directories = json.loads(agent.get_tool_by_name("list_dirs").apply_ex(directories=[{"relative_path": "notes"}], catch_exceptions=False))
    assert [Path(path).as_posix() for path in directories["results"][0]["files"]] == ["notes/readme.txt"]
    raw = agent.get_tool_by_name("read_file_raw").apply_ex(relative_path="notes/readme.txt", catch_exceptions=False)
    assert isinstance(raw, CallToolResult)
    assert isinstance(raw.content[1], EmbeddedResource)
    assert base64.b64decode(raw.content[1].resource.blob) == (root / "notes/readme.txt").read_bytes()

    # shell defaults and relative overrides share the stable workspace root
    command = "cd" if sys.platform == "win32" else "pwd"
    for cwd, expected in [(None, root), ("notes", root / "notes")]:
        result = json.loads(agent.get_tool_by_name("execute_shell_command").apply_ex(command=command, cwd=cwd, catch_exceptions=False))
        assert result["return_code"] == 0
        assert Path(result["stdout"].strip()).resolve() == expected.resolve()
    assert agent.get_active_project() is None
    configuration = agent.get_tool_by_name("get_current_config").apply_ex(catch_exceptions=False)
    assert f"Working directory: {root.resolve()}" in configuration
    assert not (root / ".serena").exists()
    assert not (elsewhere / "notes").exists()


def test_projectless_editing_preserves_access_and_ignore_rules(agent: SerenaAgent, tmp_path: Path) -> None:
    # deny outside-root reads and edits until the existing global opt-in is enabled
    root = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    for name, arguments in [
        ("read_file", {"relative_path": "../outside.txt"}),
        ("create_text_file", {"relative_path": "../outside.txt", "content": "denied"}),
        ("replace_content", {"relative_path": "../outside.txt", "needle": "original", "repl": "denied", "mode": "literal"}),
    ]:
        with pytest.raises(ToolCallError, match="full_access_mode"):
            agent.get_tool_by_name(name).apply_ex(**arguments, catch_exceptions=False)
    assert outside.read_text(encoding="utf-8") == "original"
    agent.serena_config.full_access_mode = True
    agent.get_tool_by_name("create_text_file").apply_ex(relative_path=str(outside), content="allowed", catch_exceptions=False)
    assert agent.get_tool_by_name("read_file").apply_ex(relative_path=str(outside), catch_exceptions=False) == "allowed"

    # searches and filtered listings honor global ignores without a project config
    (root / "visible.txt").write_text("needle", encoding="utf-8")
    (root / "ignored.txt").write_text("needle", encoding="utf-8")
    matches = json.loads(agent.get_tool_by_name("search_for_pattern").apply_ex(substring_pattern="needle", catch_exceptions=False))
    assert set(matches) == {"visible.txt"}
    listing = json.loads(
        agent.get_tool_by_name("list_dir").apply_ex(relative_path=".", recursive=False, skip_ignored_files=True, catch_exceptions=False)
    )
    assert listing["files"] == ["visible.txt"]


def test_language_specific_operations_still_require_a_project(agent: SerenaAgent, tmp_path: Path) -> None:
    # source text remains readable while semantic reads and edits require activation
    source = tmp_path / "workspace/module.py"
    source.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    assert "def greet" in agent.get_tool_by_name("read_file").apply_ex(relative_path="module.py", catch_exceptions=False)
    for name, arguments in [
        ("find_symbol", {"name_path_pattern": "greet"}),
        ("get_symbols_overview", {"relative_path": "module.py"}),
        ("replace_symbol_body", {"relative_path": "module.py", "name_path": "greet", "body": "def greet(): pass"}),
        ("search_for_pattern", {"substring_pattern": "greet", "restrict_search_to_code_files": True}),
    ]:
        with pytest.raises(ToolCallError, match="No active project"):
            agent.get_tool_by_name(name).apply_ex(**arguments, catch_exceptions=False)

    # direct facade calls retain the same boundary when bypassing classic tools
    with pytest.raises(ValueError, match="No active project"):
        FsApi(agent).search_for_pattern("greet", restrict_search_to_code_files=True)
    with pytest.raises(ValueError, match="No active project"):
        EditApi(agent).replace_symbol_body("greet", "module.py", "def greet(): pass")
    assert source.read_text(encoding="utf-8") == "def greet():\n    return 'hello'\n"


def test_projectless_repl_can_read_and_edit_text(agent: SerenaAgent, tmp_path: Path) -> None:
    result = agent.get_tool_by_name("serena_repl").apply_ex(
        session="projectless-notes",
        code='s.fs.create_text_file("note.txt", "draft")\ns.edit.replace_content("note.txt", "draft", "ready", "literal")\ns.fs.read_file("note.txt")',
        catch_exceptions=False,
    )
    assert result == "ready"
    assert (tmp_path / "workspace/note.txt").read_text(encoding="utf-8") == "ready"
    error = agent.get_tool_by_name("serena_repl").apply_ex(
        session="projectless-notes", code='s.lsp.find_symbol("greet")', catch_exceptions=False
    )
    assert "No active project" in error


def test_activation_switches_file_root_and_applies_project_permissions(agent: SerenaAgent, tmp_path: Path) -> None:
    # start with a writable unconfigured directory
    agent.get_tool_by_name("create_text_file").apply_ex(relative_path="note.txt", content="workspace", catch_exceptions=False)
    root = tmp_path / "configured"
    (root / ".serena").mkdir(parents=True)
    (root / ".serena/project.yml").write_text("project_name: configured\nlanguage_servers: [python]\nread_only: true\n", encoding="utf-8")
    (root / "note.txt").write_text("configured project", encoding="utf-8")
    (root / "module.py").write_text("def greet(): pass\n", encoding="utf-8")

    # activate an actual project and use its root and language configuration
    agent.activate_project_from_path_or_name(str(root))
    agent.execute_task(lambda: None)
    assert agent.get_tool_by_name("read_file").apply_ex(relative_path="note.txt", catch_exceptions=False) == "configured project"
    matches = json.loads(
        agent.get_tool_by_name("search_for_pattern").apply_ex(
            substring_pattern="greet", restrict_search_to_code_files=True, catch_exceptions=False
        )
    )
    assert set(matches) == {"module.py"}
    with pytest.raises(ToolCallError, match="is not active"):
        agent.get_tool_by_name("create_text_file").apply_ex(relative_path="note.txt", content="denied", catch_exceptions=False)
    assert (root / "note.txt").read_text(encoding="utf-8") == "configured project"
    assert (tmp_path / "workspace/note.txt").read_text(encoding="utf-8") == "workspace"
