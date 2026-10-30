# SPDX-License-Identifier: GPL-3.0-or-later

import json
from pathlib import Path

import pytest

from serena.agent_workflow.directories import DirectoryBatchReader, DirectoryRequest


class DirectoryTestProject:
    """Filesystem-backed test project with explicit outside-root and ignore policies."""

    def __init__(self, root: Path, full_access: bool = False):
        self.project_root = str(root)
        self.full_access = full_access

    def validate_relative_path(self, relative_path: str, require_not_ignored: bool = False) -> None:
        path = (Path(self.project_root) / relative_path).resolve()
        if not self.full_access and not path.is_relative_to(self.project_root):
            raise ValueError("outside project")

    def get_is_ignored_path_fn(self, base_path: str, skip_ignored_paths: bool):
        return lambda path: skip_ignored_paths and Path(path).name.startswith("ignored")


@pytest.fixture
def directory_project(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "one").mkdir()
    (root / "two").mkdir()
    (root / "one/a.txt").write_text("a")
    (root / "two/b.txt").write_text("b")
    return DirectoryTestProject(root)


def test_independent_directory_results(directory_project):
    reader = DirectoryBatchReader(directory_project)
    result = json.loads(reader.read([DirectoryRequest(relative_path=p) for p in ["one", "missing", "two"]]))
    assert result["results"][0]["files"] == ["one/a.txt"]
    assert "FileNotFoundError" in result["results"][1]["error"]
    assert result["results"][2]["files"] == ["two/b.txt"]


def test_directory_options_and_depth(directory_project):
    root = Path(directory_project.project_root)
    (root / "one/child").mkdir()
    (root / "one/child/deep.txt").write_text("deep")
    (root / "one/ignored.txt").touch()
    requests = [
        DirectoryRequest(relative_path="one"),
        DirectoryRequest(relative_path="one", recursive=True, max_depth=0),
        DirectoryRequest(relative_path="one", recursive=True, skip_ignored_files=False),
    ]
    rows = json.loads(DirectoryBatchReader(directory_project).read(requests))["results"]
    assert rows[0]["files"] == ["one/a.txt"]
    assert rows[0]["truncated"] is False
    assert rows[1]["reasons"] == ["depth_limit"]
    assert set(rows[2]["files"]) == {"one/a.txt", "one/ignored.txt", "one/child/deep.txt"}


def test_access_policy_and_symlinks(directory_project, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").touch()
    root = Path(directory_project.project_root)
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "loop").symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    reader = DirectoryBatchReader(directory_project)
    rows = json.loads(reader.read([DirectoryRequest(relative_path="escape"), DirectoryRequest(relative_path=".", recursive=True)]))[
        "results"
    ]
    assert rows[0]["error"] is not None
    assert set(rows[1]["symlinks"]) == {"escape", "loop"}
    assert all("secret" not in path for path in rows[1]["files"])
    allowed = DirectoryBatchReader(DirectoryTestProject(root, full_access=True))
    assert json.loads(allowed.read([DirectoryRequest(relative_path="escape")]))["results"][0]["files"] == ["../outside/secret.txt"]


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_entry_limit_is_explicit(directory_project, limit):
    root = Path(directory_project.project_root) / "one"
    for index in range(20):
        (root / f"file-{index}").touch()
    row = json.loads(DirectoryBatchReader(directory_project).read([DirectoryRequest(relative_path="one")], limit))["results"][0]
    assert len(row["files"]) <= limit
    assert row["reasons"] == ["entry_limit"]


def test_ignored_entries_are_also_bounded(directory_project):
    root = Path(directory_project.project_root) / "ignored-only"
    root.mkdir()
    for index in range(20):
        (root / f"ignored-{index}").touch()
    row = json.loads(DirectoryBatchReader(directory_project).read([DirectoryRequest(relative_path="ignored-only")], 5))["results"][0]
    assert not row["files"] and row["truncated"]


def test_serialized_output_bound(directory_project):
    root = Path(directory_project.project_root) / "one"
    for index in range(100):
        (root / (str(index) + '"' * 100)).touch()
    text = DirectoryBatchReader(directory_project).read(
        [DirectoryRequest(relative_path="one"), DirectoryRequest(relative_path="missing")], 200, 2048
    )
    assert len(text) <= 2048
    rows = json.loads(text)["results"]
    assert "response_limit" in rows[0]["reasons"]
    assert rows[1]["error"]


@pytest.mark.parametrize("count", [0, 9])
def test_batch_size_rejected(directory_project, count):
    with pytest.raises(ValueError):
        DirectoryBatchReader(directory_project).read([DirectoryRequest(relative_path="one")] * count)


@pytest.mark.parametrize("entries,chars", [(0, 20000), (5001, 20000), (True, 20000), (2, 1000), (2, 200001)])
def test_invalid_limits_rejected(directory_project, entries, chars):
    with pytest.raises(ValueError):
        DirectoryBatchReader(directory_project).read([DirectoryRequest(relative_path="one")], entries, chars)


def test_cancellation_is_not_reported_as_a_path_error(directory_project):
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise TimeoutError("cancelled")

    with pytest.raises(TimeoutError, match="cancelled"):
        DirectoryBatchReader(directory_project, check).read([DirectoryRequest(relative_path="one")])


def test_non_directory_is_an_independent_error(directory_project):
    rows = json.loads(
        DirectoryBatchReader(directory_project).read([DirectoryRequest(relative_path="one/a.txt"), DirectoryRequest(relative_path="two")])
    )["results"]
    assert "NotADirectoryError" in rows[0]["error"]
    assert rows[1]["files"] == ["two/b.txt"]


def test_escaped_errors_do_not_exceed_reserved_response_space(directory_project):
    class EscapedErrorProject(DirectoryTestProject):
        def validate_relative_path(self, relative_path: str, require_not_ignored: bool = False) -> None:
            if relative_path == "denied":
                raise PermissionError("\x01" * 200)
            super().validate_relative_path(relative_path, require_not_ignored)

    root = Path(directory_project.project_root)
    for index in range(100):
        (root / "one" / (str(index) + "x" * 80)).touch()
    requests = [DirectoryRequest(relative_path="one"), DirectoryRequest(relative_path="denied")]
    result = DirectoryBatchReader(EscapedErrorProject(root)).read(requests, max_total_chars=2048)
    assert len(result) <= 2048
    rows = json.loads(result)["results"]
    assert rows[0]["files"] and rows[1]["error"].startswith("PermissionError")
