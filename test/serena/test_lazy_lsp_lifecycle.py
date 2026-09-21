import threading
from time import monotonic
from unittest.mock import MagicMock

from serena.agent import SerenaAgent
from serena.config.serena_config import LanguageBackend
from serena.project import Project


def _bare_project(timeout: float = 600.0) -> Project:
    project = Project.__new__(Project)
    project.project_root = "/project"
    project.project_config = MagicMock(project_name="project")
    project.serena_config = MagicMock(
        language_server_idle_timeout_seconds=timeout,
        language_server_lazy_start=True,
    )
    project.language_server_manager = None
    project._language_server_manager_init_error = None
    project._language_server_manager_lock = threading.RLock()
    project._language_server_activity_lock = threading.Lock()
    project._language_server_active_operations = 0
    project._language_server_last_activity = monotonic()
    project._language_server_idle_stop_event = threading.Event()
    project._language_server_idle_thread = None
    project._agent = None
    return project


def test_language_server_starts_on_first_activity(monkeypatch) -> None:
    project = _bare_project()
    manager = MagicMock()

    def create() -> MagicMock:
        project.language_server_manager = manager
        return manager

    monkeypatch.setattr(project, "_create_language_server_manager_unlocked", create)

    assert project.language_server_manager is None
    with project.language_server_activity() as leased:
        assert leased is manager
        assert project._language_server_active_operations == 1

    assert project.language_server_manager is manager
    assert project._language_server_active_operations == 0


def test_idle_language_server_stops_and_releases_manager() -> None:
    project = _bare_project(timeout=10.0)
    manager = MagicMock()
    project.language_server_manager = manager
    project._language_server_last_activity = 100.0

    assert project._maybe_stop_idle_language_server(now=109.9) is False
    assert project._maybe_stop_idle_language_server(now=110.0) is True
    manager.stop_all.assert_called_once_with(save_cache=True)
    assert project.language_server_manager is None


def test_active_symbolic_operation_blocks_idle_shutdown() -> None:
    project = _bare_project(timeout=1.0)
    manager = MagicMock()
    project.language_server_manager = manager
    project._language_server_last_activity = 0.0
    project._language_server_active_operations = 1

    assert project._maybe_stop_idle_language_server(now=100.0) is False
    manager.stop_all.assert_not_called()
    assert project.language_server_manager is manager


def test_agent_defers_lsp_initialization_when_lazy() -> None:
    agent = SerenaAgent.__new__(SerenaAgent)
    agent._active_project = MagicMock(project_name="project")
    agent._language_backend = LanguageBackend.LSP
    agent.serena_config = MagicMock(language_server_lazy_start=True)
    agent.reset_language_server_manager = MagicMock()

    agent._init_active_project_language_backend()

    agent.reset_language_server_manager.assert_not_called()


def test_agent_eager_lsp_initialization_remains_available() -> None:
    agent = SerenaAgent.__new__(SerenaAgent)
    agent._active_project = MagicMock(project_name="project")
    agent._language_backend = LanguageBackend.LSP
    agent.serena_config = MagicMock(language_server_lazy_start=False)
    agent.reset_language_server_manager = MagicMock()

    agent._init_active_project_language_backend()

    agent.reset_language_server_manager.assert_called_once_with()
