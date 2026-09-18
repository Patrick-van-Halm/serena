# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Self

from serena.jetbrains import jetbrains_types as jb
from solidlsp.ls_utils import TextUtils

if TYPE_CHECKING:
    from serena.project import Project

log = logging.getLogger(__name__)


class FileProxy(ABC):
    @abstractmethod
    def get_contents(self) -> str:
        """:return: the contents of the file as a string."""

    def get_lines(self, start_line: int = 0, end_line: int | None = None) -> list[str]:
        """Return a slice of the file's LSP lines.

        The default implementation materialises the contents and is used for proxies whose
        backing store is not a local file (for example JetBrains external paths).
        """
        lines = TextUtils.split_lines(self.get_contents())
        return lines[start_line:] if end_line is None else lines[start_line : end_line + 1]

    @abstractmethod
    def get_relative_path(self) -> str:
        """:return: the relative path reported by Serena (actual relative path or encoded external path)"""

    @abstractmethod
    def is_glob_supported(self):
        """
        :return: whether the proxy supports glob filtering based on its relative path
        """

    @staticmethod
    def is_external_path(relative_path: str) -> bool:
        """
        :return: whether the given relative path is an encoded external path (not a local project file)
        """
        # This is intended to be extended once we also support external paths in other backends
        return jb.is_external_path(relative_path)

    @classmethod
    def from_project_relative_path(cls, project: "Project", relative_path: str) -> "FileProxy":
        if cls.is_external_path(relative_path):
            if project.language_backend.is_jetbrains():
                return JetBrainsFileProxy(relative_path, project)
        return LocalProjectFileProxy(relative_path, project)


class LocalProjectFileProxy(FileProxy):
    def __init__(self, relative_path: str, project: "Project"):
        self._relative_path = relative_path
        self._project = project

    def get_contents(self) -> str:
        abs_path = os.path.join(self._project.project_root, self._relative_path)
        with open(abs_path, encoding=self._project.project_config.encoding) as f:
            return f.read()

    def get_lines(self, start_line: int = 0, end_line: int | None = None) -> list[str]:
        # A bounded, non-negative range is the common agent read pattern. Stream only as
        # far as the requested end line instead of materialising and splitting the whole
        # file. Full reads and negative slicing still use the C-accelerated split path.
        if start_line < 0 or end_line is None or end_line < 0:
            return super().get_lines(start_line, end_line)
        if end_line < start_line:
            return []

        abs_path = os.path.join(self._project.project_root, self._relative_path)
        result: list[str] = []
        line_no = 0
        last_had_newline = False
        reached_eof = True
        with open(abs_path, encoding=self._project.project_config.encoding) as f:
            for raw_line in f:
                # Text-mode universal-newline handling normalises LF, CRLF and bare CR to
                # LF, exactly matching the LSP line-break set used by TextUtils.
                last_had_newline = raw_line.endswith("\n")
                if line_no >= start_line:
                    result.append(raw_line[:-1] if last_had_newline else raw_line)
                line_no += 1
                if line_no > end_line:
                    reached_eof = False
                    break

        if reached_eof:
            # TextUtils.split_lines("") == [""] and a trailing line break creates one
            # final empty logical line. Preserve both behaviours for bounded reads.
            if line_no == 0:
                if start_line == 0 <= end_line:
                    result.append("")
            elif last_had_newline and start_line <= line_no <= end_line:
                result.append("")
        return result

    def get_relative_path(self) -> str:
        return self._relative_path

    def is_glob_supported(self):
        return True


class JetBrainsFileProxy(FileProxy):
    """
    Retrieves the contents of a file from the JetBrains plugin via the plugin client, given its relative path,
    which may be an external path (e.g., "<ext:FileUtil.class|472e0a13>")
    """

    def __init__(self, relative_path: str, project: "Project"):
        self._relative_path = relative_path
        self._project = project

    def get_contents(self) -> str:
        from serena.jetbrains.jetbrains_plugin_client import JetBrainsPluginClient

        client = JetBrainsPluginClient.from_project(self._project)
        return client.read_file(self._relative_path)

    def get_relative_path(self) -> str:
        return self._relative_path

    def is_glob_supported(self):
        return False


class FileCollection:
    def __init__(self, file_proxies: list[FileProxy]):
        self._file_proxies = file_proxies

    def __len__(self) -> int:
        return len(self._file_proxies)

    def __iter__(self) -> Iterator[FileProxy]:
        return iter(self._file_proxies)

    @classmethod
    def from_local_project_paths(cls, relative_paths: list[str], project: "Project") -> Self:
        return cls([LocalProjectFileProxy(path, project) for path in relative_paths])

    def filter_glob(self, paths_include_glob: str | None = None, paths_exclude_glob: str | None = None) -> "FileCollection":
        """
        Filters the collection based on the given patterns.
        Note: Filtering is applied only to local project files. Other files are always retained.

        :param paths_include_glob: optional glob pattern to include files from the list
        :param paths_exclude_glob: optional glob pattern to exclude files from the list
        :return: the filtered collection
        """
        from serena.util.text_utils import GlobMatcher

        if paths_include_glob is None and paths_exclude_glob is None:
            return self

        include_glob_matcher = GlobMatcher(paths_include_glob) if paths_include_glob else None
        exclude_glob_matcher = GlobMatcher(paths_exclude_glob) if paths_exclude_glob else None

        filtered_files = []
        for f in self._file_proxies:
            if f.is_glob_supported():
                path = f.get_relative_path()
                if include_glob_matcher:
                    if not include_glob_matcher.matches(path):
                        log.debug(f"Skipping {path}: does not match include pattern {paths_include_glob}")
                        continue
                if exclude_glob_matcher:
                    if exclude_glob_matcher.matches(path):
                        log.debug(f"Skipping {path}: matches exclude pattern {paths_exclude_glob}")
                        continue
            filtered_files.append(f)

        return FileCollection(filtered_files)
