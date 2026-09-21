# SPDX-License-Identifier: GPL-3.0-or-later
"""
The implementation of operations on the project's files.
"""

import os
import shutil
from collections import defaultdict
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from serena.tools import (
    CopyPathTool,
    CreateTextFileTool,
    DeletePathTool,
    FindFileTool,
    ListDirTool,
    MovePathTool,
    ReadFileTool,
    SearchForPatternTool,
)
from serena.util.file_proxy import FileProxy
from serena.util.file_system import scan_directory
from serena.util.text_utils import MatchedConsecutiveLines

from ..facade import FacadeApi, ReferencedType, facade_method
from ..representable import Renderer, RepresentableViaRenderer

if TYPE_CHECKING:
    from serena.agent import SerenaAgent


class FileContent(RepresentableViaRenderer):
    """
    The content of a file (or of a range of its lines): `text` (the joined lines) and `lines`.
    """

    def __init__(self, lines: list[str], renderer: "FileContentRenderer"):
        """
        :param lines: the lines (without line breaks)
        :param renderer: the renderer to use for representing the content
        """
        super().__init__(renderer)
        self.lines = lines

    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class FileContentRenderer(Renderer[FileContent]):
    def render(self, obj: FileContent) -> str:
        return self._limit_length(obj.text)


class DirectoryListing(RepresentableViaRenderer):
    """
    The entries of a directory: `dirs` and `files` (relative paths).
    """

    def __init__(self, dirs: list[str], files: list[str], renderer: "DirectoryListingRenderer"):
        """
        :param dirs: the relative paths of the directories
        :param files: the relative paths of the files
        :param renderer: the renderer to use for representing the listing
        """
        super().__init__(renderer)
        self.dirs = dirs
        self.files = files

    dirs: list[str]
    files: list[str]


class DirectoryListingRenderer(Renderer[DirectoryListing]):
    def render(self, obj: DirectoryListing) -> str:
        return self._limit_length(self._to_json({"dirs": obj.dirs, "files": obj.files}))


class PatternMatches(RepresentableViaRenderer):
    """
    The matches of a pattern search (`MatchedConsecutiveLines`).
    """

    def __init__(self, matches: list[MatchedConsecutiveLines], renderer: "PatternMatchesRenderer"):
        """
        :param matches: the matches
        :param renderer: the renderer to use for representing the matches
        """
        super().__init__(renderer)
        self.matches = matches

    matches: list[MatchedConsecutiveLines]

    def __len__(self) -> int:
        return len(self.matches)

    def matches_by_file_(self) -> dict[str, list[MatchedConsecutiveLines]]:
        result: defaultdict[str, list[MatchedConsecutiveLines]] = defaultdict(list)
        for match in self.matches:
            assert match.source_file_path is not None
            result[match.source_file_path].append(match)
        return result


class PatternMatchesRenderer(Renderer[PatternMatches]):
    """
    Renders matches as a mapping from file paths to matched line blocks (with context), falling back to progressively
    shorter representations (first lines, truncated first lines, line numbers, per-file counts, a summary) if the
    length limit is exceeded.

    The renderer deliberately estimates candidate size before materialising it. A minified/generated file can have
    a single 100k+ character line with tens of thousands of regex matches on that line; eagerly formatting the whole
    line once per match can otherwise create many gigabytes of temporary strings before the output limit is applied.
    """

    _TEXT_TRUNCATE = 60

    @staticmethod
    def _full_result_lower_bound(matches_by_file: dict[str, list[MatchedConsecutiveLines]]) -> int:
        # Each displayed block contains every line_content at least once. JSON/prefix escaping only makes it larger.
        return sum(len(line.line_content) for matches in matches_by_file.values() for match in matches for line in match.lines)

    def _overflow_prefix(self, lower_bound: int) -> str:
        return (
            f"The answer is too long (at least {lower_bound} characters). "
            "You can adjust your query or raise the max_answer_chars parameter."
        )

    def render(self, obj: PatternMatches) -> str:
        matches_by_file = obj.matches_by_file_()
        max_answer_chars = self._get_max_answer_chars()
        full_lower_bound = self._full_result_lower_bound(matches_by_file)

        def first_line_text(match: MatchedConsecutiveLines, truncate: bool) -> str:
            # Slice before stripping when truncating, so a pathological 150k-character line is
            # never copied in full merely to produce a 60-character snippet.
            text = match.matched_lines[0].line_content
            if truncate and len(text) > self._TEXT_TRUNCATE:
                return text[: self._TEXT_TRUNCATE].strip() + "..."
            return text.strip()

        def render_first_lines(truncate: bool) -> str:
            compact = {
                path: [
                    {"line": match.matched_lines[0].line_number, "text": first_line_text(match, truncate)}
                    for match in matches
                ]
                for path, matches in matches_by_file.items()
            }
            if truncate:
                header = (
                    f"Matched lines (text over {self._TEXT_TRUNCATE} chars is truncated, marked with a trailing '...'); "
                    "use read_file with the line numbers for full content:"
                )
            else:
                header = "Matched lines per file; use read_file with the line numbers for surrounding context:"
            return f"{header}\n{self._to_json(compact)}"

        def make_line_numbers_only() -> str:
            numbers = {path: [match.matched_lines[0].line_number for match in matches] for path, matches in matches_by_file.items()}
            return f"Match lines per file:\n{self._to_json(numbers)}"

        def make_per_file_counts() -> str:
            counts = {path: len(matches) for path, matches in matches_by_file.items()}
            return f"Match counts per file:\n{self._to_json(counts)}"

        def make_summary() -> str:
            return f"Found {len(obj)} matches in {len(matches_by_file)} files."

        # Build shortening candidates only when their raw payload can plausibly fit. This avoids
        # allocating an intermediate that is known in advance to be much larger than the answer.
        shortened_factories: list[Callable[[], str]] = []
        first_lines_lower_bound = sum(
            len(match.matched_lines[0].line_content)
            for matches in matches_by_file.values()
            for match in matches
        )
        if first_lines_lower_bound <= max_answer_chars:
            shortened_factories.append(lambda: render_first_lines(truncate=False))

        # Truncated snippets have bounded per-match text, but for hundreds of thousands of
        # matches even those cannot fit. Skip directly to line numbers/counts in that case.
        truncated_estimate = sum(
            min(len(match.matched_lines[0].line_content), self._TEXT_TRUNCATE) + 40
            for matches in matches_by_file.values()
            for match in matches
        )
        if truncated_estimate <= max_answer_chars:
            shortened_factories.append(lambda: render_first_lines(truncate=True))

        line_number_estimate = sum(
            len(str(match.matched_lines[0].line_number)) + 2
            for matches in matches_by_file.values()
            for match in matches
        )
        if line_number_estimate <= max_answer_chars:
            shortened_factories.append(make_line_numbers_only)

        shortened_factories.extend([make_per_file_counts, make_summary])

        if full_lower_bound > max_answer_chars:
            prefix = self._overflow_prefix(full_lower_bound)
            for make_shorter in shortened_factories:
                shortened = make_shorter()
                candidate = f"{prefix}\n{shortened}"
                if len(candidate) <= max_answer_chars:
                    return candidate
            return prefix

        # The lower bound fits, so materialising the complete result is memory-bounded by the
        # caller's requested answer size rather than by match_count * giant_line_length.
        file_to_matches = {path: [m.to_display_string() for m in matches] for path, matches in matches_by_file.items()}
        return self._limit_length(self._to_json(file_to_matches), shortened_result_factories=shortened_factories)


class FsApi(FacadeApi):
    def __init__(self, agent: "SerenaAgent") -> None:
        super().__init__(
            agent,
            name="fs",
            description="the project's files as units (as opposed to their content, see `edit`)",
            types=[
                ReferencedType(
                    MatchedConsecutiveLines, members=["source_file_path", "matched_lines", "start_line", "end_line", "to_display_string"]
                ),
            ],
        )

    def _resolve_fs_path(self, path: str) -> Path:
        """Validate a path against the active-project boundary and return an absolute lexical path."""
        if FileProxy.is_external_path(path):
            raise ValueError("Encoded JetBrains external paths are not supported by raw filesystem operations.")
        project = self._get_project()
        project.validate_relative_path(path)
        return Path(os.path.abspath(os.path.join(project.project_root, path)))

    def _mark_file_system_dirty_if_project_path(self, *paths: str) -> None:
        project = self._get_project()
        if any(project.is_path_in_project(path) for path in paths):
            project.mark_file_system_dirty()

    def _is_project_root(self, path: Path) -> bool:
        root = Path(os.path.abspath(self._get_project().project_root))
        return os.path.normcase(str(path)) == os.path.normcase(str(root))

    @staticmethod
    def _path_exists(path: Path) -> bool:
        # Path.exists() follows symlinks and returns False for a broken symlink, which is
        # still a real filesystem entry that delete/move/copy should be able to address.
        return os.path.lexists(path)

    @staticmethod
    def _remove_path(path: Path, recursive: bool) -> str:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return "file"

        if not recursive:
            if next(path.iterdir(), None) is not None:
                raise ValueError(f"Directory is not empty: {path}. Pass recursive=True to delete it recursively.")
            path.rmdir()
        else:
            shutil.rmtree(path)
        return "directory"

    def _validate_copy_move_destination(self, source: Path, destination: Path, overwrite: bool) -> None:
        source_real = source.resolve(strict=False)
        destination_real = destination.resolve(strict=False)
        if source_real == destination_real:
            raise ValueError("Source and destination refer to the same path.")

        # Never recursively copy/move a real directory into one of its descendants.
        if source.is_dir() and not source.is_symlink() and destination_real.is_relative_to(source_real):
            raise ValueError(f"Destination {destination} is inside source directory {source}.")

        if overwrite and self._path_exists(destination) and destination.is_dir() and not destination.is_symlink():
            destination_existing_real = destination.resolve()
            if source_real.is_relative_to(destination_existing_real):
                raise ValueError(f"Refusing to overwrite destination {destination}: it contains the source path.")
            project_root_real = Path(self._get_project().project_root).resolve()
            if project_root_real == destination_existing_real or project_root_real.is_relative_to(destination_existing_real):
                raise ValueError(f"Refusing to overwrite destination {destination}: it contains the active project root.")

    def _prepare_destination(self, source: Path, destination: Path, overwrite: bool) -> None:
        self._validate_copy_move_destination(source, destination, overwrite)
        if self._path_exists(destination):
            if not overwrite:
                raise FileExistsError(f"Destination already exists: {destination}")
            self._remove_path(destination, recursive=True)
        destination.parent.mkdir(parents=True, exist_ok=True)

    @facade_method(corresponding_tool=ReadFileTool)
    def read_file(self, relative_path: str, start_line: int = 0, end_line: int | None = None, max_answer_chars: int = -1) -> FileContent:
        """
        Reads the given file or a range of its lines.

        :param relative_path: path to the file. Normally relative to the project root; with full_access_mode enabled it may be absolute or point outside the project.
        :param start_line: the 0-based index of the first line to be retrieved, negative values count from the end of the file.
        :param end_line: the 0-based index of the last line to be retrieved (inclusive). If None, read until the end of the file.
        :return: the content
        """
        project = self._get_project()
        project.validate_relative_path(relative_path)

        # Read only the requested range where the backing file supports it, using the same
        # LSP-compliant notion of line breaks as the line-based editing operations.
        lines = project.read_file_lines(relative_path, start_line, end_line)
        return FileContent(lines, FileContentRenderer(self._agent, max_answer_chars))

    @facade_method(can_edit=True, corresponding_tool=CreateTextFileTool)
    def create_text_file(self, relative_path: str, content: str) -> str:
        """
        Writes a new file or overwrites an existing file with the given content.

        :param relative_path: path to the file to create. Normally relative to the project root; with full_access_mode enabled it may be absolute or point outside the project.
        :param content: the (appropriately encoded) content to write to the file
        :return: a message indicating success
        """
        project = self._get_project()
        project_root = Path(project.project_root)
        abs_path = (project_root / relative_path).resolve()
        will_overwrite_existing = abs_path.exists()

        # Validate both existing and not-yet-existing destinations. With full_access_mode
        # enabled this deliberately permits destinations outside the project root.
        project.validate_relative_path(relative_path)

        # write the file
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding=project.project_config.encoding, newline=project.line_ending.newline_str)
        self._mark_file_system_dirty_if_project_path(relative_path)
        answer = f"File created: {relative_path}."
        if will_overwrite_existing:
            answer += " Overwrote existing file."
        return answer

    @facade_method(can_edit=True, corresponding_tool=DeletePathTool)
    def delete_path(self, relative_path: str, recursive: bool = False) -> str:
        """
        Deletes a file, symlink, or directory.

        Directories must be empty unless recursive=True. The active project root itself is
        never deleted through this API.

        :param relative_path: path to delete; outside-project paths require full_access_mode
        :param recursive: whether a non-empty directory may be deleted recursively
        :return: a message indicating what was deleted
        """
        project = self._get_project()
        path = self._resolve_fs_path(relative_path)
        if self._is_project_root(path):
            raise ValueError("Refusing to delete the active project root.")
        if not self._path_exists(path):
            raise FileNotFoundError(f"Path not found: {relative_path}")

        kind = self._remove_path(path, recursive=recursive)
        self._mark_file_system_dirty_if_project_path(relative_path)
        return f"Deleted {kind}: {relative_path}."

    @facade_method(can_edit=True, corresponding_tool=CopyPathTool)
    def copy_path(self, source_path: str, destination_path: str, overwrite: bool = False) -> str:
        """
        Copies a file, symlink, or directory to an exact destination path.

        Directory copies are recursive and preserve symlinks. Existing destinations are
        rejected unless overwrite=True.

        :param source_path: source path; outside-project paths require full_access_mode
        :param destination_path: exact destination path; outside-project paths require full_access_mode
        :param overwrite: whether an existing destination may be replaced
        :return: a message indicating success
        """
        project = self._get_project()
        source = self._resolve_fs_path(source_path)
        destination = self._resolve_fs_path(destination_path)
        if not self._path_exists(source):
            raise FileNotFoundError(f"Source path not found: {source_path}")

        self._prepare_destination(source, destination, overwrite)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
            kind = "directory"
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
            kind = "path"

        self._mark_file_system_dirty_if_project_path(destination_path)
        return f"Copied {kind}: {source_path} -> {destination_path}."

    @facade_method(can_edit=True, corresponding_tool=MovePathTool)
    def move_path(self, source_path: str, destination_path: str, overwrite: bool = False) -> str:
        """
        Moves or renames a file, symlink, or directory to an exact destination path.

        Cross-filesystem moves are supported through shutil.move. Existing destinations are
        rejected unless overwrite=True. The active project root itself is never moved.

        :param source_path: source path; outside-project paths require full_access_mode
        :param destination_path: exact destination path; outside-project paths require full_access_mode
        :param overwrite: whether an existing destination may be replaced
        :return: a message indicating success
        """
        project = self._get_project()
        source = self._resolve_fs_path(source_path)
        if self._is_project_root(source):
            raise ValueError("Refusing to move the active project root.")
        destination = self._resolve_fs_path(destination_path)
        if not self._path_exists(source):
            raise FileNotFoundError(f"Source path not found: {source_path}")

        self._prepare_destination(source, destination, overwrite)
        shutil.move(str(source), str(destination))

        self._mark_file_system_dirty_if_project_path(source_path, destination_path)
        return f"Moved path: {source_path} -> {destination_path}."

    @facade_method(corresponding_tool=ListDirTool)
    def list_dir(
        self, relative_path: str, recursive: bool, skip_ignored_files: bool = False, max_answer_chars: int = -1
    ) -> DirectoryListing:
        """
        Lists files and directories in the given directory (optionally with recursion).

        :param relative_path: path to the directory to list; pass "." to scan the project root. With full_access_mode enabled it may be absolute or point outside the project.
        :param recursive: whether to scan subdirectories recursively
        :param skip_ignored_files: whether to skip files and directories that are ignored
        :return: the listing
        """
        project = self._get_project()
        if not project.relative_path_exists(relative_path):
            raise FileNotFoundError(f"Directory not found: {relative_path} (check if the path is correct relative to the project root)")
        project.validate_relative_path(relative_path)

        is_ignored_path_fn = project.get_is_ignored_path_fn(relative_path, skip_ignored_files)
        dirs, files = scan_directory(
            os.path.join(project.project_root, relative_path),
            relative_to=project.project_root,
            recursive=recursive,
            is_ignored_dir=is_ignored_path_fn,
            is_ignored_file=is_ignored_path_fn,
        )
        return DirectoryListing(dirs, files, DirectoryListingRenderer(self._agent, max_answer_chars))

    @facade_method(corresponding_tool=FindFileTool)
    def find_file(self, file_mask: str, relative_path: str) -> list[str]:
        """
        Finds files matching the given file mask within the given relative path.

        :param file_mask: the filename or file mask (using the wildcards * or ?) to search for
        :param relative_path: path to the directory to search in; pass "." to scan the project root. With full_access_mode enabled it may be absolute or point outside the project.
        :return: the relative paths of the matching files
        """
        project = self._get_project()
        project.validate_relative_path(relative_path)

        is_ignored_path_fn = project.get_is_ignored_path_fn(relative_path, skip_ignored_paths=False)

        # find the files by ignoring everything that doesn't match
        def is_ignored_file(abs_path: str) -> bool:
            if is_ignored_path_fn(abs_path):
                return True
            return not fnmatch(os.path.basename(abs_path), file_mask)

        _dirs, files = scan_directory(
            path=os.path.join(project.project_root, relative_path),
            recursive=True,
            is_ignored_dir=is_ignored_path_fn,
            is_ignored_file=is_ignored_file,
            relative_to=project.project_root,
        )
        return files

    @facade_method(corresponding_tool=SearchForPatternTool)
    def search_for_pattern(
        self,
        substring_pattern: str,
        context_lines_before: int = 0,
        context_lines_after: int = 0,
        paths_include_glob: str = "",
        paths_exclude_glob: str = "",
        relative_path: str = "",
        restrict_search_to_code_files: bool = False,
        skip_ignored_files: bool = True,
        multiline: bool = True,
        max_answer_chars: int = -1,
    ) -> PatternMatches:
        """
        Searches for a regex pattern across project files, returning whole matched lines (plus optional context).
        Prefer symbolic operations if you know which symbols you are looking for!

        :param substring_pattern: regular expression to search for.
        :param context_lines_before: number of context lines to include before each match.
        :param context_lines_after: number of context lines to include after each match.
        :param paths_include_glob: optional glob (relative to project root, e.g. ``"src/**/*.ts"``) restricting which files are searched.
        :param paths_exclude_glob: optional glob to exclude files; takes precedence over `paths_include_glob`.
        :param relative_path: restricts the search to this file or directory. Normally project-relative; with full_access_mode enabled it may be absolute or point outside the project.
        :param restrict_search_to_code_files: whether to search only (non-ignored) files containing analyzable code symbols
            (useful when looking for class/method definitions); otherwise also search non-code files.
        :param skip_ignored_files: whether to skip ignored sub-paths (default: True)
        :param multiline: whether to apply multi-line matching (default: True), enabling the flags re.DOTALL and re.MULTILINE
        :return: the matches, rendered as a mapping from file paths to matched consecutive lines (0-based line numbers)
        """
        project = self._get_project()
        relative_path = relative_path.strip()
        if relative_path:
            project.validate_relative_path(relative_path)

        matches = project.search_project_files_for_pattern(
            pattern=substring_pattern,
            relative_path=relative_path,
            context_lines_before=context_lines_before,
            context_lines_after=context_lines_after,
            paths_include_glob=paths_include_glob.strip(),
            paths_exclude_glob=paths_exclude_glob.strip(),
            multiline=multiline,
            code_files_only=restrict_search_to_code_files,
            skip_ignored_files=skip_ignored_files,
        )
        return PatternMatches(matches, PatternMatchesRenderer(self._agent, max_answer_chars))
