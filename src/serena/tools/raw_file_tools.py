# SPDX-License-Identifier: GPL-3.0-or-later

import json
from typing import Annotated, NotRequired

from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from mcp_types import CallToolResult, TextContent
from pydantic import ConfigDict, Field, StrictInt, TypeAdapter, with_config
from typing_extensions import TypedDict

from serena.util.raw_file import RawFileContent
from serena.util.tool_call import ToolCallContext, ToolCallInterrupted

from .tools_base import Tool, ToolMarkerDoesNotRequireActiveProject

RawFileByteLimit = Annotated[StrictInt, Field(ge=1, le=RawFileContent.HARD_MAX_BYTES)]


@with_config(ConfigDict(extra="forbid", strict=True))
class RawFileReadInput(TypedDict):
    """Known file and its optional original-byte limit."""

    relative_path: Annotated[str, Field(min_length=1, max_length=4096)]
    max_bytes: NotRequired[RawFileByteLimit]


RawFileReadInputs = Annotated[list[RawFileReadInput], Field(min_length=1, max_length=8)]


@with_config(ConfigDict(extra="forbid", strict=True))
class _RawFileBatchInput(TypedDict):
    files: RawFileReadInputs
    max_total_bytes: RawFileByteLimit


class ReadFilesRawTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """Read original bytes from known files, returning images as native multimodal MCP content."""

    _input_adapter = TypeAdapter(_RawFileBatchInput)

    @classmethod
    def get_apply_fn_metadata_from_cls(cls, structured_output: bool | None = None) -> FuncMetadata:
        # reject misspelled byte budgets before the SDK discards unknown arguments
        metadata = super().get_apply_fn_metadata_from_cls(structured_output=structured_output)
        metadata.arg_model.model_config["extra"] = "forbid"
        metadata.arg_model.model_rebuild(force=True)
        return metadata

    def apply(self, files: RawFileReadInputs, max_total_bytes: RawFileByteLimit = RawFileContent.HARD_MAX_BYTES) -> CallToolResult:
        """
        Read one to eight regular files without text decoding or image transformations.
        Use a one-item files array for a single file. Each item accepts relative_path
        and optional max_bytes (5 MiB default, 20 MiB maximum).
        PNG, JPEG, GIF, and WebP are returned as MCP image blocks; all other files are
        embedded binary resources. Display and model support depend on the MCP client.
        Prefer read_files for source code and other text.

        The entire request shape and limits are validated before reading. Missing,
        denied, or oversized files return individual errors without discarding siblings.
        Each metadata block identifies the request index and path; a successful block
        is followed by its native image or resource. Files never return partial bytes.
        The total limit counts original bytes, excluding base64 expansion and metadata;
        each read is limited to the remaining budget. Reads are serial, not a snapshot.

        :param files: known file paths relative to the active project or startup working directory,
            with optional per-file byte limits; paths or symlink targets outside that root
            require the host-global full_access_mode setting
        :param max_total_bytes: total original payload bytes; 1..20971520 (20 MiB), default 20 MiB
        :return: ordered per-file outcomes and native content with MIME type, byte count, and SHA-256
        """
        # reject malformed later items before opening any file
        request = self._input_adapter.validate_python({"files": files, "max_total_bytes": max_total_bytes})
        remaining = request["max_total_bytes"]
        context = ToolCallContext.current()
        result = CallToolResult(content=[])

        # retain each independent outcome without truncating native payloads
        for index, item in enumerate(request["files"]):
            if context is not None:
                context.call.check()
            try:
                if remaining == 0:
                    raise ValueError("Batch max_total_bytes budget exhausted; no content was read.")
                raw = RawFileContent.read(
                    self.file_system_project, item["relative_path"], min(item.get("max_bytes", RawFileContent.DEFAULT_MAX_BYTES), remaining)
                )
                if context is not None:
                    context.call.check()
                native = raw.to_mcp_result()
                metadata = native.content[0]
                assert isinstance(metadata, TextContent)
                metadata.text = json.dumps(
                    dict(json.loads(metadata.text), index=index, relative_path=item["relative_path"], status="completed", error=None),
                    ensure_ascii=True,
                )
                remaining -= len(raw.data)
                result.content.extend(native.content)
            except ToolCallInterrupted:
                raise
            except Exception as error:
                result.content.append(
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "index": index,
                                "relative_path": item["relative_path"],
                                "status": "error",
                                "error": f"{type(error).__name__}: {error}",
                            },
                            ensure_ascii=True,
                        ),
                    )
                )
        return result
