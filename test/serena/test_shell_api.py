"""
Tests for the shell facade API.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project import Project
from serena.repl.api.shell_api import ShellApi
from serena.repl.facade import ApiScope, Facade
from serena.util.shell import execute_shell_command


@pytest.fixture
def api(tmp_path: Path) -> ShellApi:
    (tmp_path / "sub").mkdir()
    project = Project.load(str(tmp_path), serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False))
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = project
    agent.get_file_system_project.return_value = project
    agent.serena_config.default_max_tool_answer_chars = 10000
    return ShellApi(agent)


def test_facade_exposes_shell_command_as_editing_operation(api: ShellApi) -> None:
    facade = Facade.from_api(api, ApiScope())
    assert facade.name == "shell"
    assert facade.enabled_method_names == ["execute_shell_command"]
    assert facade.get_method("execute_shell_command").info.can_edit


def test_execute_shell_command(api: ShellApi, tmp_path: Path) -> None:
    print_cwd = "cd" if sys.platform == "win32" else "pwd"

    output = api.execute_shell_command(f"{print_cwd}")
    assert output.return_code == 0
    assert Path(output.stdout.strip()).resolve() == tmp_path.resolve()
    assert '"stdout"' in output.represent() and '"return_code"' in output.represent()

    # a relative working directory is resolved against the project root and must exist
    assert Path(api.execute_shell_command(print_cwd, cwd="sub").stdout.strip()).resolve() == (tmp_path / "sub").resolve()
    with pytest.raises(FileNotFoundError):
        api.execute_shell_command(print_cwd, cwd="missing")


def test_shell_absolute_cwd_remains_unrestricted(api: ShellApi, tmp_path: Path) -> None:
    """full_access_mode intentionally does not gate shell cwd."""
    print_cwd = "cd" if sys.platform == "win32" else "pwd"
    outside_dir = tmp_path.parent / f"{tmp_path.name}-shell-outside"
    outside_dir.mkdir(exist_ok=True)

    project = api._get_project()
    assert project.serena_config.full_access_mode is False

    output = api.execute_shell_command(print_cwd, cwd=str(outside_dir))
    assert Path(output.stdout.strip()).resolve() == outside_dir.resolve()


def test_execute_shell_command_timeout_terminates_process(api: ShellApi) -> None:
    api._agent.serena_config.tool_timeout = 0.2
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    with pytest.raises(TimeoutError, match="timed out"):
        api.execute_shell_command(command)


def test_argument_vector_keeps_shell_characters_literal(tmp_path: Path) -> None:
    arguments = ["project with spaces", "$(echo expanded)", 'quote"; & | %PATH% *']
    result = execute_shell_command(
        [sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))", *arguments],
        cwd=str(tmp_path),
        capture_stderr=True,
    )
    assert result.return_code == 0, result.stderr
    assert json.loads(result.stdout) == arguments
