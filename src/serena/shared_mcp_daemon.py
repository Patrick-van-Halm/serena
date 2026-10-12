"""Endpoint-scoped shared daemon startup with verified process ownership."""

# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from filelock import FileLock, Timeout

from serena.project_server_endpoint import ProjectServerEndpoint, ProjectServerEndpointError, ProjectServerUnavailableError
from serena.shared_mcp_client import IncompatibleSharedMCPDaemonError, SharedMCPDaemonClient

if TYPE_CHECKING:
    from serena.config.serena_config import SerenaConfig

log = logging.getLogger(__name__)


class ProjectServerStartupError(ProjectServerEndpointError):
    """Failed owned startup, including captured daemon diagnostics."""


class SharedMCPDaemon:
    """Coordinate one local endpoint without replacing unrelated or unauthenticated services."""

    _BOOTSTRAP = "from serena.cli import top_level; top_level()"
    _DIAGNOSTIC_TAIL_BYTES = 8192

    def __init__(self, config: "SerenaConfig", endpoint: ProjectServerEndpoint | None = None) -> None:
        # freeze configuration routing before health checks, locks, or subprocess startup
        self._config = config
        self._endpoint = endpoint or ProjectServerEndpoint.from_config(config)
        if config.config_file_path is not None:
            self._home = Path(config.config_file_path).parent.resolve()
        else:
            from serena.config.serena_config import SerenaPaths

            self._home = Path(SerenaPaths().serena_user_home_dir).resolve()

    def ensure_running(self, startup_timeout: float = 30.0) -> SharedMCPDaemonClient:
        """
        Reuse a compatible daemon or start one after a refused connection.

        Auth failures, invalid HTTP responses, and indeterminate network failures are
        terminal. Only an authenticated incompatible Serena listener owned by this user
        may be replaced; ownership is verified against the resolved endpoint.
        """
        # budget lock contention and readiness checks together
        if not math.isfinite(startup_timeout) or startup_timeout <= 0:
            raise ValueError("startup_timeout must be finite and positive")
        deadline = time.monotonic() + startup_timeout
        try:
            return self._connect(deadline)
        except (ProjectServerUnavailableError, IncompatibleSharedMCPDaemonError):
            pass

        # serialize startup for this endpoint without blocking another port
        if not self._endpoint.is_loopback:
            raise ProjectServerEndpointError(
                f"Automatic daemon startup/replacement requires a loopback endpoint: {self._endpoint.base_url}"
            )
        self._home.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self._endpoint.startup_lock_path(self._home)), timeout=self._remaining(deadline))
        try:
            with lock:
                try:
                    return self._connect(deadline)
                except IncompatibleSharedMCPDaemonError as error:
                    self._replace_stale_daemon(error.pid)
                except ProjectServerUnavailableError:
                    pass
                self._remaining(deadline)
                return self._start_and_wait(deadline)
        except Timeout as error:
            raise ProjectServerStartupError(f"Timed out waiting for the startup lock for {self._endpoint.base_url}") from error

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProjectServerStartupError(f"Shared Serena daemon startup timed out at {self._endpoint.base_url}")
        return remaining

    def _connect(self, deadline: float) -> SharedMCPDaemonClient:
        return SharedMCPDaemonClient(
            self._config,
            host=self._endpoint.host,
            port=self._endpoint.port,
            heartbeat_timeout=min(5.0, self._remaining(deadline)),
        )

    def _command(self) -> list[str]:
        # use the bridge's interpreter instead of an unrelated installation found on PATH
        return [
            sys.executable,
            "-c",
            self._BOOTSTRAP,
            "start-project-server",
            "--host",
            self._endpoint.host,
            "--port",
            str(self._endpoint.port),
            "--log-level",
            "WARNING",
        ]

    def _start_and_wait(self, deadline: float) -> SharedMCPDaemonClient:
        # capture both startup streams in a private file, avoiding unread PIPE deadlocks
        logs = self._home / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        process: subprocess.Popen | None = None
        owned_process: psutil.Process | None = None
        with tempfile.NamedTemporaryFile(
            prefix=f"project-server-startup-{self._endpoint.port}-", suffix=".log", dir=logs, delete=False
        ) as output:
            log_path = Path(output.name)
            environment = os.environ.copy()
            environment["SERENA_HOME"] = str(self._home)
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": output,
                "stderr": output,
                "close_fds": True,
                "env": environment,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            else:
                kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(self._command(), **kwargs)
                try:
                    owned_process = psutil.Process(process.pid)
                except psutil.NoSuchProcess:
                    pass
                last_error: Exception | None = None
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"daemon exited with code {process.returncode}")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"daemon did not become ready before the startup deadline; last health check: {last_error}")
                    try:
                        client = self._connect(deadline)
                    except ProjectServerUnavailableError as error:
                        last_error = error
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    else:
                        # a manually started compatible daemon may have won the bind race
                        if client.pid != process.pid:
                            self._stop_owned_child(process, owned_process)
                        log.info("Shared Serena daemon ready at %s; startup log: %s", self._endpoint.base_url, log_path)
                        return client
            except BaseException as error:
                # clean up only this attempt's child, never a process discovered through the endpoint
                cleanup_error: Exception | None = None
                if process is not None:
                    try:
                        self._stop_owned_child(process, owned_process)
                    except Exception as failure:
                        cleanup_error = failure
                if not isinstance(error, Exception):
                    if cleanup_error is not None:
                        log.error("Owned daemon cleanup failed: %s", cleanup_error)
                    raise
                details = self._read_diagnostics(log_path)
                reason = str(error).replace(self._config.auth_secret, "<redacted>") if self._config.auth_secret else str(error)
                message = f"Failed to start shared Serena daemon at {self._endpoint.base_url}: {reason}. Startup log: {log_path}"
                if process is not None:
                    message += f"; child exit code: {process.returncode}"
                if cleanup_error is not None:
                    message += f"; owned child cleanup failed: {cleanup_error}"
                if details:
                    message += f"\nDaemon startup output (tail):\n{details}"
                raise ProjectServerStartupError(message) from error

    def _read_diagnostics(self, path: Path) -> str:
        # report a bounded tail and avoid echoing the configured bearer secret
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - self._DIAGNOSTIC_TAIL_BYTES))
                details = stream.read(self._DIAGNOSTIC_TAIL_BYTES).decode("utf-8", errors="replace").strip()
            return details.replace(self._config.auth_secret, "<redacted>") if self._config.auth_secret else details
        except OSError as error:
            return f"Could not read startup log: {error}"

    @classmethod
    def _is_daemon_command(cls, command: list[str]) -> bool:
        # match entrypoints and the subcommand structurally, never arbitrary argument substrings
        if not command:
            return False
        scripts = {"serena", "serena-agent", "serena.exe", "serena-agent.exe"}
        if Path(command[0]).name.lower() in scripts:
            return len(command) > 1 and command[1] == "start-project-server"
        executable = Path(command[0]).name.lower()
        if not executable.startswith(("python", "pypy")):
            return False
        if len(command) >= 3 and Path(command[1]).name.lower() in scripts:
            return command[2] == "start-project-server"
        return len(command) >= 4 and command[1:4] == ["-c", cls._BOOTSTRAP, "start-project-server"]

    def _owned_listener(self, pid: int) -> psutil.Process:
        # verify user, identity, and listening socket before permitting a destructive action
        if pid == os.getpid():
            raise ProjectServerEndpointError("Refusing to replace the current process")
        try:
            process = psutil.Process(pid)
            if os.name == "posix":
                same_user = process.uids().real == os.getuid()
            else:
                same_user = process.username() == psutil.Process().username()
            if not same_user or not self._is_daemon_command(process.cmdline()):
                raise ProjectServerEndpointError(f"Refusing to terminate pid={pid}: not a Serena project server owned by this user")
            connections = process.net_connections(kind="tcp")
            if not any(
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and self._endpoint.matches_listener(connection.laddr.ip, connection.laddr.port)
                for connection in connections
            ):
                raise ProjectServerEndpointError(f"Refusing to terminate pid={pid}: not the listener at {self._endpoint.base_url}")
            return process
        except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
            raise ProjectServerEndpointError(
                f"Cannot verify ownership of daemon pid={pid} at {self._endpoint.base_url}: {error}"
            ) from error

    def _replace_stale_daemon(self, pid: int | None) -> None:
        # discover only this endpoint when an authenticated legacy heartbeat omits its PID
        if pid is None:
            try:
                candidates = {
                    connection.pid
                    for connection in psutil.net_connections(kind="tcp")
                    if connection.pid is not None
                    and connection.status == psutil.CONN_LISTEN
                    and connection.laddr
                    and self._endpoint.matches_listener(connection.laddr.ip, connection.laddr.port)
                }
            except (psutil.AccessDenied, OSError) as error:
                raise ProjectServerEndpointError(f"Cannot discover the listener at {self._endpoint.base_url}: {error}") from error
            if len(candidates) != 1:
                raise ProjectServerEndpointError(
                    f"Cannot identify one owned Serena listener at {self._endpoint.base_url}; no process was terminated"
                )
            pid = candidates.pop()
        process = self._owned_listener(pid)
        log.info("Replacing incompatible Serena daemon pid=%s at %s", pid, self._endpoint.base_url)
        self._terminate_tree(process)

    @staticmethod
    def _is_executing(process: psutil.Process) -> bool:
        # a zombie has exited even if a different parent has not reaped its PID yet
        try:
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    @classmethod
    def _terminate_tree(cls, process: psutil.Process) -> None:
        # retain psutil identities so reused PIDs are not signalled during cleanup
        try:
            descendants = process.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        processes = [*reversed(descendants), process]
        for item in processes:
            try:
                item.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=5.0)
        alive = [item for item in alive if cls._is_executing(item)]
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=2.0)
        alive = [item for item in alive if cls._is_executing(item)]
        if alive:
            raise ProjectServerStartupError(f"Could not terminate owned daemon process tree: {[item.pid for item in alive]}")

    @classmethod
    def _stop_owned_child(cls, process: subprocess.Popen, identity: psutil.Process | None) -> None:
        # reap only the Popen child started by this attempt, including interrupted startup
        if process.poll() is None:
            if identity is not None:
                cls._terminate_tree(identity)
            else:
                process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
