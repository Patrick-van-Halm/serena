# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from types import SimpleNamespace

import pytest

from serena.constants import SerenaPorts
from serena.project_server_endpoint import ProjectServerEndpoint


@pytest.fixture(autouse=True)
def clean_port_override(monkeypatch):
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT", raising=False)


def test_default_and_configured_ports() -> None:
    assert ProjectServerEndpoint.from_config(SimpleNamespace(project_server_port=SerenaPorts.PROJECT_SERVER_PORT)).port == 24225
    assert ProjectServerEndpoint.from_config(SimpleNamespace(project_server_port=25432)).port == 25432


def test_override_precedence_and_no_configuration_mutation(monkeypatch) -> None:
    config = SimpleNamespace(project_server_port=25432)
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", " 25433 ")
    assert ProjectServerEndpoint.from_config(config).port == 25433
    assert ProjectServerEndpoint.from_config(config, port=25434).port == 25434
    assert config.project_server_port == 25432
    monkeypatch.delenv("SERENA_PROJECT_SERVER_PORT")
    assert ProjectServerEndpoint.from_config(config).port == 25432


@pytest.mark.parametrize("port", [0, -1, 65536, True, False, None, 2.5, "25432"])
def test_invalid_global_port_is_rejected(port) -> None:
    with pytest.raises(ValueError, match="project_server_port.*integer between 1 and 65535"):
        ProjectServerEndpoint.from_config(SimpleNamespace(project_server_port=port))


@pytest.mark.parametrize("value", ["", " ", "0", "-1", "65536", "3.14", "true", "0x5ea1", "２５４３２"])
def test_invalid_environment_override_is_not_silently_ignored(monkeypatch, value) -> None:
    monkeypatch.setenv("SERENA_PROJECT_SERVER_PORT", value)
    with pytest.raises(ValueError, match="SERENA_PROJECT_SERVER_PORT"):
        ProjectServerEndpoint.from_config(SimpleNamespace(project_server_port=25432))


@pytest.mark.parametrize("port", [1, 65535])
def test_valid_boundaries(port) -> None:
    assert ProjectServerEndpoint.from_config(SimpleNamespace(project_server_port=port)).port == port


def test_endpoint_identity_and_ipv6_url(tmp_path: Path) -> None:
    canonical = ProjectServerEndpoint("127.0.0.1", 25432)
    alias = ProjectServerEndpoint("LOCALHOST.", 25432)
    assert alias.base_url == "http://127.0.0.1:25432"
    assert alias.startup_lock_path(tmp_path) == canonical.startup_lock_path(tmp_path)
    assert ProjectServerEndpoint("127.0.0.1", 25433).startup_lock_path(tmp_path) != canonical.startup_lock_path(tmp_path)
    assert ProjectServerEndpoint("127.0.0.2", 25432).startup_lock_path(tmp_path) != canonical.startup_lock_path(tmp_path)
    assert ProjectServerEndpoint("[::1]", 25432).base_url == "http://[::1]:25432"
    assert canonical.matches_listener("0.0.0.0", 25432)
    assert not canonical.matches_listener("127.0.0.1", 25433)
    assert not canonical.matches_listener("127.0.0.2", 25432)
