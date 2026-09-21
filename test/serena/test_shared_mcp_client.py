from unittest.mock import MagicMock

import pytest

from serena.config.serena_config import SerenaConfig
from serena.shared_mcp_client import IncompatibleSharedMCPDaemonError, SharedMCPDaemonClient
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION, shared_mcp_build_id


def test_shared_client_rejects_legacy_daemon_without_protocol(monkeypatch) -> None:
    response = MagicMock()
    response.json.return_value = {"status": "alive"}
    monkeypatch.setattr("serena.shared_mcp_client.requests.get", lambda *args, **kwargs: response)

    with pytest.raises(IncompatibleSharedMCPDaemonError):
        SharedMCPDaemonClient(SerenaConfig())


def test_shared_client_accepts_matching_daemon(monkeypatch) -> None:
    response = MagicMock()
    response.json.return_value = {
        "status": "alive",
        "shared_mcp_protocol_version": SHARED_MCP_PROTOCOL_VERSION,
        "shared_mcp_build_id": shared_mcp_build_id(),
        "pid": 123,
    }
    monkeypatch.setattr("serena.shared_mcp_client.requests.get", lambda *args, **kwargs: response)

    client = SharedMCPDaemonClient(SerenaConfig())
    assert client is not None
