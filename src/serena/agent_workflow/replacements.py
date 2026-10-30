# SPDX-License-Identifier: GPL-3.0-or-later

"""Preflighted heterogeneous content replacements using Serena's existing editor."""

import difflib
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from serena.util.text_utils import ContentReplacer
from serena.util.tool_call import ToolCallContext

if TYPE_CHECKING:
    from serena.code_editor import CodeEditor
    from serena.project import Project


class ReplacementRequest(BaseModel):
    """One replacement evaluated against earlier replacements to the same file."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1, max_length=1024)
    needle: str = Field(min_length=1, max_length=65536)
    repl: str = Field(max_length=262144)
    mode: Literal["literal", "regex"]
    allow_multiple_occurrences: bool = False
    expected_count: int = Field(default=-1, ge=-1, le=10000)
    expected_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReplacementWorkspace(Protocol):
    """Validated editor operations and cooperative cancellation for one project."""

    def check(self) -> None: ...
    def resolve(self, relative_path: str) -> str: ...
    def read(self, path: str) -> str: ...
    def validate_content(self, content: str) -> None: ...
    def write(self, path: str, original: str, updated: str) -> None: ...


class ProjectReplacementWorkspace:
    """Project policy and editor-buffer integration for batch replacements."""

    def __init__(self, project: "Project", editor: "CodeEditor"):
        self._project = project
        self._editor = editor

    def check(self) -> None:
        context = ToolCallContext.current()
        if context is not None:
            context.call.check()

    def resolve(self, relative_path: str) -> str:
        from serena.util.file_proxy import FileProxy

        if self._project.project_config.read_only:
            raise PermissionError("The project is read-only")
        if FileProxy.is_external_path(relative_path):
            raise ValueError("Encoded external editor paths cannot be modified")
        self._project.validate_relative_path(relative_path)
        path = (Path(self._project.project_root) / relative_path).resolve(strict=True)
        self._project.validate_relative_path(str(path))
        if not path.is_file():
            raise ValueError("Only existing regular files can be modified")
        if path.stat().st_size > BatchContentReplacer.MAX_FILE_CHARS * 4:
            raise ValueError("File exceeds the batch-edit size limit")
        return os.path.relpath(path, self._project.project_root).replace(os.sep, "/")

    def read(self, path: str) -> str:
        self.resolve(path)
        return self._editor.read_file(path)

    def validate_content(self, content: str) -> None:
        content.encode(self._editor.encoding)

    def write(self, path: str, original: str, updated: str) -> None:
        self.resolve(path)
        with self._editor.edited_file_context(path) as file:
            if file.get_contents() != original:
                raise ValueError("File changed after preflight; this file was not written")
            self.check()
            file.set_contents(updated)


@dataclass
class _FilePlan:
    path: str
    original: str
    updated: str
    indices: list[int] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    status: str = "not_applied"
    preview: str = ""

    def report(self) -> dict:
        return {
            "relative_path": self.path,
            "replacement_indices": self.indices,
            "match_counts": self.counts,
            "status": self.status,
            "original_content_sha256": BatchContentReplacer.digest(self.original),
            "planned_content_sha256": BatchContentReplacer.digest(self.updated),
            "preview": self.preview,
        }


class BatchReplacementError(ValueError):
    """Failed batch with its explicit per-file outcome attached."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


class BatchContentReplacer:
    """Plan all replacements before writing; never roll back or retry ambiguous writes."""

    MAX_FILE_CHARS = 1048576
    MAX_TOTAL_CHARS = 8388608

    def __init__(self, workspace: ReplacementWorkspace):
        self._workspace = workspace

    @staticmethod
    def digest(content: str) -> str:
        """SHA-256 of the editor's text encoded as UTF-8, not original filesystem bytes."""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _preview(plan: _FilePlan) -> str:
        pieces: list[str] = []
        remaining = 2048
        lines = difflib.unified_diff(
            plan.original.splitlines(keepends=True), plan.updated.splitlines(keepends=True),
            fromfile=plan.path, tofile=plan.path, n=2,
        )
        for line in lines:
            if len(line) > remaining:
                pieces.append(line[:remaining] + "\n[preview truncated]\n")
                break
            pieces.append(line)
            remaining -= len(line)
        return "".join(pieces)

    def _replace(self, text: str, item: ReplacementRequest) -> tuple[str, int]:
        # bound replacement expansion before allocating the updated file
        pattern = re.compile(re.escape(item.needle) if item.mode == "literal" else item.needle, re.MULTILINE | re.DOTALL)
        references = list(re.finditer(r"\$!(\d+)", item.repl))
        count = 0
        output_size = len(text)
        for match in pattern.finditer(text):
            self._workspace.check()
            count += 1
            if count > 10000:
                raise ValueError("More than 10000 matches in one replacement")
            expansion = len(item.repl)
            for reference in references:
                value = match.group(int(reference.group(1)))
                if value is not None:
                    expansion += len(value) - len(reference.group(0))
            output_size += expansion - len(match.group(0))
            if output_size > self.MAX_FILE_CHARS:
                raise ValueError("Replacement expansion exceeds the batch-edit size limit")
        if item.expected_count != -1 and count != item.expected_count:
            raise ValueError(f"Expected {item.expected_count} matches, found {count}")
        updated = ContentReplacer(item.mode, item.allow_multiple_occurrences).replace(text, item.needle, item.repl)
        return updated, count

    @staticmethod
    def _error(error: Exception) -> str:
        message = f"{type(error).__name__}: {str(error)[:160]}"
        while len(json.dumps(message, ensure_ascii=False)) > 512:
            message = message[:-1]
        return message

    @staticmethod
    def _report(plans: list[_FilePlan], status: str, limit: int, **details) -> dict:
        report = {"status": status, "atomic": False, "files": [plan.report() for plan in plans], **details}
        while len(json.dumps(report, ensure_ascii=False, separators=(",", ":"))) > limit:
            largest = max(report["files"], key=lambda row: len(row["preview"]), default=None)
            if largest is None or not largest["preview"]:
                raise ValueError("max_answer_chars is too small for batch outcome metadata")
            largest["preview"] = largest["preview"][: len(largest["preview"]) // 2]
            report["preview_truncated"] = True
        return report

    def replace(self, replacements: list[ReplacementRequest], dry_run: bool = False, max_answer_chars: int = 20000) -> dict:
        """Apply different replacements to explicit files after validating the entire batch.

        :param replacements: at most 50 replacements across at most 16 files
        :param dry_run: return bounded diffs and content hashes without writing
        :param max_answer_chars: complete serialized response allowance, 4096..200000
        :return: file outcomes; failed batches raise BatchReplacementError with the same report structure
        """
        if not 1 <= len(replacements) <= 50:
            raise ValueError("replacements must contain 1..50 entries")
        if isinstance(max_answer_chars, bool) or not isinstance(max_answer_chars, int) or not 4096 <= max_answer_chars <= 200000:
            raise ValueError("max_answer_chars must be between 4096 and 200000")
        if sum(len(item.needle) + len(item.repl) for item in replacements) > self.MAX_TOTAL_CHARS:
            raise ValueError("Replacement input exceeds the batch size limit")
        plans: dict[str, _FilePlan] = {}
        index = 0

        # stage ordered replacements without modifying any editor buffer
        try:
            for index, item in enumerate(replacements):
                self._workspace.check()
                path = self._workspace.resolve(item.relative_path)
                if path not in plans:
                    if len(plans) >= 16:
                        raise ValueError("A batch may affect at most 16 files")
                    original = self._workspace.read(path)
                    if len(original) > self.MAX_FILE_CHARS:
                        raise ValueError("File exceeds the batch-edit size limit")
                    plans[path] = _FilePlan(path, original, original)
                plan = plans[path]
                if item.expected_content_sha256 is not None and self.digest(plan.original) != item.expected_content_sha256:
                    raise ValueError("Original content hash does not match")
                plan.updated, count = self._replace(plan.updated, item)
                plan.indices.append(index)
                plan.counts.append(count)
                self._workspace.validate_content(plan.updated)
                if sum(len(p.original) + len(p.updated) for p in plans.values()) > self.MAX_TOTAL_CHARS:
                    raise ValueError("Combined file content exceeds the batch size limit")
            rows = list(plans.values())
            self._report(rows, "preflight", max_answer_chars - 1024)
            for plan in rows:
                self._workspace.check()
                if self._workspace.read(plan.path) != plan.original:
                    raise ValueError("A file changed during preflight")
        except Exception as error:
            report = self._report(list(plans.values()), "rejected", max_answer_chars, failed_replacement_index=index,
                                  error=self._error(error))
            raise BatchReplacementError(report) from error

        # preview without writes, or commit each file once under the existing project guard
        if dry_run:
            for plan in rows:
                plan.status = "preview" if plan.updated != plan.original else "unchanged"
                plan.preview = self._preview(plan)
            return self._report(rows, "dry_run", max_answer_chars)
        for plan in rows:
            try:
                self._workspace.check()
                if plan.updated == plan.original:
                    plan.status = "unchanged"
                    continue
                # an exception from the editor can occur after the filesystem write
                plan.status = "unknown"
                self._workspace.write(plan.path, plan.original, plan.updated)
                plan.status = "applied"
            except Exception as error:
                report = self._report(rows, "incomplete", max_answer_chars, error=self._error(error))
                raise BatchReplacementError(report) from error
        return self._report(rows, "applied", max_answer_chars)
