"""Resolved project-server endpoints and non-destructive health checks."""

# SPDX-License-Identifier: GPL-3.0-or-later

import errno
import hashlib
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from serena.constants import SerenaPorts

if TYPE_CHECKING:
    from serena.config.serena_config import SerenaConfig


class ProjectServerEndpointError(ConnectionError):
    """A reachable or indeterminate endpoint that must not trigger automatic startup."""


class ProjectServerAuthenticationError(ProjectServerEndpointError):
    """An endpoint/configuration mismatch reported by authentication rejection."""


class ProjectServerTimeoutError(ProjectServerEndpointError):
    """A timed-out heartbeat, not evidence that the selected endpoint is unoccupied."""


class ProjectServerUnavailableError(ConnectionError):
    """A refused connection, for which local daemon startup may be attempted."""


@dataclass(frozen=True)
class ProjectServerEndpoint:
    """Validated, immutable address shared by listeners, clients, and startup coordination."""

    host: str = "127.0.0.1"
    port: int = SerenaPorts.PROJECT_SERVER_PORT

    PORT_ENV_VAR = "SERENA_PROJECT_SERVER_PORT"

    def __post_init__(self) -> None:
        # reject ambiguous or invalid addresses before accessing the network
        self.validate_port(self.port)
        host = self.host.strip().lower().removesuffix(".")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host == "localhost":
            host = "127.0.0.1"
        if not host or any(character in host for character in "/?#@"):
            raise ValueError(f"Invalid project server host: {self.host!r}")
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            if ":" in host or any(character.isspace() for character in host):
                raise ValueError(f"Invalid project server host: {self.host!r}") from None
        object.__setattr__(self, "host", host)

    @staticmethod
    def validate_port(port: object, source: str = "project_server_port") -> int:
        """Validate a TCP port without silently coercing Boolean, fractional, or null values."""
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{source} must be an integer between 1 and 65535, got {port!r}")
        return port

    @classmethod
    def from_config(cls, config: "SerenaConfig", host: str = "127.0.0.1", port: int | None = None) -> "ProjectServerEndpoint":
        """
        Resolve explicit port, environment override, then the host-global configuration.

        Environment overrides are transient and never written back to the configuration.
        """
        # resolve once so clients and their spawned daemon retain the same endpoint
        if port is not None:
            resolved = cls.validate_port(port, "explicit project server port")
        elif cls.PORT_ENV_VAR in os.environ:
            value = os.environ[cls.PORT_ENV_VAR].strip()
            if not value.isascii() or not value.isdecimal():
                raise ValueError(f"{cls.PORT_ENV_VAR} must be an integer between 1 and 65535, got {value!r}")
            resolved = cls.validate_port(int(value), cls.PORT_ENV_VAR)
        else:
            resolved = cls.validate_port(config.project_server_port)
        return cls(host=host, port=resolved)

    @property
    def base_url(self) -> str:
        """HTTP origin, including brackets for IPv6 literals."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def is_loopback(self) -> bool:
        """Whether the endpoint is an explicit loopback address eligible for local startup."""
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def startup_lock_path(self, home: Path) -> Path:
        """Per-user startup lock scoped to the canonical host and port, not to a project."""
        host_key = hashlib.sha256(self.host.encode("utf-8")).hexdigest()[:16]
        return home / f"shared-mcp-daemon-{host_key}-{self.port}.lock"

    def matches_listener(self, host: str, port: int) -> bool:
        """Whether a local TCP listening address can serve this endpoint."""
        if port != self.port:
            return False
        try:
            address = ipaddress.ip_address(host)
            target = ipaddress.ip_address(self.host)
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address == target or (address.is_unspecified and address.version == target.version)

    def check_authentication(self, response: requests.Response) -> None:
        """Report authentication rejection without treating an occupied port as an absent daemon."""
        if response.status_code in (401, 403):
            raise ProjectServerAuthenticationError(
                f"Serena endpoint/configuration mismatch at {self.base_url}: authentication rejected "
                f"(HTTP {response.status_code}). Check SERENA_HOME, auth_secret, project_server_port, "
                f"and {self.PORT_ENV_VAR} in the client and daemon environments. "
                "No replacement daemon will be launched for this rejection."
            )

    def heartbeat(self, auth_secret: str, timeout: float = 5.0) -> dict[str, Any]:
        """Authenticate and validate a heartbeat; distinguish timeouts from confirmed refusal."""
        # distinguish refusal from timeouts, HTTP errors, redirects, and invalid service responses
        if not auth_secret:
            raise ProjectServerAuthenticationError(f"Serena endpoint/configuration mismatch at {self.base_url}: auth_secret is empty")
        try:
            response = requests.get(
                f"{self.base_url}/heartbeat",
                headers={"Authorization": f"Bearer {auth_secret}"},
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.Timeout as error:
            raise ProjectServerTimeoutError(f"Project server health check timed out at {self.base_url}: {error}") from error
        except requests.ConnectionError as error:
            if not isinstance(error, requests.exceptions.ProxyError) and self._is_connection_refused(error):
                raise ProjectServerUnavailableError(f"No project server is accepting connections at {self.base_url}") from error
            raise ProjectServerEndpointError(f"Project server health check failed at {self.base_url}: {error}") from error
        except requests.RequestException as error:
            raise ProjectServerEndpointError(f"Project server health check failed at {self.base_url}: {error}") from error

        with response:
            self.check_authentication(response)
            if response.status_code != 200:
                raise ProjectServerEndpointError(
                    f"Serena endpoint/configuration mismatch at {self.base_url}: heartbeat returned HTTP {response.status_code}. "
                    "An existing service will not be replaced."
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise ProjectServerEndpointError(f"Invalid project server heartbeat at {self.base_url}: expected JSON") from error
            if not isinstance(payload, dict) or payload.get("status") != "alive":
                raise ProjectServerEndpointError(f"Invalid project server heartbeat at {self.base_url}: expected status 'alive'")
            return payload

    @staticmethod
    def _is_connection_refused(error: BaseException) -> bool:
        # inspect wrapped socket errors without relying on translated exception text
        pending = [error]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, OSError) and (current.errno == errno.ECONNREFUSED or getattr(current, "winerror", None) == 10061):
                return True
            for nested in (current.__cause__, current.__context__, getattr(current, "reason", None), *current.args):
                if isinstance(nested, BaseException):
                    pending.append(nested)
        return False
