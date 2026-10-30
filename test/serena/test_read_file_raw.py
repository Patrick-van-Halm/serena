# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from mcp_types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent

from serena.project import Project
from serena.tools.raw_file_tools import ReadFileRawTool
from serena.util.raw_file import RawFileContent
from serena.util.tool_result import ToolResultCodec

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMXsAAAAASUVORK5CYII=")


@pytest.fixture
def raw_project(tmp_path: Path) -> Project:
    project = Project.__new__(Project)
    project.project_root = str(tmp_path)
    project.serena_config = cast(Any, SimpleNamespace(full_access_mode=False))
    return project


def test_png_bytes_are_native_image_content(raw_project: Project) -> None:
    path = Path(raw_project.project_root) / "screenshot.bin"
    path.write_bytes(PNG)
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = raw_project
    result = ReadFileRawTool(agent).apply("screenshot.bin")

    assert isinstance(result, CallToolResult)
    assert not result.is_error
    image = result.content[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data, validate=True) == PNG
    assert isinstance(result.content[0], TextContent)
    metadata = json.loads(result.content[0].text)
    assert metadata["size_bytes"] == len(PNG)
    assert metadata["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert not ReadFileRawTool.can_edit()


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"\xff\xd8\xff\xe0jpeg", "image/jpeg"),
        (b"GIF87a1234", "image/gif"),
        (b"GIF89a1234", "image/gif"),
        (b"RIFF\x04\0\0\0WEBP", "image/webp"),
    ],
)
def test_image_signature_selects_mime_without_reencoding(raw_project: Project, data: bytes, mime: str) -> None:
    path = Path(raw_project.project_root) / "wrong-extension.txt"
    path.write_bytes(data)
    image = RawFileContent.read(raw_project, path.name).to_mcp_result().content[1]
    assert isinstance(image, ImageContent)
    assert image.mime_type == mime
    assert base64.b64decode(image.data) == data


@pytest.mark.parametrize("data", [b"", b"\x00\xff\xfe\r\n\r\x80", bytes(range(256))])
def test_arbitrary_bytes_are_embedded_without_text_decoding(raw_project: Project, data: bytes) -> None:
    path = Path(raw_project.project_root) / "data.bin"
    path.write_bytes(data)
    result = RawFileContent.read(raw_project, path.name).to_mcp_result()
    resource = result.content[1]
    assert isinstance(resource, EmbeddedResource)
    assert isinstance(resource.resource, BlobResourceContents)
    assert resource.resource.mime_type == "application/octet-stream"
    assert str(resource.resource.uri) == path.as_uri()
    assert base64.b64decode(resource.resource.blob, validate=True) == data


@pytest.mark.parametrize("name", ["fake.png", "vector.svg", "document.pdf"])
def test_other_formats_are_binary_resources_not_inline_images(raw_project: Project, name: str) -> None:
    path = Path(raw_project.project_root) / name
    path.write_bytes(b"not an inline image")
    resource = RawFileContent.read(raw_project, name).to_mcp_result().content[1]
    assert isinstance(resource, EmbeddedResource)
    assert isinstance(resource.resource, BlobResourceContents)
    assert base64.b64decode(resource.resource.blob) == path.read_bytes()


def test_byte_limit_is_exact_and_does_not_return_partial_data(raw_project: Project) -> None:
    path = Path(raw_project.project_root) / "data.bin"
    path.write_bytes(b"1234")
    assert RawFileContent.read(raw_project, path.name, max_bytes=4).data == b"1234"
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        RawFileContent.read(raw_project, path.name, max_bytes=3)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10", RawFileContent.HARD_MAX_BYTES + 1])
def test_invalid_limits_fail_before_file_access(raw_project: Project, limit: Any) -> None:
    with pytest.raises(ValueError, match="max_bytes must be an integer"):
        RawFileContent.read(raw_project, "missing.bin", max_bytes=limit)


def test_outside_path_requires_global_opt_in(raw_project: Project, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    raw_project.project_root = str(root)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    for name in ("../outside.bin", str(outside)):
        with pytest.raises(ValueError, match="outside the project"):
            RawFileContent.read(raw_project, name)
    raw_project.serena_config.full_access_mode = True
    assert RawFileContent.read(raw_project, "../outside.bin").data == b"outside"


def test_symlink_target_obeys_access_boundary(raw_project: Project, tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    raw_project.project_root = str(root)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = root / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(ValueError, match="outside the project"):
        RawFileContent.read(raw_project, "link.bin")
    raw_project.serena_config.full_access_mode = True
    assert RawFileContent.read(raw_project, "link.bin").data == b"outside"


def test_directory_and_missing_file_fail(raw_project: Project) -> None:
    with pytest.raises(ValueError, match="regular file"):
        RawFileContent.read(raw_project, ".")
    with pytest.raises(FileNotFoundError):
        RawFileContent.read(raw_project, "missing.bin")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Named pipes require POSIX")
def test_pipe_is_rejected_without_waiting_for_a_writer(raw_project: Project) -> None:
    pipe = Path(raw_project.project_root) / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(ValueError, match="regular file"):
        RawFileContent.read(raw_project, "pipe")


def test_logs_and_usage_summary_do_not_contain_binary_payload(raw_project: Project) -> None:
    path = Path(raw_project.project_root) / "image.png"
    path.write_bytes(PNG)
    result = RawFileContent.read(raw_project, path.name).to_mcp_result()
    summary = ToolResultCodec.summary(result)
    assert '"size_bytes"' in summary
    assert "[image content omitted]" in summary
    assert base64.b64encode(PNG).decode() not in summary


@pytest.mark.parametrize("binary", [True, False])
def test_http_codec_preserves_content_type_and_bytes(raw_project: Project, binary: bool) -> None:
    path = Path(raw_project.project_root) / "data.bin"
    path.write_bytes(PNG if binary else b"\xff\0\r\n")
    original = RawFileContent.read(raw_project, path.name).to_mcp_result()
    body, content_type = ToolResultCodec.encode(original)
    decoded = ToolResultCodec.decode(body, content_type + "; charset=utf-8")
    assert isinstance(decoded, CallToolResult)
    assert decoded.model_dump() == original.model_dump()


@pytest.mark.parametrize("text", ["", "ordinary text", '{"content":[{"type":"image","data":"AAAA","mimeType":"image/png"}]}'])
def test_text_responses_are_not_reinterpreted(text: str) -> None:
    body, content_type = ToolResultCodec.encode(text)
    assert content_type == "text/plain"
    assert ToolResultCodec.decode(body, content_type) == text
    assert ToolResultCodec.decode(body, "application/json") == text


def test_corrupt_typed_response_fails_instead_of_becoming_text() -> None:
    with pytest.raises(ValueError):
        ToolResultCodec.decode("not JSON", ToolResultCodec.MEDIA_TYPE)


def test_encoded_external_path_is_rejected(raw_project: Project) -> None:
    with pytest.raises(ValueError, match="Encoded JetBrains external"):
        RawFileContent.read(raw_project, "<ext:FileUtil.class|472e0a13>")


def test_internal_symlink_preserves_original_bytes(raw_project: Project) -> None:
    root = Path(raw_project.project_root)
    (root / "actual.bin").write_bytes(PNG)
    try:
        (root / "link.png").symlink_to(root / "actual.bin")
    except OSError:
        pytest.skip("Symlinks are unavailable")
    assert RawFileContent.read(raw_project, "link.png").data == PNG


def test_growth_between_stat_and_open_cannot_bypass_size_limit(raw_project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(raw_project.project_root) / "growing.bin"
    path.write_bytes(b"1234")
    original_open = os.open

    def open_after_growth(*args: Any, **kwargs: Any) -> int:
        path.write_bytes(b"12345")
        return original_open(*args, **kwargs)

    monkeypatch.setattr(os, "open", open_after_growth)
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        RawFileContent.read(raw_project, path.name, max_bytes=4)


def test_http_codec_preserves_native_metadata_aliases() -> None:
    native = CallToolResult.model_validate(
        {
            "content": [{"type": "image", "data": "AAAA", "mimeType": "image/png", "_meta": {"source": "test"}}],
            "_meta": {"request": "test-request"},
        }
    )
    body, content_type = ToolResultCodec.encode(native)
    decoded = ToolResultCodec.decode(body, content_type)
    assert isinstance(decoded, CallToolResult)
    assert decoded.model_dump(by_alias=True) == native.model_dump(by_alias=True)
