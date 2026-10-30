# SPDX-License-Identifier: GPL-3.0-or-later

"""Observable contracts for bounded multi-file text retrieval."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from serena.agent_workflow.files import FileBatchReader, FileReadRequest
from serena.project import Project
from serena.tools.batch_file_tools import ReadFilesTool
from serena.util.tool_call import ToolCallInterrupted


@pytest.fixture
def batch_file_project(tmp_path: Path) -> Project:
    project = Project.__new__(Project)
    root = tmp_path / "project"
    root.mkdir()
    project.project_root = str(root)
    project.project_config = cast(Any, SimpleNamespace(encoding="utf-8"))
    project.serena_config = cast(Any, SimpleNamespace(full_access_mode=False))
    return project


def read(project: Project, files: list[dict], **kwargs) -> list[dict]:
    return json.loads(FileBatchReader(project).read([FileReadRequest.model_validate(item) for item in files], **kwargs))["results"]


def test_files_and_ranges_preserve_request_order_with_missing_sibling(batch_file_project: Project):
    root = Path(batch_file_project.project_root)
    (root / "one.txt").write_bytes(b"first\r\nsecond\rthird\n")
    (root / "two.txt").write_text("different", encoding="utf-8")
    rows = read(
        batch_file_project,
        [
            {"relative_path": "one.txt", "start_line": 1, "end_line": 2},
            {"relative_path": "missing.txt"},
            {"relative_path": "two.txt"},
            {"relative_path": "one.txt", "start_line": -2},
        ],
    )
    assert [r["request_index"] for r in rows] == [0, 1, 2, 3]
    assert rows[0]["content"] == "second\nthird"
    assert rows[1]["error"].startswith("FileNotFoundError:")
    assert rows[2]["content"] == "different"
    assert rows[3]["content"] == "third\n"
    assert rows[0]["file_sha256"] == hashlib.sha256((root / "one.txt").read_bytes()).hexdigest()
    assert rows[0]["text_sha256"] == hashlib.sha256(b"first\nsecond\nthird\n").hexdigest()


@pytest.mark.parametrize(
    ("text", "start", "end", "expected"),
    [("", 0, None, ""), ("a\n", 1, 1, ""), ("a\nb\nc", 1, 1, "b"), ("a", 9, None, ""), ("a\u2028b", 0, 0, "a\u2028b")],
)
def test_line_semantics(batch_file_project: Project, text: str, start: int, end: int | None, expected: str):
    (Path(batch_file_project.project_root) / "file.txt").write_text(text, encoding="utf-8")
    row = read(batch_file_project, [{"relative_path": "file.txt", "start_line": start, "end_line": end}])[0]
    assert row["content"] == expected
    assert row["selected_chars"] == len(expected)
    assert not row["truncated"] and row["error"] is None


def test_project_encoding_is_used_before_normalizing_newlines(batch_file_project: Project):
    batch_file_project.project_config.encoding = "utf-16"
    (Path(batch_file_project.project_root) / "file.txt").write_bytes("café\r\nnext\r".encode("utf-16"))
    row = read(batch_file_project, [{"relative_path": "file.txt"}])[0]
    assert row["content"] == "café\nnext\n"
    assert row["text_sha256"] == hashlib.sha256("café\nnext\n".encode()).hexdigest()


@pytest.mark.parametrize("data", [b"invalid\xff", b"binary\x00data"])
def test_binary_or_decode_failure_does_not_discard_other_files(batch_file_project: Project, data: bytes):
    root = Path(batch_file_project.project_root)
    (root / "bad.bin").write_bytes(data)
    (root / "good.txt").write_text("good", encoding="utf-8")
    rows = read(batch_file_project, [{"relative_path": "bad.bin"}, {"relative_path": "good.txt"}])
    assert rows[0]["error"] and rows[0]["content"] == ""
    assert rows[1]["content"] == "good"


def test_per_file_truncation_keeps_full_snapshot_hashes(batch_file_project: Project):
    (Path(batch_file_project.project_root) / "file.txt").write_text("0123456789", encoding="utf-8")
    row = read(batch_file_project, [{"relative_path": "file.txt"}], max_chars_per_file=4)[0]
    assert row["content"] == "0123" and row["selected_chars"] == 10
    assert row["truncated"] and row["reasons"] == ["per_file_limit"]
    assert row["text_sha256"] == hashlib.sha256(b"0123456789").hexdigest()


def test_total_limit_counts_escaped_json_and_gives_each_file_content(batch_file_project: Project):
    root = Path(batch_file_project.project_root)
    content = '"\\\t\n😀' * 2000
    for name in ["one.txt", "two.txt"]:
        (root / name).write_text(content, encoding="utf-8")
    text = FileBatchReader(batch_file_project).read(
        [FileReadRequest(relative_path=name) for name in ["one.txt", "two.txt"]], max_total_chars=2048
    )
    assert len(text) <= 2048
    for row in json.loads(text)["results"]:
        assert row["content"] and content.startswith(row["content"])
        assert row["truncated"] and "total_limit" in row["reasons"]


def test_byte_bound_applies_to_whole_file_not_only_range(batch_file_project: Project):
    root = Path(batch_file_project.project_root)
    (root / "large.txt").write_bytes(b"ok\n" + b"x" * 100)
    (root / "small.txt").write_bytes(b"ok")
    rows = read(
        batch_file_project,
        [{"relative_path": "large.txt", "end_line": 0}, {"relative_path": "small.txt"}],
        max_bytes_per_file=2,
    )
    assert "max_bytes" in rows[0]["error"]
    assert rows[1]["content"] == "ok"


def test_outside_and_symlink_targets_require_global_opt_in(batch_file_project: Project, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = Path(batch_file_project.project_root) / "outside-link"
    link.symlink_to(outside)
    paths = [{"relative_path": "../outside.txt"}, {"relative_path": "outside-link"}]
    rows = read(batch_file_project, paths)
    assert all(row["error"] and row["content"] == "" for row in rows)
    batch_file_project.serena_config.full_access_mode = True
    assert [row["content"] for row in read(batch_file_project, paths)] == ["private", "private"]


def test_directory_and_fifo_do_not_block_sibling_reads(batch_file_project: Project):
    root = Path(batch_file_project.project_root)
    (root / "good.txt").write_text("good", encoding="utf-8")
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable")
    os.mkfifo(root / "pipe")
    rows = read(batch_file_project, [{"relative_path": "."}, {"relative_path": "pipe"}, {"relative_path": "good.txt"}])
    assert all("regular file" in row["error"] for row in rows[:2])
    assert rows[2]["content"] == "good"


def test_encoded_external_path_returns_error(batch_file_project: Project):
    row = read(batch_file_project, [{"relative_path": "<ext:File.class|abcdef>"}])[0]
    assert row["error"] and row["content"] == ""


@pytest.mark.parametrize(
    "item",
    [
        {"relative_path": ""},
        {"relative_path": "a", "start_line": True},
        {"relative_path": "a", "end_line": -1},
        {"relative_path": "a", "typo": 1},
    ],
)
def test_invalid_file_requests_are_rejected(item: dict):
    with pytest.raises(ValidationError):
        FileReadRequest.model_validate(item)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_total_chars": 2047},
        {"max_total_chars": 200001},
        {"max_chars_per_file": 0},
        {"max_bytes_per_file": True},
        {"max_bytes_per_file": 20971521},
    ],
)
def test_invalid_limits_are_rejected(batch_file_project: Project, limits: dict):
    with pytest.raises(ValueError, match="must be an integer"):
        read(batch_file_project, [{"relative_path": "missing"}], **limits)


@pytest.mark.parametrize("count", [0, 9])
def test_batch_cardinality_is_bounded(batch_file_project: Project, count: int):
    with pytest.raises(ValueError, match="between 1 and 8"):
        read(batch_file_project, [{"relative_path": "missing"}] * count)


def test_path_metadata_must_fit_without_dropping_items(batch_file_project: Project):
    with pytest.raises(ValueError, match="per-file metadata"):
        read(batch_file_project, [{"relative_path": "x" * 4096}], max_total_chars=2048)


def test_cancellation_propagates_instead_of_becoming_a_file_error(batch_file_project: Project):
    def cancel():
        raise ToolCallInterrupted("cancelled")

    with pytest.raises(ToolCallInterrupted):
        FileBatchReader(batch_file_project, cancel).read([FileReadRequest(relative_path="missing")])


def test_tool_refuses_to_bypass_read_file_exclusion(batch_file_project: Project):
    agent = MagicMock()
    agent.get_active_project_or_raise.return_value = batch_file_project
    agent.is_tool_function_available.return_value = False
    with pytest.raises(ValueError, match="read_file to be enabled"):
        ReadFilesTool(agent).apply([{"relative_path": "missing"}])
    assert ReadFilesTool(agent).is_readonly()
