# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from serena.config.serena_config import SerenaConfig
from serena.project_server import ProjectServerClient
from serena.project_server_endpoint import (
    ProjectServerAuthenticationError,
    ProjectServerEndpoint,
    ProjectServerTimeoutError,
    ProjectServerUnavailableError,
)
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION


@pytest.fixture
def endpoint_server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def do_GET(self):
            self.send_response(200 if self.headers.get("Authorization") == "Bearer test-secret" else 401)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "alive",
                        "pid": 1234,
                        "shared_mcp_protocol_version": SHARED_MCP_PROTOCOL_VERSION,
                        "shared_mcp_build_id": "test-build",
                    }
                ).encode()
            )

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = b"read-result"
            self.send_response(200 if self.headers.get("Authorization") == "Bearer test-secret" else 401)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

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
def test_listener_serves_resolved_port_without_reverse_dns(tmp_path: Path, override: str) -> None:
    # use a private configuration and an OS-assigned port for the real listener
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        expected = reservation.getsockname()[1]
    config_port = expected if override == "config" else 1
    (tmp_path / "serena_config.yml").write_text(
        f"projects: []\nproject_server_port: {config_port}\nauth_secret: test-secret\n", encoding="utf-8"
    )
    environment = dict(os.environ, SERENA_HOME=str(tmp_path), PYTHONUNBUFFERED="1")
    environment.pop("SERENA_PROJECT_SERVER_PORT", None)
    environment.pop("FLASK_RUN_FROM_CLI", None)
    if override in {"environment", "explicit"}:
        environment["SERENA_PROJECT_SERVER_PORT"] = str(expected if override == "environment" else 2)
    explicit = expected if override == "explicit" else None
    code = f"""
from unittest.mock import patch
from serena.project_server import ProjectServer

with patch("socket.getfqdn", side_effect=RuntimeError("reverse DNS unavailable")):
    ProjectServer(port={explicit!r}).run()
"""
    endpoint = ProjectServerEndpoint(port=expected)
    with (tmp_path / "listener.log").open("w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        try:
            # wait for authenticated HTTP readiness, retaining startup failures as evidence
            deadline = time.monotonic() + 10
            while True:
                try:
                    assert endpoint.heartbeat("test-secret", timeout=0.2)["status"] == "alive"
                    break
                except (ProjectServerUnavailableError, ProjectServerTimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        output.seek(0)
                        pytest.fail(f"Project listener did not start: {output.read()}")
                    time.sleep(0.02)
            with pytest.raises(ProjectServerAuthenticationError):
                endpoint.heartbeat("wrong-secret", timeout=1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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
