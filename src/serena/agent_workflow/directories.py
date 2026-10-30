# SPDX-License-Identifier: GPL-3.0-or-later

"""Bounded multi-directory retrieval under the active project's access policy."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class DirectoryRequest(BaseModel):
    """One directory and its traversal options."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str = Field(min_length=1, max_length=4096)
    recursive: bool = False
    skip_ignored_files: bool = True
    max_depth: int = Field(default=4, ge=0, le=32)


class DirectoryProject(Protocol):
    """Project access validation and ignore rules used during traversal."""

    @property
    def project_root(self) -> str: ...

    def validate_relative_path(self, relative_path: str, require_not_ignored: bool = False) -> None: ...

    def get_is_ignored_path_fn(self, base_path: str, skip_ignored_paths: bool) -> Callable[[str], bool]: ...


@dataclass
class DirectoryListing:
    """One listing, including independent failure and incompleteness information."""

    request_index: int
    relative_path: str
    dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symlinks: list[str] = field(default_factory=list)
    truncated: bool = False
    reasons: list[str] = field(default_factory=list)
    error: str | None = None

    def truncate(self, reason: str) -> None:
        """Record a work or response boundary without implying complete enumeration."""
        self.truncated = True
        if reason not in self.reasons:
            self.reasons.append(reason)

    def to_dict(self) -> dict:
        """Return wire metadata without copying the collected path arrays."""
        return {
            "request_index": self.request_index,
            "relative_path": self.relative_path,
            "dirs": self.dirs,
            "files": self.files,
            "symlinks": self.symlinks,
            "truncated": self.truncated,
            "reasons": self.reasons,
            "error": self.error,
        }


@dataclass
class _OutputBudget:
    used: int
    limit: int

    def admit(self, path: str, has_entries: bool) -> bool:
        size = len(json.dumps(path, ensure_ascii=False)) + int(has_entries)
        if self.used + size > self.limit:
            return False
        self.used += size
        return True


class DirectoryBatchReader:
    """Retrieve small independent listings with bounded work and serialized output."""

    MAX_DIRECTORIES = 8
    MAX_ENTRIES_PER_DIRECTORY = 5000

    def __init__(self, project: DirectoryProject, check_cancelled: Callable[[], None] | None = None):
        self._project = project
        self._check_cancelled = check_cancelled or (lambda: None)

    @staticmethod
    def _encode(results: list[DirectoryListing]) -> str:
        return json.dumps({"results": [result.to_dict() for result in results]}, ensure_ascii=False, separators=(",", ":"))

    def read(self, directories: list[DirectoryRequest], max_entries_per_dir: int = 200, max_total_chars: int = 20000) -> str:
        """List directories serially, retaining successful siblings when one path fails.

        Discovered symlinks are reported but never traversed. Entry limits count
        inspected entries, including ignored entries, so ignored trees cannot cause
        unbounded scanning. Reaching a limit is conservative: the uninspected
        remainder may be empty. Filesystem operations can still be slow or blocked.

        :param directories: at most eight independent directory requests
        :param max_entries_per_dir: maximum entries inspected per requested directory
        :param max_total_chars: maximum characters in the complete serialized JSON response
        :return: independent listings with explicit errors and truncation reasons
        """
        # validate limits and reserve room for bounded per-item diagnostics
        if not 1 <= len(directories) <= self.MAX_DIRECTORIES:
            raise ValueError("directories must contain between 1 and 8 requests")
        if isinstance(max_entries_per_dir, bool) or not isinstance(max_entries_per_dir, int):
            raise ValueError("max_entries_per_dir must be an integer")
        if not 1 <= max_entries_per_dir <= self.MAX_ENTRIES_PER_DIRECTORY:
            raise ValueError("max_entries_per_dir must be between 1 and 5000")
        if isinstance(max_total_chars, bool) or not isinstance(max_total_chars, int) or not 2048 <= max_total_chars <= 200000:
            raise ValueError("max_total_chars must be an integer between 2048 and 200000")
        results = [DirectoryListing(index, item.relative_path) for index, item in enumerate(directories)]
        reserve = 256 * len(results)
        content_limit = max_total_chars - reserve
        budget = _OutputBudget(len(self._encode(results)), content_limit)
        if budget.used > content_limit:
            raise ValueError("max_total_chars cannot hold the requested paths and per-directory metadata")

        # visit each root independently without swallowing cancellation or deadline errors
        for item, result in zip(directories, results, strict=True):
            self._check_cancelled()
            try:
                self._read_one(item, result, max_entries_per_dir, budget)
            except TimeoutError:
                raise
            except (OSError, ValueError) as error:
                # reserve by serialized length, including escaping in filesystem errors
                message = f"{type(error).__name__}: {str(error)[:100]}"
                while len(json.dumps(message, ensure_ascii=False)) > 128:
                    message = message[:-1]
                result.error = message
                result.truncate("access_error")
            result.dirs.sort()
            result.files.sort()
            result.symlinks.sort()

        rendered = self._encode(results)
        if len(rendered) > max_total_chars:
            raise RuntimeError("Directory response exceeded its reserved metadata allowance")
        return rendered

    def _read_one(
        self,
        item: DirectoryRequest,
        result: DirectoryListing,
        entry_limit: int,
        budget: _OutputBudget,
    ) -> None:
        # validate both the requested path and the resolved root before opening it
        self._project.validate_relative_path(item.relative_path)
        root = (Path(self._project.project_root) / item.relative_path).resolve(strict=True)
        self._project.validate_relative_path(str(root))
        if not root.is_dir():
            raise NotADirectoryError(item.relative_path)
        ignored = self._project.get_is_ignored_path_fn(item.relative_path, item.skip_ignored_files)
        pending = [(root, 0)]
        inspected = 0

        # bound inspection as well as output; only one directory descriptor remains open
        while pending:
            self._check_cancelled()
            directory, depth = pending.pop()
            self._project.validate_relative_path(str(directory.resolve(strict=True)))
            with os.scandir(directory) as entries:
                for entry in entries:
                    self._check_cancelled()
                    if inspected >= entry_limit:
                        result.truncate("entry_limit")
                        return
                    inspected += 1
                    if ignored(entry.path):
                        continue
                    relative = os.path.relpath(entry.path, self._project.project_root).replace(os.sep, "/")
                    is_link = entry.is_symlink()
                    is_directory = not is_link and entry.is_dir(follow_symlinks=False)
                    target = result.symlinks if is_link else result.dirs if is_directory else result.files
                    if not budget.admit(relative, bool(target)):
                        result.truncate("response_limit")
                        return
                    target.append(relative)
                    if item.recursive and is_directory:
                        if depth < item.max_depth:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            result.truncate("depth_limit")
