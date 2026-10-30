# SPDX-License-Identifier: GPL-3.0-or-later

import json
from typing import Literal

from typing_extensions import NotRequired, TypedDict

from serena.tools.tools_base import Tool, ToolMarkerCanEdit


class ContentReplacementInput(TypedDict):
    """JSON-compatible per-file replacement for direct and bridge MCP transports."""

    relative_path: str
    needle: str
    repl: str
    mode: Literal["literal", "regex"]
    allow_multiple_occurrences: NotRequired[bool]
    expected_count: NotRequired[int]
    expected_content_sha256: NotRequired[str]


class ReplaceContentsTool(Tool, ToolMarkerCanEdit):
    """Apply different content replacements across explicit files in one validated batch."""

    def apply(self, replacements: list[ContentReplacementInput], dry_run: bool = False, max_answer_chars: int = 20000) -> str:
        """Replace content across up to 16 files with at most 50 replacement entries.

        Entries use relative_path, needle, repl, mode (literal or regex), and optional
        allow_multiple_occurrences, expected_count, expected_content_sha256. Regex
        behavior and $!1 backreferences match replace_content. Entries for the same
        canonical file are evaluated in input order against the staged text.
        Hash guards always refer to the original editor text encoded as UTF-8;
        dry_run returns these hashes. They are not read_file_raw byte hashes.

        All entries are preflighted before writing; a validation failure changes no
        files. Each changed file is written once through Serena's existing editor.
        Multi-file writes are NOT atomic. A later failure reports applied, unknown,
        and not_applied files; no rollback or automatic retry occurs. Verify state
        after interrupted or unknown outcomes. The underlying replace_content tool
        must be enabled. Dry runs still require editing permission.

        :param replacements: explicit replacement objects; different patterns and replacements are supported
        :param dry_run: validate and return bounded diffs without writes
        :param max_answer_chars: complete response limit, 4096..200000 characters
        :return: JSON outcome with per-file status, match counts, and content hashes
        """
        from serena.agent_workflow.replacements import BatchContentReplacer, ProjectReplacementWorkspace, ReplacementRequest
        from serena.tools.file_tools import ReplaceContentTool

        if not self.agent.is_tool_function_available(ReplaceContentTool):
            raise ValueError("replace_contents requires replace_content to be enabled in the active context")
        requests = [ReplacementRequest.model_validate(item) for item in replacements]
        workspace = ProjectReplacementWorkspace(self.project, self.create_code_editor())
        report = BatchContentReplacer(workspace).replace(requests, dry_run, max_answer_chars)
        return json.dumps(report, ensure_ascii=False, separators=(",", ":"))
