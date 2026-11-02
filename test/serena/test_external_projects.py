"""External-project permission checks and transfer of REPL results through the project server."""

import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from werkzeug.serving import make_server

from serena.agent import SerenaAgent
from serena.config.serena_config import ApiInclusionDefinition, LanguageBackend, ProjectConfig, SerenaConfig
from serena.project import Project
from serena.project_server import ProjectServer, ProjectServerClient
from serena.repl.api.lsp_api import LspApi, LspSymbolCollection
from serena.repl.external_project import ExternalProjectExecution
from serena.repl.facade import ApiScope, Facade
from serena.tools import SerenaReplTool
from solidlsp.ls_config import LanguageServerId
from test.conftest import language_server_tests_enabled
from test.serena.test_serena_agent import serena_config  # noqa: F401  (fixture)


@pytest.mark.parametrize("method_name", ["get_diagnostics_for_file", "get_diagnostics_for_symbol"])
def test_read_only_external_context_blocks_unity_diagnostic_refresh(tmp_path: Path, method_name: str) -> None:
    # use a real project configuration and facade metadata without starting a language server
    project_config = ProjectConfig(project_name="unity-project", language_servers=[LanguageServerId.UNITY])
    project = Project(
        project_root=str(tmp_path),
        project_config=project_config,
        serena_config=SerenaConfig(gui_log_window=False, web_dashboard=False),
    )
    agent = MagicMock(spec=SerenaAgent)
    agent.get_active_project_or_raise.return_value = project
    agent.get_language_backend.return_value = LanguageBackend.LSP
    scope = ApiScope()
    scope.process(ApiInclusionDefinition(included_apis=[f"lsp.{method_name}"]))
    facade = Facade.from_api(LspApi(agent), scope)
    method = facade.get_method(method_name)
    read_only = ExternalProjectExecution(project.project_name, read_only=True, agent=agent)

    # compilation is refused in a read-only context while ordinary symbol queries remain available
    with pytest.raises(PermissionError, match="Diagnostics.*read-only"):
        read_only.check_call_permission(method)
    read_only.check_call_permission(facade.get_method("find_symbol"))
    ExternalProjectExecution(project.project_name, read_only=False, agent=agent).check_call_permission(method)

    # ordinary C# diagnostics and the JetBrains backend do not require this compilation permission
    project_config.language_servers = [LanguageServerId.CSHARP]
    read_only.check_call_permission(method)
    project_config.language_servers = [LanguageServerId.UNITY]
    project_config.language_backend = LanguageBackend.JETBRAINS
    agent.get_language_backend.return_value = LanguageBackend.JETBRAINS
    read_only.check_call_permission(method)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def project_server(serena_config: SerenaConfig) -> Iterator[tuple[ProjectServer, int]]:  # noqa: F811
    """
    Runs a project server (backed by a real agent) on a free port for the duration of the test.
    """
    config = serena_config
    port = _free_port()

    # construct the server around an agent with the test configuration (the constructor would load the user's configuration)
    server = ProjectServer.__new__(ProjectServer)
    server._agent = SerenaAgent(serena_config=config)
    server._loaded_projects_by_root = {}
    server._project_load_locks_by_root = {}
    server._active_project_lock = threading.Lock()
    server._loaded_projects_lock = threading.Lock()
    server._port = port
    server._host = "127.0.0.1"
    from flask import Flask

    server._app = Flask(__name__)
    server._setup_routes()

    http_server = make_server("127.0.0.1", port, server._app, threaded=True)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, port
    finally:
        http_server.shutdown()
        server._agent.on_shutdown(timeout=5)


@pytest.mark.python
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.PYTHON), reason="python tests are disabled in this environment")
def test_facade_method_results_are_transferred_from_the_project_server(project_server: tuple[ProjectServer, int]) -> None:
    server, port = project_server
    client = ProjectServerClient(server.get_serena_config(), port=port)
    result = client.call_facade_method("test_repo_python", "lsp", "find_symbol", ["create_user"], {"include_body": True})

    # the result is a self-contained object which can be processed and rendered locally
    assert isinstance(result, LspSymbolCollection)
    assert [s.name for s in result.symbols] == ["create_user"]
    assert result.symbols[0].body.startswith("def create_user")
    assert "create_user" in result.represent()


@pytest.mark.python
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.PYTHON), reason="python tests are disabled in this environment")
def test_external_project_context_in_repl(
    project_server: tuple[ProjectServer, int],
    serena_config: SerenaConfig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, port = project_server
    monkeypatch.setattr(ProjectServer, "PORT", port)  # let the REPL's external project context use the test server

    # enable the optional "ext" facade
    serena_config.included_apis = ["ext"]

    # the querying agent has another project active and queries the python test project
    serena_config.auth_secret = server.get_auth_secret()
    agent = SerenaAgent(project="test_repo_typescript", serena_config=serena_config)
    agent.execute_task(lambda: None)
    try:
        tool = agent.get_tool(SerenaReplTool)
        session_id = agent.create_session().session_id
        code = (
            'with s.ext.read_project_context("test_repo_python"):\n'
            '    result = s.lsp.find_symbol("create_user")\n'
            "[s.name for s in result.symbols]"
        )
        assert tool.apply(session_id, code) == "create_user"
        assert server._loaded_projects_by_root, "the operation was not executed by the project server"

        # the result persists and can be used after the context; the active project is restored
        assert "services.py" in tool.apply(session_id, "result.represent()")
        assert "test_repo_typescript" in agent.get_current_config_overview()
    finally:
        agent.on_shutdown(timeout=5)
