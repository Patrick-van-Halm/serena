"""Lightweight authenticated client for Serena's shared local MCP daemon."""

# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from serena.config.serena_config import SerenaConfig
from serena.constants import SerenaPorts
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION, shared_mcp_build_id
from serena.util.tool_call import ToolCall, ToolCallContext

log = logging.getLogger(__name__)


class IncompatibleSharedMCPDaemonError(ConnectionError):
    def __init__(
        self,
        message: str,
        *,
        pid: int | None = None,
        server_protocol_version: int | None = None,
        server_build_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pid = pid
        self.server_protocol_version = server_protocol_version
        self.server_build_id = server_build_id


@dataclass(frozen=True, slots=True)
class SharedMCPRuntimeInfo:
    project_root: str
    project_id: str
    project_name: str
    tool_names: list[str]
    instructions: str
    structured_tool_output: bool | None


class SharedMCPDaemonClient:
    """
    Client-side half of the shared MCP bridge protocol.

    This module deliberately has no dependency on project_server/Flask, keeping each
    per-conversation stdio bridge substantially lighter than the long-lived daemon.
    """

    def __init__(self, serena_config: SerenaConfig, host: str = "127.0.0.1", port: int = SerenaPorts.PROJECT_SERVER_PORT) -> None:
        self._base_url = f"http://{host}:{port}"
        self._tool_timeout = max(float(serena_config.tool_timeout), 1.0)
        self._timeout = self._tool_timeout + ToolCall.RESPONSE_GRACE_SECONDS
        self._headers = {"Authorization": f"Bearer {serena_config.auth_secret}"}

        try:
            response = requests.get(f"{self._base_url}/heartbeat", headers=self._headers, timeout=5)
            response.raise_for_status()
        except requests.ConnectionError as e:
            raise ConnectionError(f"Shared Serena daemon is not reachable at {self._base_url}") from e
        except requests.RequestException as e:
            raise ConnectionError(f"Shared Serena daemon health check failed: {e}") from e

        try:
            heartbeat = response.json()
        except ValueError as e:
            raise IncompatibleSharedMCPDaemonError(
                "Shared Serena daemon returned an invalid heartbeat payload"
            ) from e

        server_protocol = heartbeat.get("shared_mcp_protocol_version")
        server_build_id = heartbeat.get("shared_mcp_build_id")
        server_pid = heartbeat.get("pid")
        pid = server_pid if isinstance(server_pid, int) else None
        expected_build_id = shared_mcp_build_id()
        if server_protocol != SHARED_MCP_PROTOCOL_VERSION or server_build_id != expected_build_id:
            raise IncompatibleSharedMCPDaemonError(
                "Shared Serena daemon is stale/incompatible "
                f"(protocol={server_protocol!r}, build={server_build_id!r}; "
                f"expected protocol={SHARED_MCP_PROTOCOL_VERSION}, build={expected_build_id})",
                pid=pid,
                server_protocol_version=server_protocol if isinstance(server_protocol, int) else None,
                server_build_id=server_build_id if isinstance(server_build_id, str) else None,
            )

    def get_runtime_info(self, project_root: str, context: str, session_id: str | None = None) -> SharedMCPRuntimeInfo:
        response = requests.post(
            f"{self._base_url}/mcp/runtime-info",
            json={"project_root": project_root, "context": context, "session_id": session_id},
            headers=self._headers,
            timeout=self._timeout,
        )
        if not response.ok:
            raise ValueError(f"Shared MCP daemon error ({response.status_code}): {response.text[:2000]}")
        data = response.json()
        return SharedMCPRuntimeInfo(
            project_root=str(data["project_root"]),
            project_id=str(data["project_id"]),
            project_name=str(data["project_name"]),
            tool_names=list(data["tool_names"]),
            instructions=str(data["instructions"]),
            structured_tool_output=data.get("structured_tool_output"),
        )

    def heartbeat_bridge(self, project_root: str, context: str, session_id: str) -> None:
        response = requests.post(
            f"{self._base_url}/mcp/bridge-heartbeat",
            json={"project_root": project_root, "context": context, "session_id": session_id},
            headers=self._headers,
            timeout=5,
        )
        response.raise_for_status()

    def close_bridge(self, project_root: str, context: str, session_id: str) -> None:
        try:
            requests.post(
                f"{self._base_url}/mcp/bridge-close",
                json={"project_root": project_root, "context": context, "session_id": session_id},
                headers=self._headers,
                timeout=5,
            )
        except requests.RequestException:
            # Best-effort only; bridge leases expire server-side after crashes.
            pass

    def cancel_tool_call(self, session_id: str, request_id: str) -> None:
        """Deliver cancellation separately from the potentially blocked tool request."""
        response = requests.post(
            f"{self._base_url}/mcp/cancel-tool-call",
            json={"session_id": session_id, "request_id": request_id},
            headers=self._headers,
            timeout=(1.0, 1.0),
        )
        response.raise_for_status()

    def call_tool(
        self,
        project_root: str,
        context: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        # carry one deadline through worker admission, HTTP dispatch, server queueing, and execution
        call_context = ToolCallContext.current()
        call = call_context.call if call_context is not None else ToolCall(self._tool_timeout)
        call.check()
        deadline = time.time() + call.remaining
        call.mark_dispatched()
        try:
            response = requests.post(
                f"{self._base_url}/mcp/tool-call",
                json={
                    "project_root": project_root,
                    "context": context,
                    "session_id": session_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "request_id": call.request_id,
                    "deadline": deadline,
                },
                headers=self._headers,
                timeout=(5.0, call.remaining + ToolCall.RESPONSE_GRACE_SECONDS),
            )
        except requests.RequestException as e:
            # cancellation is best-effort; a lost response never proves an edit did not happen
            call.cancel()
            try:
                self.cancel_tool_call(session_id, call.request_id)
            except requests.RequestException:
                log.warning("Cancellation delivery failed for %s; outcome unknown", call.request_id, exc_info=True)
            raise ConnectionError(
                f"Shared MCP transport failed for tool call {call.request_id}; outcome=unknown. "
                "Verify changes before retrying; the operation may still be running."
            ) from e
        if not response.ok:
            raise ValueError(f"Shared MCP daemon error ({response.status_code}): {response.text[:2000]}")
        return response.text
