# SPDX-License-Identifier: GPL-3.0-or-later
"""Lossless transport and payload-free diagnostics for text and multimodal tool results."""

from mcp.types import CallToolResult, TextContent

ToolResult = str | CallToolResult


class ToolResultCodec:
    """Preserve native MCP content across the daemon's HTTP boundary."""

    MEDIA_TYPE = "application/vnd.serena.mcp-tool-result+json"

    @classmethod
    def encode(cls, result: ToolResult) -> tuple[str, str]:
        """:return: response body and media type, leaving existing text responses unchanged."""
        if isinstance(result, CallToolResult):
            return result.model_dump_json(by_alias=True, exclude_none=True), cls.MEDIA_TYPE
        return result, "text/plain"

    @classmethod
    def decode(cls, body: str, content_type: str) -> ToolResult:
        """Decode only explicitly typed responses; ordinary JSON-looking text remains text."""
        if content_type.split(";", 1)[0].strip().lower() == cls.MEDIA_TYPE:
            return CallToolResult.model_validate_json(body)
        return body

    @staticmethod
    def summary(result: ToolResult) -> str:
        """Summarize binary results for logs and usage statistics without exposing their payloads."""
        if isinstance(result, str):
            return result
        parts = [block.text if isinstance(block, TextContent) else f"[{block.type} content omitted]" for block in result.content]
        return "\n".join(parts)
