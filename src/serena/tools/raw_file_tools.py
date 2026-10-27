# SPDX-License-Identifier: GPL-3.0-or-later

from mcp.types import CallToolResult

from serena.util.raw_file import RawFileContent

from .tools_base import Tool


class ReadFileRawTool(Tool):
    """Read original file bytes, returning images as native multimodal MCP content."""

    def apply(self, relative_path: str, max_bytes: int = RawFileContent.DEFAULT_MAX_BYTES) -> CallToolResult:
        """
        Read a regular file as bytes, without text decoding or image transformations.
        PNG, JPEG, GIF, and WebP are returned as MCP image blocks; all other files are
        embedded binary resources. Display and model support depend on the MCP client.
        Prefer read_file for source code and other text. Oversized files fail, never truncate.

        :param relative_path: file path relative to the active project; paths or symlinks
            resolving outside it require the host-global full_access_mode setting.
        :param max_bytes: maximum file size in bytes; defaults to 5242880 (5 MiB), at most 20971520 (20 MiB).
        :return: the original file content and metadata including its MIME type, byte count, and SHA-256.
        """
        return RawFileContent.read(self.project, relative_path, max_bytes).to_mcp_result()
