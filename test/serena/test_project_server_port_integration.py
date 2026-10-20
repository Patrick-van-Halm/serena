# SPDX-License-Identifier: GPL-3.0-or-later

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project_server import ProjectServer, ProjectServerClient
from serena.project_server_endpoint import ProjectServerAuthenticationError
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION


@pytest.fixture
def endpoint_server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200 if self.headers.get("Authorization") == "Bearer test-secret" else 401)
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "alive", "pid": 1234,
                "shared_mcp_protocol_version": SHARED_MCP_PROTOCOL_VERSION, "shared_mcp_build_id": "test-build",
            }).encode())

        def do_POST(self):
            self.send_response(200 if self.headers.get("Authorization") == "Bearer test-secret" else 401)
            self.end_headers()
            self.wfile.write(b"read-result")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)
    monkeypatch.setattr("serena.shared_mcp_client.shared_mcp_build_id", lambda: "test-build")
    try:
        yield server.server_port
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("use_environment", [False, True])
def test_both_http_clients_resolve_the_same_configured_port(endpoint_server, monkeypatch, use_environment) -> None:
    config = SerenaConfig(project_server_port=endpoint_server, auth_secret="test-secret")
    if use_environment:
        config.project_server_port = 1
        monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", str(endpoint_server))
    legacy = ProjectServerClient(config)
    shared = SharedMCPDaemonClient(config)
    assert legacy.query_project("repo", "read_file", "{}") == "read-result"
    assert shared.call_tool("repo", "codex", "session", "read_file", {}) == "read-result"
    if use_environment:
        assert config.project_server_port == 1


@pytest.mark.parametrize("client_class", [ProjectServerClient, SharedMCPDaemonClient])
def test_explicit_port_overrides_environment_in_both_clients(endpoint_server, monkeypatch, client_class) -> None:
    config = SerenaConfig(project_server_port=1, auth_secret="test-secret")
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", "2")
    assert client_class(config, port=endpoint_server) is not None


@pytest.mark.parametrize("client_class", [ProjectServerClient, SharedMCPDaemonClient])
def test_both_clients_report_endpoint_authentication_mismatch(endpoint_server, client_class) -> None:
    config = SerenaConfig(project_server_port=endpoint_server, auth_secret="wrong-secret")
    with pytest.raises(ProjectServerAuthenticationError, match=f"127.0.0.1:{endpoint_server}.*authentication rejected"):
        client_class(config)


@pytest.mark.parametrize("override", ["config", "environment", "explicit"])
def test_listener_uses_resolved_port(monkeypatch, override) -> None:
    config = SerenaConfig(project_server_port=25432)
    monkeypatch.setattr(SerenaConfig, "from_config_file", lambda: config)
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)
    expected = 25432
    explicit = None
    if override in {"environment", "explicit"}:
        monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", "25433")
        expected = 25433
    if override == "explicit":
        explicit = expected = 25434
    server = ProjectServer(port=explicit)
    listened = []
    monkeypatch.setattr(server._app, "run", lambda **kwargs: listened.append(kwargs["port"]))
    try:
        server.run()
    finally:
        server._mcp_runtime_stop_event.set()
    assert listened == [expected]


def test_global_port_round_trip_does_not_persist_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "serena_config.yml"
    path.write_text("projects: []\nproject_server_port: 25432\nauth_secret: test-secret\n")
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", lambda: str(path))
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", "25433")
    loaded = SerenaConfig.from_config_file()
    assert loaded.project_server_port == 25432
    reloaded = SerenaConfig.from_config_file()
    assert reloaded.project_server_port == 25432
    assert "project_server_port: 25432" in path.read_text()


def test_legacy_configuration_gets_default_port(tmp_path, monkeypatch) -> None:
    path = tmp_path / "serena_config.yml"
    path.write_text("projects: []\nauth_secret: test-secret\n")
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", lambda: str(path))
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)
    assert SerenaConfig.from_config_file().project_server_port == 24225


@pytest.mark.parametrize("value", ["true", "0", "65536", "1.5", "null", "'25432'"])
def test_invalid_stored_port_fails_before_migration(tmp_path, monkeypatch, value) -> None:
    path = tmp_path / "serena_config.yml"
    original = f"projects: []\nproject_server_port: {value}\n"
    path.write_text(original)
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", lambda: str(path))
    with pytest.raises(ValueError, match="project_server_port"):
        SerenaConfig.from_config_file()
    assert path.read_text() == original
