"""Lightweight stdio MCP bridge to Serena's shared project daemon."""

# SPDX-License-Identifier: GPL-3.0-or-later

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import docstring_parser
from filelock import FileLock
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import Context, FastMCP, Settings
from mcp.server.fastmcp.tools.base import Tool as FastMCPTool
from mcp.types import ToolAnnotations
from pydantic_settings import SettingsConfigDict
from sensai.util import logging

from serena import __version__
from serena.config.context_mode import SerenaAgentContext
from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.project_server import ProjectServerClient
from serena.tools import Tool, ToolCallError, ToolRegistry

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

    def __init__(self, tool: Tool, openai_tool_compatible: bool, structured_output: bool | None):
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

        def execute_fn(**kwargs) -> str:
            try:
                return tool.apply_ex(log_call=True, catch_exceptions=False, **kwargs)
            except ToolCallError as e:
                raise ToolError(e.get_error_message()) from e

        title = " ".join(word.capitalize() for word in func_name.split("_"))
        can_edit = tool.can_edit()
        annotations = ToolAnnotations(title=title, readOnlyHint=not can_edit, destructiveHint=can_edit)

        super().__init__(
            fn=execute_fn,
            name=func_name,
            description=func_doc,
            parameters=parameters,
            fn_metadata=func_arg_metadata,
            is_async=False,
            context_kwarg="mcp_ctx",
            annotations=annotations,
            title=title,
        )
        self._param_aliases = tool.get_param_aliases()

    async def run(self, arguments: dict[str, Any], context: Context | None = None, convert_result: bool = False) -> Any:
        for param_alias, param_name in self._param_aliases.items():
            if param_alias in arguments and param_name not in arguments:
                arguments[param_name] = arguments.pop(param_alias)
        return await super().run(arguments, context, convert_result)


def _daemon_command() -> list[str]:
    executable = shutil.which("serena") or shutil.which("serena-agent")
    if executable is not None:
        return [executable, "start-project-server", "--log-level", "WARNING"]
    # Fallback for editable/dev installs where the console script is not on PATH.
    return [
        sys.executable,
        "-c",
        "from serena.cli import top_level; top_level()",
        "start-project-server",
        "--log-level",
        "WARNING",
    ]


def _spawn_shared_daemon() -> subprocess.Popen:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": os.environ.copy(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(_daemon_command(), **kwargs)


def ensure_shared_daemon(serena_config: SerenaConfig, startup_timeout: float = 30.0) -> ProjectServerClient:
    """Return the singleton daemon client, starting the daemon if necessary."""
    try:
        return ProjectServerClient(serena_config)
    except ConnectionError:
        pass

    home = Path(SerenaPaths().serena_user_home_dir)
    home.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(home / "shared-mcp-daemon.lock"), timeout=startup_timeout)

    with lock:
        # Another bridge may have completed startup while this process waited.
        try:
            return ProjectServerClient(serena_config)
        except ConnectionError:
            pass

        process = _spawn_shared_daemon()
        deadline = time.monotonic() + startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ConnectionError(f"Shared Serena daemon exited during startup with code {process.returncode}")
            try:
                return ProjectServerClient(serena_config)
            except ConnectionError as e:
                last_error = e
                time.sleep(0.1)

        raise ConnectionError(f"Shared Serena daemon did not become ready within {startup_timeout:.1f}s: {last_error}")


class SerenaMCPBridge:
    """Per-conversation stdio MCP endpoint backed by one shared Serena daemon."""

    def __init__(self, project_root: str, context_name: str = "codex") -> None:
        self.project_root = str(Path(project_root).expanduser().resolve())
        self.context = SerenaAgentContext.load(context_name)
        self.session_id = secrets.token_hex(8)

        config = SerenaConfig.from_config_file()
        self.client = ensure_shared_daemon(config)
        runtime_info = self.client.get_mcp_runtime_info(self.project_root, self.context.name, session_id=self.session_id)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="SerenaMCPBridgeHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        Settings.model_config = SettingsConfigDict(env_prefix="FASTMCP_")
        self.server = FastMCP(
            name="Serena",
            website_url="https://oraios.github.io/serena",
            instructions=runtime_info.instructions,
        )
        self.server._mcp_server.version = __version__

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
            )
            self.server._tool_manager._tools[tool_name] = mcp_tool

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
        ) -> str:
            try:
                return bridge.client.call_mcp_tool(
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

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(60.0):
            try:
                self.client.heartbeat_mcp_bridge(
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
                    client = ensure_shared_daemon(SerenaConfig.from_config_file())
                    client.get_mcp_runtime_info(
                        self.project_root,
                        self.context.name,
                        session_id=self.session_id,
                    )
                    self.client = client
                except Exception as reconnect_error:
                    log.debug("Shared MCP bridge reconnect failed: %s", reconnect_error)

    def run(self) -> None:
        try:
            self.server.run(transport="stdio")
        finally:
            self._heartbeat_stop.set()
            self.client.close_mcp_bridge(
                project_root=self.project_root,
                context=self.context.name,
                session_id=self.session_id,
            )
