# SPDX-License-Identifier: GPL-3.0-or-later

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from serena.cli import top_level
from serena.config.serena_config import SerenaConfig
from serena.shared_mcp_daemon import SharedMCPDaemon


@pytest.fixture
def restart_environment(monkeypatch):
    config = SimpleNamespace(project_server_port=25432, config_file_path="/tmp/serena/serena_config.yml")
    monkeypatch.setattr(SerenaConfig, "from_config_file", lambda: config)
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)
    daemon = Mock(spec=SharedMCPDaemon)
    daemon.restart.return_value = SimpleNamespace(pid=4321)
    factory = Mock(return_value=daemon)
    monkeypatch.setattr("serena.shared_mcp_daemon.SharedMCPDaemon", factory)
    return factory, daemon


def test_restart_help_is_registered_without_starting_daemon(restart_environment):
    factory, _ = restart_environment
    result = CliRunner().invoke(top_level, ["restart", "--help"])
    assert result.exit_code == 0
    assert "--startup-timeout" in result.output
    assert "--port" in result.output
    factory.assert_not_called()


@pytest.mark.parametrize("selection", ["config", "environment", "explicit"])
def test_restart_reports_resolved_endpoint_and_ready_pid(restart_environment, monkeypatch, selection):
    factory, daemon = restart_environment
    args = ["restart", "--yes", "--startup-timeout", "12"]
    expected = 25432
    if selection != "config":
        monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", "25433")
        expected = 25433
    if selection == "explicit":
        args += ["--port", "25434"]
        expected = 25434
    result = CliRunner().invoke(top_level, args)
    assert result.exit_code == 0, result.output
    assert f"127.0.0.1:{expected}" in result.output and "pid=4321" in result.output
    assert factory.call_args.args[1].port == expected
    daemon.restart.assert_called_once_with(startup_timeout=12.0)


def test_declining_restart_keeps_daemon_running(restart_environment):
    _, daemon = restart_environment
    result = CliRunner().invoke(top_level, ["restart"], input="n\n")
    assert result.exit_code != 0
    daemon.restart.assert_not_called()


@pytest.mark.parametrize("args", [["--port", "0"], ["--startup-timeout", "nan"], ["--startup-timeout", "inf"], ["--host", "192.0.2.1"]])
def test_invalid_restart_options_do_not_signal_daemon(restart_environment, args):
    _, daemon = restart_environment
    result = CliRunner().invoke(top_level, ["restart", "--yes", *args])
    assert result.exit_code != 0
    daemon.restart.assert_not_called()


def test_restart_failure_is_nonzero_and_preserves_startup_diagnostics(restart_environment):
    _, daemon = restart_environment
    daemon.restart.side_effect = ConnectionError("Actual startup error; startup log: /tmp/startup.log")
    result = CliRunner().invoke(top_level, ["restart", "--yes"])
    assert result.exit_code == 1
    assert "Actual startup error" in result.output and "/tmp/startup.log" in result.output
    assert "daemon ready" not in result.output
