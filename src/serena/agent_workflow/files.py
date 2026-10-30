# SPDX-License-Identifier: GPL-3.0-or-later

"""Small, bounded batches of text-file snapshots under the project's access policy."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from serena.util.raw_file import RawFileContent
from solidlsp.ls_utils import TextUtils

if TYPE_CHECKING:
    from serena.project import Project


class FileReadRequest(BaseModel):
    """One known text file and an optional inclusive, zero-based line range."""

    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: str = Field(min_length=1, max_length=4096)
    start_line: int = 0
    end_line: int | None = Field(default=None, ge=0)


@dataclass
class _FileReadResult:
    request_index: int
    relative_path: str
    start_line: int
    end_line: int | None
    content: str = ""
    selected_chars: int | None = None
    file_sha256: str | None = None
    text_sha256: str | None = None
    truncated: bool = False
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


class FileBatchReader:
    """Read independent file snapshots and fit their complete serialized response.

    The byte bound applies to the whole file, even when a line range is selected.
    Files are read serially and do not constitute a multi-file filesystem snapshot.
    """

    MAX_FILES = 8
    _METADATA_RESERVE = 384

    def __init__(self, project: "Project", check_cancelled: Callable[[], None] | None = None):
        self._project = project
        self._check_cancelled = check_cancelled or (lambda: None)

    @staticmethod
    def _encode(results: list[_FileReadResult]) -> str:
        # escape control characters and surrogate filenames within the measured JSON bound
        return json.dumps({"results": [asdict(result) for result in results]}, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _check_limit(name: str, value: int, lower: int, upper: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}")

    @staticmethod
    def _prefix(text: str, max_chars: int, encoded_budget: int) -> str:
        # fit actual escaped JSON rather than assuming one character occupies one byte
        low, high = 0, min(len(text), max_chars, max(0, encoded_budget))
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(text[:middle], ensure_ascii=True)) - 2 <= encoded_budget:
                low = middle
            else:
                high = middle - 1
        return text[:low]

    def _read_one(self, request: FileReadRequest, result: _FileReadResult, max_bytes: int) -> str:
        # reuse the descriptor, access, symlink, regular-file and byte-limit protections
        raw = RawFileContent.read(self._project, request.relative_path, max_bytes)
        self._check_cancelled()
        text = raw.data.decode(self._project.project_config.encoding)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in text:
            raise ValueError("File contains NUL characters; use read_file_raw for binary content")

        # match read_file's line semantics, including empty files and trailing empty lines
        lines = TextUtils.split_lines(text)
        selected = lines[request.start_line :] if request.end_line is None else lines[request.start_line : request.end_line + 1]
        content = "\n".join(selected)
        result.selected_chars = len(content)
        result.file_sha256 = hashlib.sha256(raw.data).hexdigest()
        result.text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._check_cancelled()
        return content

    def read(
        self,
        files: list[FileReadRequest],
        max_chars_per_file: int = 8000,
        max_total_chars: int = 20000,
        max_bytes_per_file: int = RawFileContent.DEFAULT_MAX_BYTES,
    ) -> str:
        """Read up to eight known files while retaining successes beside individual errors.

        :param files: known paths and ranges; negative starts count from the end
        :param max_chars_per_file: maximum decoded characters returned for each file
        :param max_total_chars: maximum characters in the complete JSON response
        :param max_bytes_per_file: whole-file byte bound, including for range requests
        :return: ordered text results with separate errors, hashes and explicit truncation
        """
        # reject invalid batch shape and impossible response budgets before opening files
        if not 1 <= len(files) <= self.MAX_FILES:
            raise ValueError("files must contain between 1 and 8 requests")
        self._check_limit("max_chars_per_file", max_chars_per_file, 1, 200000)
        self._check_limit("max_total_chars", max_total_chars, 2048, 200000)
        self._check_limit("max_bytes_per_file", max_bytes_per_file, 1, RawFileContent.HARD_MAX_BYTES)
        results = [_FileReadResult(index, item.relative_path, item.start_line, item.end_line) for index, item in enumerate(files)]
        if len(self._encode(results)) + self._METADATA_RESERVE * len(results) > max_total_chars:
            raise ValueError("max_total_chars cannot hold the requested paths and per-file metadata")

        # keep filesystem failures local to their item, but propagate request interruption
        for index, (item, result) in enumerate(zip(files, results, strict=True)):
            self._check_cancelled()
            try:
                content = self._read_one(item, result, max_bytes_per_file)
            except TimeoutError:
                raise
            except (OSError, ValueError, LookupError) as error:
                message = f"{type(error).__name__}: {error}"
                result.error = self._prefix(message, 192, 192)
                continue

            # reserve future metadata and share the remaining output allowance fairly
            remaining = len(results) - index - 1
            result.truncated = True
            result.reasons = ["per_file_limit", "total_limit"]
            available = max_total_chars - len(self._encode(results)) - self._METADATA_RESERVE * remaining
            result.content = self._prefix(content, max_chars_per_file, max(0, available) // (remaining + 1))
            result.reasons = []
            if len(content) > max_chars_per_file:
                result.reasons.append("per_file_limit")
            if len(result.content) < min(len(content), max_chars_per_file):
                result.reasons.append("total_limit")
            result.truncated = len(result.content) < len(content)

        # include every item and reject any internal accounting error rather than hide it
        self._check_cancelled()
        rendered = self._encode(results)
        if len(rendered) > max_total_chars:
            raise RuntimeError("File response exceeded its reserved metadata allowance")
        return rendered
