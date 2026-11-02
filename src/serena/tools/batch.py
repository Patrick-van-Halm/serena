# SPDX-License-Identifier: GPL-3.0-or-later

"""Typed, bounded MCP batches over the existing per-item tool implementations."""

import inspect
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any, NotRequired, get_type_hints

import docstring_parser
from pydantic import ConfigDict, Field, TypeAdapter, with_config
from typing_extensions import TypedDict

from serena.util.tool_call import ToolCallContext, ToolCallInterrupted
from solidlsp.ls_exceptions import SolidLSPException

if TYPE_CHECKING:
    from serena.tools.tools_base import Tool


@dataclass
class _BatchResult:
    index: int
    request: dict[str, Any]
    status: str
    result: str | None = None
    error: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class ToolBatch:
    """Public batch name and array parameter for a tool's unchanged scalar implementation.

    The request schema is derived from the scalar signature, keeping parameter types,
    defaults and documentation in one place. Typed dictionaries stay JSON-compatible
    through both direct MCP validation and the shared bridge.
    """

    name: str
    parameter: str
    max_items: int = 8

    _IDENTITY_FIELDS = frozenset(
        {
            "relative_path",
            "source_path",
            "destination_path",
            "name_path",
            "name_path_pattern",
            "reference_file",
            "memory_name",
            "old_name",
            "new_name",
        }
    )
    _METADATA_RESERVE = 128

    @lru_cache(maxsize=128)  # noqa: B019 - bounded schemas for immutable tool definitions and bridge proxy classes
    def apply_function(self, tool_class: type["Tool"]) -> Callable[..., str]:
        """Build the typed public callable once for each concrete tool class."""
        # derive strict JSON request fields from the single implementation
        scalar = getattr(tool_class, "apply")
        scalar_signature = inspect.signature(scalar)
        hints = get_type_hints(scalar, include_extras=True)
        documentation = docstring_parser.parse(inspect.getdoc(scalar) or "")
        descriptions = {param.arg_name: param.description for param in documentation.params}
        fields: dict[str, Any] = {}
        for name, parameter in scalar_signature.parameters.items():
            if name == "self":
                continue
            annotation = hints[name]
            if parameter.default is not inspect.Parameter.empty:
                field = Field(default=parameter.default, description=descriptions.get(name))
                annotation = NotRequired[Annotated[annotation, field]]
            else:
                annotation = Annotated[annotation, Field(description=descriptions.get(name))]
            fields[name] = annotation
        request_type = with_config(ConfigDict(extra="forbid", strict=True))(TypedDict(f"{tool_class.__name__}Request", fields))
        requests_type = Annotated[list[request_type], Field(min_length=1, max_length=self.max_items, strict=True)]
        budget_type = Annotated[int, Field(strict=True, ge=4096, le=200000)]
        adapter = TypeAdapter(requests_type)
        budget_adapter = TypeAdapter(budget_type)
        signature = inspect.Signature(
            [
                inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                inspect.Parameter(self.parameter, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=requests_type),
                inspect.Parameter("max_answer_chars", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=20000, annotation=budget_type),
            ],
            return_annotation=str,
        )

        def apply(tool: "Tool", *args: Any, **kwargs: Any) -> str:
            # validate the whole call before allowing the first operation to start
            bound = signature.bind(tool, *args, **kwargs)
            bound.apply_defaults()
            requests = adapter.validate_python(bound.arguments[self.parameter])
            budget = budget_adapter.validate_python(bound.arguments["max_answer_chars"])
            return self._execute(tool, scalar, requests, budget)

        # expose the same concrete signature to both MCP transports and Python inspection
        apply.__name__ = self.name
        apply.__signature__ = signature  # ty: ignore[unresolved-attribute]
        apply.__annotations__ = {self.parameter: requests_type, "max_answer_chars": budget_type, "return": str}
        policy = (
            "Entries run serially against the state left by preceding entries. A raised exception stops the batch: "
            "the attempted item is unknown and later items are not_applied. Writes are not atomic and are never "
            "automatically rolled back or retried. A completed item only means the underlying call returned; "
            "inspect its result for refusals or conflicts. Verify state after an interrupted call."
            if tool_class.can_edit()
            else "Entries run serially, not as a snapshot. Ordinary errors are reported per item without discarding successful siblings."
        )
        apply.__doc__ = (
            f"Execute one to {self.max_items} requests in input order. Use a one-item `{self.parameter}` array for one operation.\n\n"
            f"{policy}\n\n"
            "All request shapes and types are validated before execution; operational path, match and backend checks happen per item. "
            "Cancellation stops subsequent work and may prevent the result report from reaching the client. "
            "Results retain every request index and target; truncated result/error strings are explicit prefixes.\n\n"
            f"Each request performs the following operation:\n{documentation.description or tool_class.get_tool_description()}\n\n"
            f":param {self.parameter}: ordered request objects; each item uses the documented per-operation fields and defaults\n"
            ":param max_answer_chars: maximum characters in the complete JSON response, including metadata; 4096..200000\n"
            ":return: JSON results with index, request identity, status, result, error and truncated; no outcome records are dropped"
        )
        return apply

    @staticmethod
    def _encode(results: list[_BatchResult]) -> str:
        return json.dumps({"results": [asdict(result) for result in results]}, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _prefix(value: str, budget: int) -> str:
        # measure escaped JSON so control characters cannot exceed the output allowance
        low, high = 0, min(len(value), max(0, budget))
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(value[:middle], ensure_ascii=True)) - 2 <= budget:
                low = middle
            else:
                high = middle - 1
        return value[:low]

    def _execute(self, tool: "Tool", scalar: Callable[..., str], requests: list[dict[str, Any]], budget: int) -> str:
        # reserve all outcome identities before any operation can modify state
        editing = tool.can_edit()
        results = [
            _BatchResult(
                index=index,
                request={key: value for key, value in request.items() if key in self._IDENTITY_FIELDS},
                status="not_applied" if editing else "not_executed",
            )
            for index, request in enumerate(requests)
        ]
        if len(self._encode(results)) + self._METADATA_RESERVE * len(results) > budget:
            raise ValueError("max_answer_chars cannot hold all request identities and outcome metadata")

        # retain read failures independently; stop uncertain writes without replaying them
        context = ToolCallContext.current()
        for index, (request, result) in enumerate(zip(requests, results, strict=True)):
            if context is not None:
                context.call.check()
            failed = False
            try:
                value = scalar(tool, **request)
                result.status = "completed"
            except ToolCallInterrupted:
                raise
            except Exception as error:
                if not editing and isinstance(error, SolidLSPException) and error.is_language_server_terminated():
                    raise
                failed = True
                value = f"{type(error).__name__}: {error}"
                result.status = "unknown" if editing else "error"

            # share remaining space while retaining the unstarted suffix's metadata
            field = "error" if failed else "result"
            setattr(result, field, "")
            remaining = len(results) - index - 1
            available = budget - len(self._encode(results)) - self._METADATA_RESERVE * remaining
            prefix = self._prefix(value, max(0, available) // (remaining + 1))
            setattr(result, field, prefix)
            result.truncated = len(prefix) < len(value)
            if failed and editing:
                break

        return self._encode(results)
