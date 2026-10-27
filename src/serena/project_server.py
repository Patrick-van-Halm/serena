# SPDX-License-Identifier: GPL-3.0-or-later

import json
import logging
import os
import pickle
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import requests as requests_lib
from flask import Flask, Response, abort, request
from pydantic import BaseModel, Field
from sensai.util.logging import LogTime

from serena.config.serena_config import LanguageBackend, SerenaConfig, project_root_key, project_roots_equal
from serena.constants import SerenaPorts
from serena.project_server_endpoint import ProjectServerEndpoint
from serena.shared_mcp_calls import DuplicateToolCallError, SharedMCPCalls
from serena.shared_mcp_recovery import DaemonRecovery, DaemonRecoveryRefusedError, RecoveryEvidence, RecoveryScope
from serena.shared_mcp_protocol import SHARED_MCP_PROTOCOL_VERSION, shared_mcp_build_id
from serena.util.tool_call import ToolCall, ToolCallBusyError, ToolCallContext, ToolCallInterrupted
from serena.util.tool_result import ToolResult, ToolResultCodec

if TYPE_CHECKING:
    from serena.agent import SerenaAgent
    from serena.project import Project

log = logging.getLogger(__name__)

# disable Werkzeug's logging to avoid cluttering the output
logging.getLogger("werkzeug").setLevel(logging.WARNING)


class QueryProjectRequest(BaseModel):
    """
    Request model for the /query_project endpoint, matching the interface of
    :class:`~serena.tools.query_project_tools.QueryProjectTool`.
    """

    project_name: str
    tool_name: str
    tool_params_json: str


class MCPRuntimeInfoRequest(BaseModel):
    """Request the shared MCP runtime metadata for a project/context pair."""

    project_root: str
    context: str = "codex"
    session_id: str | None = None


class MCPRuntimeInfoResponse(BaseModel):
    project_root: str
    project_id: str
    project_name: str
    tool_names: list[str]
    instructions: str
    structured_tool_output: bool | None = None


class MCPBridgeSessionRequest(BaseModel):
    project_root: str
    context: str = "codex"
    session_id: str


class MCPBridgeCloseRequest(MCPBridgeSessionRequest):
    pass


class MCPRecoveryRequest(MCPBridgeSessionRequest):
    action: Literal["observe", "claim", "release"]
    request_id: str = Field(default="", max_length=128)
    token: str = Field(default="", max_length=128)


class MCPToolCallRequest(BaseModel):
    """Execute one MCP tool against a shared project runtime."""

    project_root: str
    context: str = "codex"
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    request_id: str = Field(default_factory=lambda: secrets.token_hex(16), min_length=1, max_length=128)
    deadline: float | None = Field(default=None, allow_inf_nan=False)


class MCPToolCancelRequest(BaseModel):
    session_id: str
    request_id: str = Field(min_length=1, max_length=128)


@dataclass
class MCPProjectRuntime:
    """Long-lived project-scoped agent reused by multiple MCP bridge processes."""

    agent: "SerenaAgent"
    project_root: str
    context: str
    last_access: float
    active_calls: int = 0
    current_call: ToolCall | None = field(default=None, repr=False)
    current_tool_name: str | None = None
    current_tool_read_only: bool = False
    current_session_id: str | None = None
    bridge_sessions: dict[str, float] = field(default_factory=dict)
    binding_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class CallFacadeMethodRequest(BaseModel):
    """
    Request model for the /call_facade_method endpoint: the execution of a REPL facade method
    in the context of a project.
    """

    project_name: str
    facade_name: str
    method_name: str
    args: list[Any]
    kwargs: dict[str, Any]


class ProjectServer:
    """
    A lightweight Flask server that exposes a SerenaAgent's project querying
    capabilities via HTTP, using the LSP language server backend for symbolic retrieval.

    Projects are loaded on demand when a query is made for them, and cached in memory for subsequent queries.

    The server instantiates a :class:`SerenaAgent` with default options and
    provides a ``/query_project`` endpoint whose interface matches
    :class:`~serena.tools.query_project_tools.QueryProjectTool`.
    """

    MCP_RUNTIME_MAX_CALLS = 64
    MCP_RUNTIME_IDLE_SECONDS = 30 * 60
    MCP_RUNTIME_MAX_PROJECTS = 4
    MCP_RUNTIME_EVICTION_INTERVAL_SECONDS = 60
    MCP_BRIDGE_LEASE_SECONDS = 3 * 60

    PORT = SerenaPorts.PROJECT_SERVER_PORT

    def __init__(self, host: str = "127.0.0.1", port: int | None = None) -> None:
        """
        :param host: the host address to listen on.
        :param port: explicit port override; otherwise resolve the environment and global configuration
        """
        serena_config = SerenaConfig.from_config_file().with_headless_mode_overrides()
        endpoint = ProjectServerEndpoint.from_config(serena_config, host, port)
        log.info("Project server endpoint: %s", endpoint.base_url)
        serena_config.language_backend = LanguageBackend.LSP

        self._serena_config = serena_config
        self._agent: "SerenaAgent | None" = None
        self._loaded_projects_by_root: dict[str, "Project"] = {}
        self._project_load_locks_by_root: dict[str, threading.Lock] = {}
        self._active_project_lock = threading.Lock()
        self._loaded_projects_lock = threading.Lock()

        # Shared MCP runtimes are independent of the legacy read-only query route above.
        # Each canonical project/context pair owns exactly one SerenaAgent (and therefore
        # one LSP manager/cache set), reused by every bridge/chat targeting that project.
        self._mcp_runtimes: dict[tuple[str, str], MCPProjectRuntime] = {}
        self._mcp_runtime_load_locks: dict[tuple[str, str], threading.Lock] = {}
        self._mcp_runtimes_lock = threading.Lock()
        self._mcp_runtime_stop_event = threading.Event()
        self._mcp_calls = SharedMCPCalls(float(serena_config.tool_timeout))

        self._port = endpoint.port
        self._host = endpoint.host

        # create the Flask application, limiting trusted hosts for the case where the server is running on localhost
        self._app = Flask(__name__)
        local_hosts = ["localhost", "127.0.0.1", "::1", "[::1]"]
        if self._host in local_hosts:
            self._app.config["TRUSTED_HOSTS"] = local_hosts

        self._setup_routes()
        threading.Thread(target=self._mcp_runtime_eviction_loop, name="SerenaMCPRuntimeEviction", daemon=True).start()

    def get_serena_config(self) -> SerenaConfig:
        # Tests and legacy callers may inject an agent directly; prefer its config when present.
        if self._agent is not None:
            return self._agent.serena_config
        return self._serena_config

    def _get_legacy_agent(self) -> "SerenaAgent":
        if self._agent is None:
            from serena.agent import SerenaAgent

            self._agent = SerenaAgent(serena_config=self._serena_config)
        return self._agent

    def get_auth_secret(self) -> str:
        """Returns the authentication secret used by the server."""
        return self.get_serena_config().auth_secret

    def _setup_routes(self) -> None:
        self._mcp_recovery = DaemonRecovery(self)

        def guarded(handler: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(handler)
            def invoke(*args: Any, **kwargs: Any) -> Any:
                try:
                    with self._mcp_recovery.admit():
                        return handler(*args, **kwargs)
                except DaemonRecoveryRefusedError as error:
                    return Response(str(error), status=503, mimetype="text/plain")

            return invoke

        @self._app.before_request
        def authenticate() -> None:
            # authenticate every request before parsing input or accessing projects
            secret = self.get_auth_secret()
            provided = request.headers.get("Authorization", "")
            if not secret or not secrets.compare_digest(provided.encode("utf-8"), f"Bearer {secret}".encode()):
                abort(401)

        @self._app.route("/heartbeat", methods=["GET"])
        def heartbeat() -> dict[str, str | int]:
            return {
                "status": "alive",
                "shared_mcp_protocol_version": SHARED_MCP_PROTOCOL_VERSION,
                "shared_mcp_build_id": shared_mcp_build_id(),
                "pid": os.getpid(),
            }

        @self._app.route("/query_project", methods=["POST"])
        @guarded
        def query_project() -> Response:
            query_request = QueryProjectRequest.model_validate(request.get_json())
            body, content_type = ToolResultCodec.encode(self._query_project(query_request))
            return Response(body, content_type=content_type)

        @self._app.route("/mcp/runtime-info", methods=["POST"])
        @guarded
        def mcp_runtime_info() -> Response:
            req = MCPRuntimeInfoRequest.model_validate(request.get_json())
            try:
                info = self._get_mcp_runtime_info(req)
            except Exception as e:
                log.warning("Shared MCP runtime setup failed: %s", e)
                return Response(f"{type(e).__name__}: {e}", status=400, mimetype="text/plain")
            return Response(info.model_dump_json(), mimetype="application/json")

        @self._app.route("/mcp/bridge-heartbeat", methods=["POST"])
        @guarded
        def mcp_bridge_heartbeat() -> Response:
            req = MCPBridgeSessionRequest.model_validate(request.get_json())
            self._touch_mcp_bridge(req)
            return Response("ok", mimetype="text/plain")

        @self._app.route("/mcp/bridge-close", methods=["POST"])
        @guarded
        def mcp_bridge_close() -> Response:
            req = MCPBridgeCloseRequest.model_validate(request.get_json())
            self._close_mcp_bridge(req)
            return Response("ok", mimetype="text/plain")

        @self._app.route("/mcp/recovery", methods=["POST"])
        def mcp_recovery() -> Response:
            # keep recovery outside tool dispatch, while retaining the normal authentication gate
            try:
                req = MCPRecoveryRequest.model_validate(request.get_json())
                scope = RecoveryScope(project_root_key(req.project_root), req.context, req.session_id)
                payload: dict[str, Any] = {"pid": os.getpid()}
                if req.action == "observe":
                    evidence = self._mcp_recovery.observe(scope)
                    payload.update(
                        state="observed",
                        request_id=evidence.request_id,
                        retry_after_seconds=DaemonRecovery.OBSERVATION_SECONDS,
                    )
                elif req.action == "claim":
                    token = self._mcp_recovery.claim(scope, req.request_id)
                    payload.update(state="claimed", request_id=req.request_id, token=token)
                else:
                    self._mcp_recovery.release(scope, req.token)
                    payload.update(state="released")
                return Response(json.dumps(payload), mimetype="application/json")
            except DaemonRecoveryRefusedError as error:
                return Response(f"Recovery refused: {error}", status=409, mimetype="text/plain")
            except ValueError as error:
                return Response(str(error), status=400, mimetype="text/plain")

        @self._app.route("/mcp/cancel-tool-call", methods=["POST"])
        def mcp_cancel_tool_call() -> Response:
            req = MCPToolCancelRequest.model_validate(request.get_json())
            try:
                outcome = self._mcp_calls.cancel(req.session_id, req.request_id)
            except ToolCallBusyError as e:
                return Response(str(e), status=429, mimetype="text/plain")
            return Response(json.dumps({"request_id": req.request_id, "outcome": outcome}), mimetype="application/json")

        @self._app.route("/mcp/tool-call", methods=["POST"])
        @guarded
        def mcp_tool_call() -> Response:
            req = MCPToolCallRequest.model_validate(request.get_json())
            try:
                result = self._call_mcp_tool(req)
            except ToolCallInterrupted as e:
                return Response(str(e), status=408, mimetype="text/plain")
            except DuplicateToolCallError as e:
                return Response(str(e), status=409, mimetype="text/plain")
            except ToolCallBusyError as e:
                return Response(str(e), status=429, mimetype="text/plain")
            except Exception as e:
                log.warning("Shared MCP tool call failed: %s", e)
                return Response(f"{type(e).__name__}: {e}", status=400, mimetype="text/plain")
            body, content_type = ToolResultCodec.encode(result)
            return Response(body, content_type=content_type)

        @self._app.route("/call_facade_method", methods=["POST"])
        @guarded
        def call_facade_method() -> Response:
            call_request = CallFacadeMethodRequest.model_validate(request.get_json())
            try:
                result = self._call_facade_method(call_request)
            except Exception as e:
                # report the error to the client (which raises it in the REPL) instead of a generic server error page
                log.warning("Facade method call failed: %s", e)
                return Response(f"{type(e).__name__}: {e}", status=400, mimetype="text/plain")
            # NOTE: the result is pickled; the client (a Serena instance on the same machine) unpickles it
            return Response(pickle.dumps(result), mimetype="application/octet-stream")

    @staticmethod
    def _assert_mcp_runtime_root(requested_root: str, active_root: str) -> str:
        if not project_roots_equal(requested_root, active_root):
            raise ValueError(
                f"Shared MCP project routing mismatch: requested root {str(Path(requested_root).expanduser().resolve())!r}, "
                f"but the agent activated {str(Path(active_root).expanduser().resolve())!r}"
            )
        return str(Path(active_root).expanduser().resolve())

    def _ensure_mcp_runtime_binding(self, runtime: MCPProjectRuntime) -> None:
        """
        Ensure a pooled runtime remains bound to its canonical project root.

        The display project_name is deliberately ignored: git worktrees normally share it.
        If a prior operation left the dedicated runtime inactive or temporarily pointed at
        another worktree, repair the binding before exposing any tool state/result.
        """
        with runtime.binding_lock:
            active_project = runtime.agent.get_active_project()
            if active_project is None or not project_roots_equal(active_project.project_root, runtime.project_root):
                previous_root = None if active_project is None else active_project.project_root
                log.warning(
                    "Repairing shared MCP runtime binding: expected root=%s, active root=%s",
                    runtime.project_root,
                    previous_root,
                )
                runtime.agent.activate_project_from_path_or_name(runtime.project_root)
                active_project = runtime.agent.get_active_project()

            if active_project is None:
                raise ValueError(f"Shared MCP runtime for {runtime.project_root!r} has no active project after repair")
            self._assert_mcp_runtime_root(runtime.project_root, active_project.project_root)

    @staticmethod
    def _mcp_runtime_key(project_root: str, context: str) -> tuple[str, str]:
        root = Path(project_root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Shared MCP project root is not a directory: {root}")
        return project_root_key(root), context

    def _get_mcp_runtime(self, project_root: str, context: str) -> MCPProjectRuntime:
        key = self._mcp_runtime_key(project_root, context)

        with self._mcp_runtimes_lock:
            runtime = self._mcp_runtimes.get(key)
            if runtime is not None:
                runtime.last_access = time.monotonic()
                return runtime
            load_lock = self._mcp_runtime_load_locks.get(key)
            if load_lock is None:
                load_lock = threading.Lock()
                self._mcp_runtime_load_locks[key] = load_lock

        with load_lock:
            with self._mcp_runtimes_lock:
                runtime = self._mcp_runtimes.get(key)
                if runtime is not None:
                    runtime.last_access = time.monotonic()
                    return runtime

            from serena.agent import SerenaAgent
            from serena.config.context_mode import SerenaAgentContext

            # The shared daemon owns project services, so per-project dashboards/GUI windows
            # would only multiply memory and ports. Keep runtime agents headless.
            config = SerenaConfig.from_config_file().with_headless_mode_overrides()
            agent = SerenaAgent(project=key[0], serena_config=config, context=SerenaAgentContext.load(context))
            active_project = agent.get_active_project()
            if active_project is None:
                agent.on_shutdown()
                raise ValueError(f"Failed to activate shared MCP project {key[0]!r}")

            try:
                active_root = self._assert_mcp_runtime_root(key[0], active_project.project_root)
            except Exception:
                agent.on_shutdown()
                raise

            runtime = MCPProjectRuntime(
                agent=agent,
                project_root=active_root,
                context=context,
                last_access=time.monotonic(),
            )
            with self._mcp_runtimes_lock:
                self._mcp_runtimes[key] = runtime
                self._mcp_runtime_load_locks.pop(key, None)

            self._evict_mcp_runtimes()
            log.info("Created shared MCP runtime for %s (context=%s)", runtime.project_root, context)
            return runtime

    def _get_mcp_runtime_info(self, req: MCPRuntimeInfoRequest) -> MCPRuntimeInfoResponse:
        runtime = self._get_mcp_runtime(req.project_root, req.context)
        call = ToolCall(float(self.get_serena_config().tool_timeout))
        with self._mcp_runtime_use(runtime):
            call.acquire(runtime.binding_lock)
            try:
                self._ensure_mcp_runtime_binding(runtime)
                active_project = runtime.agent.get_active_project()
                assert active_project is not None

                # Project selection is owned by the bridge/router. Exposing activate_project would
                # let one conversation retarget the shared agent underneath other conversations.
                tool_names = [
                    tool.get_name()
                    for tool in runtime.agent.get_exposed_tool_instances()
                    if tool.get_name() != "activate_project"
                ]
                if req.session_id:
                    with self._mcp_runtimes_lock:
                        runtime.bridge_sessions[req.session_id] = time.monotonic()
                        runtime.last_access = time.monotonic()

                context = runtime.agent.get_context()
                return MCPRuntimeInfoResponse(
                    project_root=runtime.project_root,
                    project_id=project_root_key(runtime.project_root),
                    project_name=active_project.project_name,
                    tool_names=tool_names,
                    instructions=runtime.agent.create_connection_prompt(),
                    structured_tool_output=context.structured_tool_output,
                )
            finally:
                runtime.binding_lock.release()

    def _touch_mcp_bridge(self, req: MCPBridgeSessionRequest) -> None:
        key = self._mcp_runtime_key(req.project_root, req.context)
        with self._mcp_runtimes_lock:
            runtime = self._mcp_runtimes.get(key)
            if runtime is not None:
                now = time.monotonic()
                runtime.bridge_sessions[req.session_id] = now
                runtime.last_access = now

    def _close_mcp_bridge(self, req: MCPBridgeCloseRequest) -> None:
        key = self._mcp_runtime_key(req.project_root, req.context)
        runtime = None
        with self._mcp_runtimes_lock:
            runtime = self._mcp_runtimes.get(key)
            if runtime is not None:
                runtime.bridge_sessions.pop(req.session_id, None)
                runtime.last_access = time.monotonic()
        if runtime is not None:
            runtime.agent.close_session(req.session_id)

    @contextmanager
    def _mcp_runtime_use(self, runtime: MCPProjectRuntime, *, admit: bool = False) -> Iterator[None]:
        # pin both waiting requests and actual workers, whose lifetimes can differ after cancellation
        with self._mcp_runtimes_lock:
            if admit and runtime.active_calls >= self.MCP_RUNTIME_MAX_CALLS:
                raise ToolCallBusyError("Shared project runtime is busy; the operation was not submitted. Reduce concurrent calls.")
            runtime.active_calls += 1
        try:
            yield
        finally:
            with self._mcp_runtimes_lock:
                runtime.active_calls -= 1
                runtime.last_access = time.monotonic()

    @contextmanager
    def _mcp_call_guard(self, runtime: MCPProjectRuntime, req: MCPToolCallRequest, call: ToolCall) -> Iterator[None]:
        # hold the binding on the executor thread until the real operation exits, not until its caller gives up
        with self._mcp_runtime_use(runtime):
            call.acquire(runtime.binding_lock)
            try:
                with self._mcp_runtimes_lock:
                    runtime.current_call = call
                self._ensure_mcp_runtime_binding(runtime)
                exposed_names = {tool.get_name() for tool in runtime.agent.get_exposed_tool_instances()}
                if req.tool_name not in exposed_names:
                    raise ValueError(f"Tool {req.tool_name!r} is not exposed by this shared runtime")
                tool = runtime.agent.get_tool_by_name(req.tool_name)
                with self._mcp_runtimes_lock:
                    runtime.current_tool_name = req.tool_name
                    runtime.current_tool_read_only = tool.is_readonly()
                    runtime.current_session_id = req.session_id
                call.check()
                yield
            finally:
                with self._mcp_runtimes_lock:
                    runtime.current_call = None
                    runtime.current_tool_name = None
                    runtime.current_tool_read_only = False
                    runtime.current_session_id = None
                runtime.binding_lock.release()

    def get_recovery_evidence(self, scope: RecoveryScope) -> RecoveryEvidence:
        """Inspect a stalled caller-owned worker without entering its executor or binding lock."""
        with self._mcp_runtimes_lock:
            # reject cold loads and any unrelated, queued, or legacy work
            if self._mcp_runtime_load_locks:
                raise DaemonRecoveryRefusedError("Project runtime loading is still in progress.")
            target = self._mcp_runtimes.get((scope.project_root, scope.context))
            if target is None:
                raise DaemonRecoveryRefusedError("The caller has no loaded shared project runtime.")
            for runtime in self._mcp_runtimes.values():
                tasks = runtime.agent.get_current_tasks()
                if runtime is target:
                    if runtime.active_calls != 1 or len(tasks) != 1 or not tasks[0].is_running:
                        raise DaemonRecoveryRefusedError("Expected one stranded worker with no waiting or queued requests.")
                elif runtime.active_calls or tasks:
                    raise DaemonRecoveryRefusedError("Another project has active or queued work.")
            if self._agent is not None and self._agent.get_current_tasks():
                raise DaemonRecoveryRefusedError("Legacy project queries are still executing.")

            # use execution metadata captured by the actual worker, never agent-supplied claims
            call = target.current_call
            if call is None or target.current_session_id != scope.session_id or target.current_tool_name is None:
                raise DaemonRecoveryRefusedError("No stranded worker belongs to this bridge session.")
            return RecoveryEvidence(
                request_id=call.request_id,
                tool_name=target.current_tool_name,
                read_only=target.current_tool_read_only,
                running=call.running,
                overdue_seconds=call.overdue_seconds,
            )

    def _call_mcp_tool(self, req: MCPToolCallRequest) -> ToolResult:
        call = self._mcp_calls.register(req.session_id, req.request_id, req.deadline)
        if req.tool_name == "activate_project":
            raise ValueError("activate_project is disabled for shared MCP runtimes; project routing is bridge-controlled")
        runtime = self._get_mcp_runtime(req.project_root, req.context)
        call.check()

        with self._mcp_runtime_use(runtime, admit=True):
            # fail promptly while interrupted work is still draining; never restart a runtime underneath an edit
            with self._mcp_runtimes_lock:
                current = runtime.current_call
                if current is not None and current.interrupted and current.running:
                    raise ToolCallBusyError(
                        f"Shared runtime is still finishing interrupted call {current.request_id}; "
                        "the new operation was not submitted. Verify changes before retrying."
                    )
                now = time.monotonic()
                runtime.bridge_sessions[req.session_id] = now
                runtime.last_access = now
            tool = runtime.agent.get_tool_by_name(req.tool_name)
            call_context = ToolCallContext(call, self._mcp_call_guard(runtime, req, call))
            with call_context.activate():
                return tool.apply_ex(
                    catch_exceptions=False,
                    session_id_override=req.session_id,
                    **req.arguments,
                )

    def _evict_mcp_runtimes(self) -> None:
        now = time.monotonic()
        evicted: list[MCPProjectRuntime] = []
        stale_session_owners: list[tuple[MCPProjectRuntime, str]] = []
        with self._mcp_runtimes_lock:
            # Bridge processes renew short leases in the background. Drop stale leases so
            # a crashed/killed Codex bridge cannot pin a project runtime or its REPL
            # namespace forever.
            for runtime in self._mcp_runtimes.values():
                stale_sessions = [
                    session_id
                    for session_id, last_seen in runtime.bridge_sessions.items()
                    if now - last_seen >= self.MCP_BRIDGE_LEASE_SECONDS
                ]
                for session_id in stale_sessions:
                    runtime.bridge_sessions.pop(session_id, None)
                    stale_session_owners.append((runtime, session_id))

            inactive = [
                (key, runtime)
                for key, runtime in self._mcp_runtimes.items()
                if runtime.active_calls == 0
                and not runtime.bridge_sessions
                and not runtime.agent.get_current_tasks()
            ]

            # Time-based eviction.
            for key, runtime in inactive:
                if now - runtime.last_access >= self.MCP_RUNTIME_IDLE_SECONDS:
                    self._mcp_runtimes.pop(key, None)
                    evicted.append(runtime)

            # Capacity-based LRU eviction after the idle pass.
            if len(self._mcp_runtimes) > self.MCP_RUNTIME_MAX_PROJECTS:
                remaining_inactive = sorted(
                    (
                        (key, runtime)
                        for key, runtime in self._mcp_runtimes.items()
                        if runtime.active_calls == 0
                        and not runtime.bridge_sessions
                        and not runtime.agent.get_current_tasks()
                    ),
                    key=lambda item: item[1].last_access,
                )
                while len(self._mcp_runtimes) > self.MCP_RUNTIME_MAX_PROJECTS and remaining_inactive:
                    key, runtime = remaining_inactive.pop(0)
                    self._mcp_runtimes.pop(key, None)
                    evicted.append(runtime)

        for runtime, session_id in stale_session_owners:
            try:
                runtime.agent.close_session(session_id)
            except Exception as e:
                log.debug("Failed to release stale shared MCP session %s: %s", session_id, e)

        for runtime in evicted:
            log.info("Evicting shared MCP runtime for %s (context=%s)", runtime.project_root, runtime.context)
            try:
                runtime.agent.on_shutdown()
            except Exception as e:
                log.error("Failed to shut down evicted shared MCP runtime", exc_info=e)

    def _mcp_runtime_eviction_loop(self) -> None:
        while not self._mcp_runtime_stop_event.wait(self.MCP_RUNTIME_EVICTION_INTERVAL_SECONDS):
            self._evict_mcp_runtimes()

    def _get_project(self, project_root_or_name: str) -> "Project":
        """Gets the project with the given name, loading it if necessary."""
        serena_config = self.get_serena_config()
        registered_project = serena_config.get_registered_project(project_root_or_name)
        if registered_project is None:
            raise ValueError(f"Project '{project_root_or_name}' is not registered with Serena.")

        key = str(registered_project.project_root)

        # find or publish the per-project load lock while holding the shared dictionaries
        with self._loaded_projects_lock:
            project = self._loaded_projects_by_root.get(key)
            if project is not None:
                return project
            project_load_lock = self._project_load_locks_by_root.get(key)
            if project_load_lock is None:
                project_load_lock = threading.Lock()
                self._project_load_locks_by_root[key] = project_load_lock

        # initialize only this project; another project's cached lookup or cold load can proceed
        with project_load_lock:
            with self._loaded_projects_lock:
                project = self._loaded_projects_by_root.get(key)
                if project is not None:
                    return project

            with LogTime(f"Loading project '{project_root_or_name}'"):
                project = registered_project.get_project_instance(serena_config)
                project.create_language_server_manager()

            with self._loaded_projects_lock:
                self._loaded_projects_by_root[key] = project
            return project

    def _query_project(self, req: QueryProjectRequest) -> ToolResult:
        """Handle a /query_project request by invoking the agent on the specified project and tool.

        The active project is process-wide state, whereas ``apply_ex`` runs the tool on the
        agent's task executor thread. Without the lock, a second request entering
        ``active_project_context`` while the first request's tool is still executing would
        redirect that tool to the wrong project (and restore the wrong project afterwards).
        """
        project = self._get_project(req.project_name)
        agent = self._get_legacy_agent()
        with self._active_project_lock, agent.active_project_context(project):
            tool = agent.get_tool_by_name(req.tool_name)
            if not tool.is_readonly():
                raise ValueError(f"Tool '{req.tool_name}' is not read-only and cannot be executed via the query_project route")
            params = json.loads(req.tool_params_json)
            return tool.apply_ex(**params)

    def _call_facade_method(self, req: CallFacadeMethodRequest) -> Any:
        """
        Handles a /call_facade_method request by executing the facade method on the agent's REPL facades in the
        context of the specified project (see `_query_project` regarding the lock).
        """
        project = self._get_project(req.project_name)
        agent = self._get_legacy_agent()
        with self._active_project_lock, agent.active_project_context(project):
            facade = agent.get_repl().entrypoint.get_facade_(req.facade_name)
            method = facade.get_method(req.method_name)
            return agent.execute_task(lambda: method(*req.args, **req.kwargs))

    def run(self) -> None:
        """
        Run the server on the given host and port.
        """
        from flask import cli

        # suppress the default Flask startup banner
        # ty cannot model reassigning a third-party module's function attribute (it rejects any
        # replacement, even one with an identical signature), so the monkeypatch is suppressed here
        cli.show_server_banner = lambda *args, **kwargs: None  # ty: ignore[invalid-assignment]

        self._app.run(host=self._host, port=self._port, debug=False, use_reloader=False, threaded=True)


class ProjectServerClient:
    """Client for interacting with a running :class:`ProjectServer`.

    Upon instantiation, the client verifies that the server is reachable
    by sending a heartbeat request. If the server is not running, a
    :class:`ConnectionError` is raised.
    """

    def __init__(self, serena_config: SerenaConfig, host: str = "127.0.0.1", port: int | None = None) -> None:
        """
        :param host: the host address of the project server.
        :param port: explicit port override; otherwise resolve the environment and global configuration.
        :param serena_config: global configuration providing the endpoint, timeout, and authentication secret.
        :raises ConnectionError: if the project server is not reachable.
        """
        self._endpoint = ProjectServerEndpoint.from_config(serena_config, host, port)
        self._base_url = self._endpoint.base_url
        self._timeout = max(float(serena_config.tool_timeout), 1.0) + ToolCall.RESPONSE_GRACE_SECONDS
        auth_secret = serena_config.auth_secret
        self._headers = {"Authorization": f"Bearer {auth_secret}"}

        # distinguish authentication/configuration rejection from connection refusal
        self._endpoint.heartbeat(auth_secret)

    def get_mcp_runtime_info(self, project_root: str, context: str, session_id: str | None = None) -> MCPRuntimeInfoResponse:
        payload = MCPRuntimeInfoRequest(project_root=project_root, context=context, session_id=session_id).model_dump()
        response = requests_lib.post(
            f"{self._base_url}/mcp/runtime-info",
            json=payload,
            headers=self._headers,
            timeout=self._timeout,
        )
        self._endpoint.check_authentication(response)
        if not response.ok:
            raise ValueError(f"Shared MCP daemon error ({response.status_code}): {response.text[:2000]}")
        return MCPRuntimeInfoResponse.model_validate_json(response.text)

    def heartbeat_mcp_bridge(self, project_root: str, context: str, session_id: str) -> None:
        payload = MCPBridgeSessionRequest(project_root=project_root, context=context, session_id=session_id).model_dump()
        response = requests_lib.post(
            f"{self._base_url}/mcp/bridge-heartbeat",
            json=payload,
            headers=self._headers,
            timeout=5,
        )
        self._endpoint.check_authentication(response)
        response.raise_for_status()

    def close_mcp_bridge(self, project_root: str, context: str, session_id: str) -> None:
        payload = MCPBridgeSessionRequest(project_root=project_root, context=context, session_id=session_id).model_dump()
        try:
            requests_lib.post(
                f"{self._base_url}/mcp/bridge-close",
                json=payload,
                headers=self._headers,
                timeout=5,
            )
        except requests_lib.RequestException:
            # Bridge shutdown is best-effort; the daemon can eventually evict a stale runtime.
            pass

    def call_mcp_tool(
        self,
        project_root: str,
        context: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        payload = MCPToolCallRequest(
            project_root=project_root,
            context=context,
            session_id=session_id,
            tool_name=tool_name,
            arguments=arguments,
        ).model_dump()
        response = requests_lib.post(
            f"{self._base_url}/mcp/tool-call",
            json=payload,
            headers=self._headers,
            timeout=self._timeout,
        )
        self._endpoint.check_authentication(response)
        if not response.ok:
            raise ValueError(f"Shared MCP daemon error ({response.status_code}): {response.text[:2000]}")
        return ToolResultCodec.decode(response.text, response.headers.get("Content-Type", ""))

    def query_project(self, project_name: str, tool_name: str, tool_params_json: str) -> ToolResult:
        """
        Query a project by executing a Serena tool in its context.

        The interface matches :meth:`QueryProjectTool.apply
        <serena.tools.query_project_tools.QueryProjectTool.apply>`.

        :param project_name: the name of the project to query.
        :param tool_name: the name of the tool to execute. The tool must be read-only.
        :param tool_params_json: the parameters to pass to the tool, encoded as a JSON string.
        :return: the tool's text or native MCP content.
        """
        payload = QueryProjectRequest(
            project_name=project_name,
            tool_name=tool_name,
            tool_params_json=tool_params_json,
        ).model_dump()

        response = requests_lib.post(f"{self._base_url}/query_project", json=payload, headers=self._headers, timeout=self._timeout)
        self._endpoint.check_authentication(response)
        response.raise_for_status()
        return ToolResultCodec.decode(response.text, response.headers.get("Content-Type", ""))

    def call_facade_method(self, project_name: str, facade_name: str, method_name: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
        """
        Executes a (read-only) REPL facade method in the context of a project.

        :param project_name: the name of the project to query
        :param facade_name: the facade's name
        :param method_name: the method's name
        :param args: the positional arguments (JSON-serialisable)
        :param kwargs: the keyword arguments (JSON-serialisable)
        :return: the method's result, as returned by the server (unpickled; the server is a trusted local process)
        """
        payload = CallFacadeMethodRequest(
            project_name=project_name, facade_name=facade_name, method_name=method_name, args=args, kwargs=kwargs
        ).model_dump()
        response = requests_lib.post(f"{self._base_url}/call_facade_method", json=payload, headers=self._headers, timeout=self._timeout)
        self._endpoint.check_authentication(response)
        if not response.ok:
            raise ValueError(f"Project server error ({response.status_code}): {response.text[:2000]}")
        return pickle.loads(response.content)
