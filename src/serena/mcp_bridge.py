"""Lightweight stdio MCP bridge to Serena's shared project daemon."""

# SPDX-License-Identifier: GPL-3.0-or-later

import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import anyio
import docstring_parser
from anyio.to_thread import run_sync
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool as FastMCPTool
from mcp_types import ToolAnnotations
from sensai.util import logging

from serena import __version__
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import SerenaConfig, project_root_key, project_roots_equal
from serena.project_server_endpoint import ProjectServerEndpointError
from serena.shared_mcp_client import SharedMCPDaemonClient
from serena.shared_mcp_daemon import SharedMCPDaemon
from serena.shared_mcp_recovery import RecoveryScope
from serena.tools import Tool, ToolCallError, ToolRegistry
from serena.util.tool_call import AsyncToolCallRunner, ToolCall, ToolCallCancellationHandler
from serena.util.tool_result import ToolResult

log = logging.getLogger(__name__)


class _BridgeAgent:
    """Minimal object required by Tool metadata generation; it owns no project services."""

    def __init__(self, context: SerenaAgentContext) -> None:
        self._context = context

    def get_context(self) -> SerenaAgentContext:
        return self._context


def _sanitize_for_openai_tools(schema: dict) -> dict:
    """Apply the same OpenAI/Codex schema compatibility rules as the full MCP server."""
    s = deepcopy(schema)

    def walk(node):
        if not isinstance(node, dict):
            return node

        t = node.get("type")
        if isinstance(t, str):
            if t == "integer":
                node["type"] = "number"
                node.setdefault("multipleOf", 1)
        elif isinstance(t, list):
            t2 = [x if x != "integer" else "number" for x in t if x != "null"]
            if not t2:
                t2 = ["object"]
            node["type"] = t2[0] if len(t2) == 1 else t2
            if "integer" in t or "number" in t2:
                node.setdefault("multipleOf", 1)

        if "enum" in node and isinstance(node["enum"], list):
            values = node["enum"]
            if values and all(isinstance(v, int) for v in values):
                node.setdefault("type", "number")
                node.setdefault("multipleOf", 1)

        for key in ("oneOf", "anyOf"):
            if key in node and isinstance(node[key], list):
                if len(node[key]) == 2:
                    types = [sub.get("type") for sub in node[key]]
                    if "null" in types:
                        non_null_type = next(value for value in types if value != "null")
                        if isinstance(non_null_type, str):
                            node["type"] = non_null_type
                            node.pop(key, None)
                            continue
                simplified = [walk(sub) for sub in node[key]]
                try:
                    import json

                    canonical = [json.dumps(value, sort_keys=True) for value in simplified]
                    if len(set(canonical)) == 1:
                        only = simplified[0]
                        node.pop(key, None)
                        for child_key, child_value in only.items():
                            node.setdefault(child_key, child_value)
                    else:
                        node[key] = simplified
                except Exception:
                    node[key] = simplified

        for child_key in ("properties", "patternProperties", "definitions", "$defs"):
            if child_key in node and isinstance(node[child_key], dict):
                for key, value in list(node[child_key].items()):
                    node[child_key][key] = walk(value)

        if "items" in node:
            node["items"] = walk(node["items"])
        if "allOf" in node and isinstance(node["allOf"], list):
            node["allOf"] = [walk(value) for value in node["allOf"]]
        for key in ("if", "then", "else"):
            if key in node:
                node[key] = walk(node[key])
        return node

    return walk(s)


class BridgeFastMCPTool(FastMCPTool):
    """MCP tool whose schema comes from a Serena Tool and whose execution is remote."""

    def __init__(
        self,
        tool: Tool,
        openai_tool_compatible: bool,
        structured_output: bool | None,
        *,
        tool_timeout: float = 240.0,
        cancellation_handler: ToolCallCancellationHandler | None = None,
    ):
        func_name = tool.get_name()
        func_doc = tool.get_apply_docstring() or ""
        func_arg_metadata = tool.get_apply_fn_metadata(structured_output=structured_output)
        parameters = func_arg_metadata.arg_model.model_json_schema()
        if openai_tool_compatible:
            parameters = _sanitize_for_openai_tools(parameters)

        docstring = docstring_parser.parse(func_doc)
        overridden_description = tool.agent.get_context().tool_description_overrides.get(func_name)
        if overridden_description is not None:
            func_doc = overridden_description
        elif docstring.description:
            func_doc = docstring.description
        else:
            func_doc = ""
        func_doc = func_doc.strip().strip(".")
        if func_doc:
            func_doc += "."
        if docstring.returns and (return_description := docstring.returns.description):
            prefix = " " if func_doc else ""
            func_doc = f"{func_doc}{prefix}Returns {return_description.strip().strip('.')}."

        documented_params = {param.arg_name: param for param in docstring.params}
        properties: dict[str, dict[str, Any]] = parameters["properties"]
        for parameter, property_schema in properties.items():
            param_doc = documented_params.get(parameter)
            if param_doc is not None and param_doc.description:
                description = param_doc.description.strip().strip(".") + "."
                property_schema["description"] = description[0].upper() + description[1:]

        async def execute_fn(**kwargs) -> ToolResult:
            try:
                return await AsyncToolCallRunner.run(
                    lambda: tool.apply_ex(log_call=True, catch_exceptions=False, **kwargs),
                    timeout=tool_timeout,
                    cancellation_handler=cancellation_handler,
                    response_grace=ToolCall.RESPONSE_GRACE_SECONDS,
                )
            except ToolCallError as e:
                raise ToolError(e.get_error_message()) from e

        title = " ".join(word.capitalize() for word in func_name.split("_"))
        can_edit = tool.can_edit()
        annotations = ToolAnnotations(title=title, read_only_hint=not can_edit, destructive_hint=can_edit)

        super().__init__(
            fn=execute_fn,
            name=func_name,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=True,
            context_kwarg="mcp_ctx",
            annotations=annotations,
            title=title,
        )
        self._param_aliases = tool.get_param_aliases()

    async def run(self, arguments: dict[str, Any], context: Context | None = None, convert_result: bool = False) -> Any:
        for param_alias, param_name in self._param_aliases.items():
            if param_alias in arguments and param_name not in arguments:
                arguments[param_name] = arguments.pop(param_alias)
        return await super().run(arguments, context or Context(), convert_result)


def _validate_runtime_project_root(requested_root: str, runtime_root: str) -> str:
    requested_path = Path(requested_root).expanduser().resolve()
    runtime_path = Path(runtime_root).expanduser().resolve()
    if not project_roots_equal(requested_path, runtime_path):
        raise ConnectionError(f"Shared Serena daemon routed bridge for {str(requested_path)!r} to unexpected project {str(runtime_path)!r}")
    return str(runtime_path)


def ensure_shared_daemon(serena_config: SerenaConfig, startup_timeout: float = 30.0) -> SharedMCPDaemonClient:
    """Connect to the configured endpoint, starting only an absent or verified stale daemon."""
    return SharedMCPDaemon(serena_config).ensure_running(startup_timeout)


class SerenaMCPBridge:
    """Per-conversation stdio MCP endpoint backed by one shared Serena daemon."""

    HEARTBEAT_INTERVAL_SECONDS = 30.0

    def __init__(self, project_root: str, context_name: str = "codex") -> None:
        self.project_root = str(Path(project_root).expanduser().resolve())
        self.context = SerenaAgentContext.load(context_name)
        self.session_id = secrets.token_hex(8)

        config = SerenaConfig.from_config_file()
        self._daemon = SharedMCPDaemon(config)
        self._recovery_lock = threading.Lock()
        self.client = self._daemon.ensure_running()
        runtime_info = self.client.get_runtime_info(self.project_root, self.context.name, session_id=self.session_id)
        try:
            confirmed_root = _validate_runtime_project_root(self.project_root, runtime_info.project_root)
            if runtime_info.project_id != project_root_key(confirmed_root):
                raise ConnectionError(
                    f"Shared Serena daemon returned inconsistent project identity {runtime_info.project_id!r} for root {confirmed_root!r}"
                )
        except Exception:
            self.client.close_bridge(
                project_root=runtime_info.project_root,
                context=self.context.name,
                session_id=self.session_id,
            )
            raise

        # From here on, use only the daemon-confirmed canonical root. project_name is
        # display metadata and may be identical for several git worktrees.
        self.project_root = confirmed_root
        self.project_id = runtime_info.project_id
        self.project_name = runtime_info.project_name

        self._heartbeat_stop = threading.Event()
        self._shutdown_started = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="SerenaMCPBridgeHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        binding_instructions = (
            f"\n\nProject binding for this MCP connection: display name={self.project_name!r}, "
            f"canonical root={self.project_root!r}. The canonical root is authoritative. "
            "Git worktrees may share the same project name; never use the name alone to infer "
            "whether this project is active."
        )
        self.server = MCPServer(
            name="Serena",
            website_url="https://oraios.github.io/serena",
            instructions=runtime_info.instructions + binding_instructions,
            version=__version__,
        )

        registry = ToolRegistry()
        bridge_agent = _BridgeAgent(self.context)
        openai_compatible = self.context.name in {"chatgpt", "codex", "oaicompat-agent"}

        for tool_name in runtime_info.tool_names:
            tool_class = registry.get_tool_class_by_name(tool_name)
            proxy_class = self._proxy_tool_class(tool_class)
            proxy_tool = proxy_class(cast(Any, bridge_agent))
            mcp_tool = BridgeFastMCPTool(
                proxy_tool,
                openai_tool_compatible=openai_compatible,
                structured_output=runtime_info.structured_tool_output,
                tool_timeout=float(config.tool_timeout),
                cancellation_handler=self,
            )
            self.server._tool_manager._tools[tool_name] = mcp_tool

        # register outside the proxy registry: this control tool must never join the stalled executor
        self.server.add_tool(
            self.restart_daemon,
            name="restart_daemon",
            description=(
                "Last-resort recovery only when Serena tools are confirmed blocked, not merely slow. "
                "Do not use after a single timeout, during normal work, or to bypass an error. "
                "The daemon must twice verify the same retrieval-only worker at least 30 seconds past its deadline "
                "with no queued or unrelated work. Edits, shell/REPL work, and unverifiable failures are refused. "
                "A successful recovery discards in-memory sessions. No force option is available. "
                "If refused, report the blocker; do not fall back to CLI restart, shell commands, or killing processes."
            ),
            annotations=ToolAnnotations(
                title="Recover Blocked Serena Daemon",
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=False,
        )

        log.info(
            "Shared MCP bridge ready for %s via daemon (%d tools, session=%s)",
            runtime_info.project_root,
            len(runtime_info.tool_names),
            self.session_id,
        )

    def _proxy_tool_class(self, tool_class: type[Tool]) -> type[Tool]:
        bridge = self

        def apply_ex(
            tool_self: Tool,
            log_call: bool = True,
            catch_exceptions: bool = True,
            mcp_ctx: Context | None = None,
            **kwargs,
        ) -> ToolResult:
            try:
                return bridge.client.call_tool(
                    project_root=bridge.project_root,
                    context=bridge.context.name,
                    session_id=bridge.session_id,
                    tool_name=tool_self.get_name(),
                    arguments=kwargs,
                )
            except Exception as e:
                error = ToolCallError(str(e))
                if catch_exceptions:
                    return error.get_error_message()
                raise error from e

        return type(
            tool_class.__name__,
            (tool_class,),
            {
                "__module__": tool_class.__module__,
                "apply_ex": apply_ex,
            },
        )

    async def restart_daemon(self) -> str:
        """Recover a proven blocked retrieval through a separate, bounded control worker."""
        if not self._recovery_lock.acquire(blocking=False):
            raise ToolError("A Serena recovery is already in progress.")
        try:

            def recover() -> str:
                client = self._daemon.recover_blocked(RecoveryScope(self.project_root, self.context.name, self.session_id))
                self.client = client
                return (
                    f"Shared Serena daemon recovered (pid={client.pid}). In-memory session state was reset. "
                    "The bridge remains connected; project services will reload on the next call. "
                    "Do not replay earlier modifying operations without checking their results."
                )

            # keep the normal MCP worker pool out of this path; finish an admitted restart even on host cancellation
            with anyio.CancelScope(shield=True):
                return await run_sync(recover, limiter=anyio.CapacityLimiter(1))
        except Exception as error:
            raise ToolError(f"Serena recovery failed or was refused: {error}") from error
        finally:
            self._recovery_lock.release()

    def cancel_tool_call(self, call: ToolCall) -> None:
        """Forward host cancellation without retrying the original operation."""
        self.client.cancel_tool_call(self.session_id, call.request_id)

    def _unregister_bridge(self) -> None:
        try:
            self.client.close_bridge(
                project_root=self.project_root,
                context=self.context.name,
                session_id=self.session_id,
            )
        except Exception as e:
            log.debug("Failed to unregister shared MCP bridge: %s", e)

    def _heartbeat_loop(self) -> None:
        # Do not self-terminate on inactivity. Codex may keep an MCP stdio transport
        # open for a conversation but does not reliably respawn a server that exits
        # while that conversation is still alive. Heavy resources idle behind this
        # lightweight bridge: project LSPs are stopped independently by the daemon.
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL_SECONDS):
            try:
                self.client.heartbeat_bridge(
                    project_root=self.project_root,
                    context=self.context.name,
                    session_id=self.session_id,
                )
            except Exception as e:
                # Re-establish the daemon/runtime proactively, but never retry an in-flight
                # tool call: retrying an edit after an ambiguous network failure could apply
                # a destructive operation twice.
                log.debug("Shared MCP bridge heartbeat failed; reconnecting: %s", e)
                try:
                    client = self._daemon.ensure_running()
                    client.get_runtime_info(
                        self.project_root,
                        self.context.name,
                        session_id=self.session_id,
                    )
                    self.client = client
                except ProjectServerEndpointError as reconnect_error:
                    log.error("Shared MCP daemon endpoint/configuration error: %s", reconnect_error)
                except Exception as reconnect_error:
                    log.debug("Shared MCP bridge reconnect failed: %s", reconnect_error)

    def run(self) -> None:
        try:
            self.server.run(transport="stdio")
        finally:
            self._heartbeat_stop.set()
            if not self._shutdown_started.is_set():
                self._shutdown_started.set()
                self._unregister_bridge()
