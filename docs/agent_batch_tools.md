# Agent batch tools

The feature branch `feat/agent-workflow-tools` contains `list_dirs` and
`replace_contents`. These are classic MCP tools, available through direct Serena
servers and the shared bridge after updating and restarting their processes.
They do not replace the existing single-operation tools. The broader agent-workflow
proposal is not yet fully implemented or merged.

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

The shared protocol remains version 7; runtime fingerprints include both batch
implementations. Update and restart bridges and the daemon together while idle.

## Validation scope

Tests cover pure batch behavior and real direct/bridge MCP CLI subprocesses with
the installed SDK in legacy and modern modes. The integration fixture explicitly
has no language servers configured. Native Zed/WSL and real configured LSP or
JetBrains editing sessions are separate validation targets; passing these tests
must not be described as exercising those environments.
