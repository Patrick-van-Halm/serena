# SPDX-License-Identifier: GPL-3.0-or-later

from typing import NotRequired

from typing_extensions import TypedDict

from serena.agent_workflow.directories import DirectoryBatchReader, DirectoryRequest
from serena.tools.tools_base import Tool, ToolMarkerDoesNotRequireActiveProject
from serena.util.tool_call import ToolCallContext


class DirectoryInput(TypedDict):
    """JSON-compatible directory request for direct and bridge transports."""

    relative_path: str
    recursive: NotRequired[bool]
    skip_ignored_files: NotRequired[bool]
    max_depth: NotRequired[int]


class ListDirsTool(Tool, ToolMarkerDoesNotRequireActiveProject):
    """List several known directories in one bounded, read-only request."""

    def apply(self, directories: list[DirectoryInput], max_entries_per_dir: int = 200, max_total_chars: int = 20000) -> str:
        """Retrieve one to eight directory listings without guessing paths.

        Use this tool even for one directory, passing a one-item directories array.

        Each item has relative_path, recursive (false), skip_ignored_files (true),
        and max_depth (4). Missing or denied paths return individual errors.
        Discovered symlinks are reported separately and are not traversed. Listings
        are not filesystem snapshots. Truncation is explicit; a partial listing does
        not establish that another file is absent.

        :param directories: directory paths and optional per-directory traversal settings
        :param max_entries_per_dir: inspected-entry limit per directory, including ignored entries; 1..5000
        :param max_total_chars: complete JSON response limit; 2048..200000 characters
        :return: independent dirs/files/symlinks arrays with errors and truncation reasons
        """
        context = ToolCallContext.current()
        reader = DirectoryBatchReader(self.file_system_project, context.call.check if context else None)
        requests = [DirectoryRequest.model_validate(item) for item in directories]
        return reader.read(requests, max_entries_per_dir, max_total_chars)
