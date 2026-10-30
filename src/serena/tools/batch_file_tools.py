# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Annotated, NotRequired

from pydantic import ConfigDict, Field, StrictInt, with_config
from typing_extensions import TypedDict

from serena.agent_workflow.files import FileBatchReader, FileReadRequest
from serena.tools.tools_base import Tool
from serena.util.raw_file import RawFileContent
from serena.util.tool_call import ToolCallContext


@with_config(ConfigDict(extra="forbid", strict=True))
class FileReadInput(TypedDict):
    """JSON-compatible text-file request for direct and shared bridge transports."""

    relative_path: str
    start_line: NotRequired[int]
    end_line: NotRequired[int | None]


class ReadFilesTool(Tool):
    """Read several known text files or line ranges in one bounded, read-only request."""

    def apply(
        self,
        files: Annotated[list[FileReadInput], Field(min_length=1, max_length=FileBatchReader.MAX_FILES)],
        max_chars_per_file: StrictInt = 8000,
        max_total_chars: StrictInt = 20000,
        max_bytes_per_file: StrictInt = RawFileContent.DEFAULT_MAX_BYTES,
    ) -> str:
        """Read up to eight known text files without guessing paths.

        Each item has relative_path, start_line (0), and end_line (null). Lines are
        zero-based; end_line is inclusive and nonnegative. A negative start_line
        counts from the end. Contents use project encoding and normalized LF newlines.
        Missing, denied, binary, or oversized files produce individual errors without
        discarding successful siblings. Truncated content is an explicit prefix, which
        can end within a line. file_sha256 covers original bytes; text_sha256 covers
        the full normalized text, not just the requested range or returned prefix.
        Reads are serial, not a multi-file snapshot. Only local regular files are
        supported; use read_file for encoded JetBrains external paths and read_file_raw
        for images/binary files. The underlying read_file tool must remain enabled.

        :param files: known file paths and optional inclusive line ranges
        :param max_chars_per_file: decoded text limit per item; 1..200000
        :param max_total_chars: complete JSON response limit; 2048..200000 characters
        :param max_bytes_per_file: whole-file byte limit even for ranges; 5 MiB default, 20 MiB maximum
        :return: ordered results with content, individual errors, hashes and truncation reasons
        """
        from serena.tools.file_tools import ReadFileTool

        if not self.agent.is_tool_function_available(ReadFileTool):
            raise ValueError("read_files requires read_file to be enabled in the active context")
        requests = [FileReadRequest.model_validate(item) for item in files]
        context = ToolCallContext.current()
        reader = FileBatchReader(self.project, context.call.check if context else None)
        return reader.read(requests, max_chars_per_file, max_total_chars, max_bytes_per_file)
