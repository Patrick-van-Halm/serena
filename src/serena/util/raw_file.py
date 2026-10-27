# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded binary file reads, without text decoding or image transformations."""

import base64
import hashlib
import json
import mimetypes
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent

from serena.util.file_proxy import FileProxy

if TYPE_CHECKING:
    from serena.project import Project


@dataclass(frozen=True)
class RawFileContent:
    """Original file bytes and their source, convertible to native MCP content blocks."""

    path: Path
    data: bytes

    DEFAULT_MAX_BYTES = 5 * 1024 * 1024
    HARD_MAX_BYTES = 20 * 1024 * 1024

    @classmethod
    def read(cls, project: "Project", relative_path: str, max_bytes: int = DEFAULT_MAX_BYTES) -> "RawFileContent":
        """
        Read one regular file under the project's access policy, with a hard allocation limit.

        :param project: active project whose global access setting governs the read.
        :param relative_path: path relative to the project root, or an allowed absolute path.
        :param max_bytes: positive byte limit, at most 20 MiB; oversized reads fail without partial content.
        :return: the original bytes, with no decoding, newline conversion, resizing, or recompression.
        """
        # validate the limit and both the supplied path and its resolved symlink target
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= cls.HARD_MAX_BYTES:
            raise ValueError(f"max_bytes must be an integer between 1 and {cls.HARD_MAX_BYTES}")
        if FileProxy.is_external_path(relative_path):
            raise ValueError("Encoded JetBrains external paths are not supported by raw filesystem reads.")
        project.validate_relative_path(relative_path)
        path = (Path(project.project_root) / relative_path).resolve(strict=True)
        project.validate_relative_path(str(path))
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("read_file_raw requires a regular file, not a directory, device, socket, or pipe.")
        if before.st_size > max_bytes:
            raise ValueError(f"File exceeds max_bytes={max_bytes}; no content was read.")

        # avoid blocking on a replaced FIFO and reject final-component symlink swaps where supported
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("File changed during read_file_raw; retry after the file is stable.")
            if opened.st_size > max_bytes:
                raise ValueError(f"File exceeds max_bytes={max_bytes}; no content was read.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError(f"File exceeds max_bytes={max_bytes}; no partial content is returned.")
            after = os.fstat(descriptor)
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("File changed during read_file_raw; retry after the file is stable.")
        finally:
            os.close(descriptor)
        return cls(path=path, data=data)

    def _image_mime_type(self) -> str | None:
        # identify common inline image formats from bytes rather than trusting the filename
        if self.data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if self.data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if self.data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if self.data.startswith(b"RIFF") and self.data[8:12] == b"WEBP":
            return "image/webp"
        return None

    def to_mcp_result(self) -> CallToolResult:
        """Return image content or an embedded binary resource, alongside payload-free metadata."""
        # preserve bytes exactly; base64 is the MCP wire encoding, not a text answer
        image_mime = self._image_mime_type()
        guessed_mime, encoding = mimetypes.guess_type(str(self.path))
        mime_type = image_mime or (guessed_mime if encoding is None else None) or "application/octet-stream"
        metadata = TextContent(
            type="text",
            text=json.dumps(
                {
                    "name": self.path.name,
                    "mime_type": mime_type,
                    "size_bytes": len(self.data),
                    "sha256": hashlib.sha256(self.data).hexdigest(),
                }
            ),
        )
        encoded = base64.b64encode(self.data).decode("ascii")
        if image_mime is not None:
            return CallToolResult(content=[metadata, ImageContent(type="image", data=encoded, mimeType=image_mime)])
        resource = BlobResourceContents(uri=self.path.as_uri(), mimeType=mime_type, blob=encoded)
        return CallToolResult(content=[metadata, EmbeddedResource(type="resource", resource=resource)])
