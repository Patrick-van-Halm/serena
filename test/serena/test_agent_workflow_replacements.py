# SPDX-License-Identifier: GPL-3.0-or-later

import json

import pytest

from serena.agent_workflow.replacements import BatchContentReplacer, BatchReplacementError, ReplacementRequest


class MemoryWorkspace:
    """Editor contract with observable file content and injectable external failures."""

    def __init__(self):
        self.files = {"a.txt": "alpha one\n", "b.txt": "beta two\n"}
        self.writes = []
        self.fail_on = None
        self.cancel_after_write = False

    def check(self):
        if self.cancel_after_write and self.writes:
            raise TimeoutError("cancelled")

    def resolve(self, path):
        if path.startswith("../"):
            raise PermissionError("outside project")
        if path not in self.files:
            raise FileNotFoundError(path)
        return path

    def read(self, path):
        return self.files[path]

    def validate_content(self, content):
        content.encode("ascii")

    def write(self, path, original, updated):
        if path == self.fail_on:
            raise OSError("injected save failure")
        assert self.files[path] == original
        self.files[path] = updated
        self.writes.append(path)


def replacement(path="a.txt", needle="alpha", repl="changed", **kwargs):
    return ReplacementRequest(relative_path=path, needle=needle, repl=repl, mode=kwargs.pop("mode", "literal"), **kwargs)


def test_heterogeneous_edits_are_staged_in_order_and_saved_once():
    workspace = MemoryWorkspace()
    entries = [
        replacement(),
        replacement(needle="changed", repl="final"),
        replacement("b.txt", r"(beta) (two)", "$!2 $!1", mode="regex"),
    ]
    report = BatchContentReplacer(workspace).replace(entries)
    assert report["status"] == "applied"
    assert workspace.files == {"a.txt": "final one\n", "b.txt": "two beta\n"}
    assert workspace.writes == ["a.txt", "b.txt"]
    assert report["files"][0]["match_counts"] == [1, 1]


@pytest.mark.parametrize(
    "bad",
    [
        replacement("missing.txt"),
        replacement(needle="absent"),
        replacement(needle="(", mode="regex"),
        replacement(expected_content_sha256="0" * 64),
        replacement(expected_count=2),
        replacement("../outside"),
        replacement(repl="\u2603"),
    ],
)
def test_any_preflight_error_prevents_all_writes(bad):
    workspace = MemoryWorkspace()
    with pytest.raises(BatchReplacementError) as error:
        BatchContentReplacer(workspace).replace([replacement("b.txt", "beta", "edited"), bad])
    assert error.value.report["status"] == "rejected"
    assert workspace.files == {"a.txt": "alpha one\n", "b.txt": "beta two\n"}
    assert workspace.writes == []


def test_dry_run_and_original_hash_guards():
    workspace = MemoryWorkspace()
    service = BatchContentReplacer(workspace)
    preview = service.replace([replacement()], dry_run=True)
    assert workspace.writes == [] and "-alpha" in preview["files"][0]["preview"]
    digest = preview["files"][0]["original_content_sha256"]
    entries = [
        replacement(expected_content_sha256=digest),
        replacement(needle="changed", repl="final", expected_content_sha256=digest),
    ]
    report = service.replace(entries)
    assert report["files"][0]["status"] == "applied"


def test_multiple_matches_require_explicit_permission():
    workspace = MemoryWorkspace()
    workspace.files["a.txt"] = "alpha alpha"
    with pytest.raises(BatchReplacementError):
        BatchContentReplacer(workspace).replace([replacement()])
    report = BatchContentReplacer(workspace).replace([replacement(allow_multiple_occurrences=True, expected_count=2)])
    assert workspace.files["a.txt"] == "changed changed"
    assert report["files"][0]["match_counts"] == [2]


def test_mid_write_failure_reports_unknown_without_rollback():
    workspace = MemoryWorkspace()
    workspace.files["c.txt"] = "third"
    workspace.fail_on = "b.txt"
    entries = [replacement(), replacement("b.txt", "beta", "edited"), replacement("c.txt", "third", "last")]
    with pytest.raises(BatchReplacementError) as error:
        BatchContentReplacer(workspace).replace(entries)
    assert [row["status"] for row in error.value.report["files"]] == ["applied", "unknown", "not_applied"]
    assert workspace.files["a.txt"] == "changed one\n" and workspace.files["c.txt"] == "third"


def test_cancellation_does_not_start_the_next_write():
    workspace = MemoryWorkspace()
    workspace.cancel_after_write = True
    with pytest.raises(BatchReplacementError) as error:
        BatchContentReplacer(workspace).replace([replacement(), replacement("b.txt", "beta", "edited")])
    assert [row["status"] for row in error.value.report["files"]] == ["applied", "not_applied"]
    assert workspace.writes == ["a.txt"]


def test_external_change_during_preflight_prevents_writes():
    class ChangingWorkspace(MemoryWorkspace):
        def read(self, path):
            original = super().read(path)
            self.files["a.txt"] = "external"
            return original

    workspace = ChangingWorkspace()
    with pytest.raises(BatchReplacementError):
        BatchContentReplacer(workspace).replace([replacement()])
    assert not workspace.writes and workspace.files["a.txt"] == "external"


def test_preview_is_bounded_valid_json():
    workspace = MemoryWorkspace()
    workspace.files["a.txt"] = "before\n" * 2000
    entries = [replacement(needle="before", repl="after", allow_multiple_occurrences=True)]
    report = BatchContentReplacer(workspace).replace(entries, dry_run=True, max_answer_chars=4096)
    assert len(json.dumps(report, ensure_ascii=False, separators=(",", ":"))) <= 4096
    assert not workspace.writes


def test_expansion_is_rejected_before_allocation_or_writes():
    workspace = MemoryWorkspace()
    workspace.files["a.txt"] = "x" * 10000
    with pytest.raises(BatchReplacementError):
        BatchContentReplacer(workspace).replace([replacement(needle="x", repl="y" * 1000, allow_multiple_occurrences=True)])
    assert workspace.writes == []


def test_noop_does_not_write():
    workspace = MemoryWorkspace()
    report = BatchContentReplacer(workspace).replace([replacement(repl="alpha")])
    assert report["files"][0]["status"] == "unchanged" and not workspace.writes
