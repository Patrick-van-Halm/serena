# Agent batch tools

Serena provides `read_files`, `list_dirs`, and `replace_contents`. These are classic MCP tools, available through direct Serena
servers and the shared bridge after updating and restarting their processes.
They do not replace the existing single-operation tools. The broader agent-workflow
proposal is not yet fully implemented; this release contains these three batch tools.

## Multiple text-file reads

`read_files` reads up to eight known regular files in one serial, read-only call.
Each request may specify an inclusive, zero-based line range:

```json
{
  "files": [
    {"relative_path": "package.json"},
    {"relative_path": "src/api/config.ts", "start_line": 0, "end_line": 79},
    {"relative_path": "src/frontend/config.ts"}
  ],
  "max_chars_per_file": 8000,
  "max_total_chars": 20000,
  "max_bytes_per_file": 5242880
}
```

Each result retains its request index, requested path/range, text `content`,
`selected_chars`, `file_sha256`, `text_sha256`, `truncated`, `reasons`, and `error`.
A missing, denied, undecodable, binary, or oversized file does not discard the
other results. Empty text and an error are distinguishable. Invalid batch shape
or limits reject the call before any files are opened.

`start_line` defaults to zero; a negative start counts from the end. `end_line`
is inclusive, nonnegative, and defaults to null (the rest of the file). Text uses
the project's configured encoding and normalizes CRLF and CR to LF, matching
`read_file`. NUL-containing decoded text is rejected; use `read_file_raw` for
images and other binary files. Encoded JetBrains external-file identifiers are
not local files and are unsupported; use `read_file` for those identifiers.

The implementation reuses the existing bounded regular-file reader, including
its path/symlink validation, opened-file identity checks and size bounds. The
whole file is read before selecting lines. This permits complete-file hashes
and limits memory use, but means the byte limit applies even to a short range.
Use the existing streaming `read_file` for targeted ranges in large files.
`max_bytes_per_file` defaults to 5 MiB and is capped at 20 MiB. No truncated byte
sequence is decoded. Reads do not constitute a multi-file filesystem snapshot.

`file_sha256` hashes the original bytes actually read. `text_sha256` hashes the
UTF-8 encoding of the full decoded, newline-normalized text, not just the selected
range or returned prefix. A file may change after reading, and unsaved editor
buffers can differ from disk. These values do not prove the current editor state;
use the replacement preview's version guard for edits.

Returned content is a prefix of the selected range when a limit is reached; that
prefix can end within a line. `reasons` distinguishes `per_file_limit` and
`total_limit`. Remaining output space is shared among the remaining files.
The entire JSON text result, including escaped content and per-file metadata,
fits `max_total_chars` (2048..200000); this does not include the outer MCP envelope.
`max_chars_per_file` accepts 1..200000 decoded characters. Budgets too small for
all request metadata are rejected rather than silently dropping records.

`read_file` must remain enabled in the active context. The REPL's fixed toolset is
unchanged. Direct MCP and the shared bridge use the same JSON-compatible request
schema. Runtime fingerprints cover this implementation; the shared protocol
remains version 7. Update and restart the bridges and daemon together while idle.

## Multiple directory listings

Call `list_dirs` with up to eight known directory requests:

```json
{
  "directories": [
    {"relative_path": "src/api"},
    {"relative_path": "src/frontend", "recursive": true, "max_depth": 2}
  ],
  "max_entries_per_dir": 200,
  "max_total_chars": 20000
}
```

Each result includes its request index, relative path, directories, files,
symlinks, error, and explicit truncation reasons. One missing or inaccessible path
does not discard successful sibling results. Discovered symlinks are listed but
not traversed. An explicitly supplied symlink root is resolved and checked against
the existing global outside-project access policy.

Recursion defaults to false, ignored files are skipped by default, and recursive
requests default to a maximum depth of four. Entry limits count inspected entries,
including ignored entries, and are capped at 5000 per root. The complete JSON
response is limited to 2048..200000 characters. Limits do not promise a wall-clock
bound on blocked filesystem operations. Partial listings do not establish absence
of unreturned files, and listings are not filesystem snapshots.

`list_dir` must remain enabled in the active context. A composite tool does not
bypass the exclusion of its underlying operation.

## Different replacements across files

Call `replace_contents` with different replacement entries:

```json
{
  "replacements": [
    {
      "relative_path": "src/api/config.ts",
      "needle": "timeout: 1000",
      "repl": "timeout: 2000",
      "mode": "literal",
      "expected_count": 1
    },
    {
      "relative_path": "src/frontend/config.ts",
      "needle": "retryCount: 2",
      "repl": "retryCount: 3",
      "mode": "literal",
      "expected_count": 1
    }
  ],
  "dry_run": true
}
```

After reviewing the preview, repeat with `dry_run: false`. For stale-content
protection, copy each returned `original_content_sha256` into
`expected_content_sha256` on the corresponding entries. Every hash guard refers
to the original editor text, even when multiple entries modify the same file.
These hashes cover UTF-8-encoded editor text, not necessarily original disk bytes;
line-ending normalization means `read_file_raw` hashes are not interchangeable.

Entries targeting the same canonical file are evaluated in input order against
the staged text. Each changed file is written once. Literal/regex matching,
ambiguity rejection, and `$!1` group references reuse the existing content
replacer. Multiple occurrences require `allow_multiple_occurrences: true`.
`expected_count` is an optional additional exact-count guard.

Preflight validates all paths, matches, hashes, size limits, and encodability
before writing. A preflight rejection changes no files. Files are reread before
writing and checked again inside the write operation. This is optimistic change
detection, not a filesystem transaction against concurrent external editors.

**Multi-file writes are not atomic.** A write or cancellation failure can leave
some files applied. Reports distinguish `applied`, `unchanged`, `unknown`, and
`not_applied`. Unknown means a write may have happened before an error was raised.
There is no automatic rollback, replay, or retry. Check resulting state before
retrying interrupted modifications; host cancellation can prevent the detailed
report from reaching the client.

Limits: 50 replacement entries, 16 canonical files, 1 Mi characters per source or
updated file, 8 Mi characters of combined original/updated text, and 4096..200000
characters for the serialized outcome report. Preview excerpts may be shortened;
per-file outcome metadata is never silently discarded. A response allowance too
small for that metadata rejects preflight. Regex checks between matches do not
provide a hard CPU timeout inside a pathological Python regex evaluation.

The `replace_content` tool must be enabled, and the project must allow edits,
including for dry runs. Configured language backends retain their existing editor
path. LSP projects explicitly configured with no language servers use Serena's
existing text reader and atomic single-file writer, respecting project encoding
and line-ending settings. Backend failures never silently select that path.

The shared protocol remains version 7; runtime fingerprints include all three batch
implementations. Update and restart bridges and the daemon together while idle.

## Validation scope

Tests cover pure batch behavior and real direct/bridge MCP CLI subprocesses with
the installed SDK in legacy and modern modes. The integration fixture explicitly
has no language servers configured. Native Zed/WSL and real configured LSP or
JetBrains editing sessions are separate validation targets; passing these tests
must not be described as exercising those environments.
