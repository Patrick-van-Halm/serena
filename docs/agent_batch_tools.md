# Agent batch tools

Serena's file, symbol, memory, and JetBrains MCP operations accept batches. Use a one-item array
for a single target. These interfaces are available through direct Serena servers and the shared
bridge after updating and restarting their processes. The scalar Python and REPL facade methods
remain available for programmatic composition.

Without an active project, filesystem batches use the selected startup working directory and
retain its path, encoding, and ignore rules without creating or registering a Serena project.
Global memory batches remain available using `global/` names. Language-aware symbol operations,
code-only searches, local project memories, and onboarding still require an active project.
Activating a project switches the filesystem root and applies that project's permissions.

## Common batch contract

The tools in the tables below accept **one to eight request objects**. Each object contains the
parameters of the corresponding per-item operation. Optional parameters retain their existing
defaults; for example, `include_body`, `depth`, and a result limit for an individual symbol query
belong inside that query object. The MCP schema lists each item's accepted fields.

```json
{
  "queries": [
    {"name_path_pattern": "UserService", "relative_path": "src/users.py", "depth": 1},
    {"name_path_pattern": "OrderService/save", "relative_path": "src/orders.py", "include_body": true}
  ],
  "max_answer_chars": 20000
}
```

This example calls `find_symbols`. The outer `max_answer_chars` bounds the complete JSON report
and accepts 4096..200000 characters. Its default is 20000. Each result retains its request `index`,
a `request` object identifying the target, `status`, `result`, `error`, and `truncated`. Source text,
replacement bodies, and other large inputs are not echoed into request metadata. Result text can
be shortened, with `truncated: true`; item identity and outcome metadata are retained. A budget too
small for that metadata rejects the batch before operations start.

The entire batch shape, field types, and limits are validated before any operation runs. Unknown
fields and invalid types are rejected rather than silently ignored or coerced. This validates
request syntax, **not every future filesystem or backend outcome**. Existing path checks, ignored
memory rules, overwrite protections, backend restrictions, and editing permissions still apply.

Requests execute serially in input order. Read operations retain errors per item and continue with
independent siblings. A cancelled call stops execution; a terminated language server retains the
existing read-only recovery behavior. A batch is not a multi-file snapshot.

For editing operations, `completed` means the underlying operation returned normally. Inspect
`result`: an operation can report a conflict, refusal, or dry-run preview without applying a change.
If an operation raises, its status is `unknown`, because a side effect may already have occurred;
later entries are `not_applied`. Earlier completed entries are preserved. There is no rollback or
automatic replay. A cancellation or transport interruption can prevent the report from reaching
the client; inspect state before retrying modifications. **Multi-item edits are not atomic.**

Operations on the same file see changes from earlier entries. For line-number operations, later
line indices refer to the file after earlier edits. Batch only requests whose inputs you already
know; use a separate call if an input depends on an earlier result.

`read_files`, `list_dirs`, and `replace_contents` retain their specialized limits and reporting
contracts, documented below. `read_files_raw` has a separate binary-byte budget and native MCP
content format; see [raw reads](raw_file_tool.md).

### Filesystem operations

| Previous MCP name | Bulk MCP name | Array parameter |
| --- | --- | --- |
| `create_text_file` | `create_text_files` | `files` |
| `find_file` | `find_files` | `queries` |
| `delete_path` | `delete_paths` | `paths` |
| `copy_path` | `copy_paths` | `copies` |
| `move_path` | `move_paths` | `moves` |
| `delete_lines` | `delete_lines_in_files` | `edits` |
| `replace_lines` | `replace_lines_in_files` | `edits` |
| `insert_at_line` | `insert_at_lines` | `insertions` |
| `search_for_pattern` | `search_for_patterns` | `queries` |
| `read_file_raw` | `read_files_raw` | `files` |

For example, call `create_text_files` with:

```json
{
  "files": [
    {"relative_path": "notes/first.md", "content": "First note\n"},
    {"relative_path": "notes/second.md", "content": "Second note\n"}
  ]
}
```

Creating a file retains its existing overwrite behavior. Path copy/move entries retain their
per-item `overwrite` option, and deletion retains its per-item `recursive` option.

### Symbol operations

| Previous MCP name | Bulk MCP name | Array parameter |
| --- | --- | --- |
| `get_symbols_overview` | `get_symbols_overviews` | `files` |
| `find_symbol` | `find_symbols` | `queries` |
| `find_referencing_symbols` | `find_referencing_symbols` | `symbols` |
| `find_implementations` | `find_implementations` | `symbols` |
| `find_declaration` | `find_declarations` | `queries` |
| `get_diagnostics_for_file` | `get_diagnostics_for_files` | `files` |
| `get_diagnostics_for_symbol` | `get_diagnostics_for_symbols` | `symbols` |
| `replace_symbol_body` | `replace_symbol_bodies` | `replacements` |
| `insert_after_symbol` | `insert_after_symbols` | `insertions` |
| `insert_before_symbol` | `insert_before_symbols` | `insertions` |
| `rename_symbol` | `rename_symbols` | `renames` |
| `safe_delete_symbol` | `safe_delete_symbols` | `symbols` |

`find_referencing_symbols` and `find_implementations` keep their already-plural names, but their
inputs are now arrays of symbol requests. Optional line-editing and symbol-diagnostics tools
remain optional. Symbol operations retain their existing language-server activity lifetime,
diagnostics, and backend behavior.

### Memory operations

| Previous MCP name | Bulk MCP name | Array parameter |
| --- | --- | --- |
| `read_memory` | `read_memories` | `memories` |
| `write_memory` | `write_memories` | `memories` |
| `edit_memory` | `edit_memories` | `memories` |
| `delete_memory` | `delete_memories` | `memories` |
| `rename_memory` | `rename_memories` | `renames` |

For example, `read_memories` accepts `{"memories": [{"memory_name": "core"},
{"memory_name": "conventions"}]}`. Existing read-only/ignored-memory protections, name validation,
reference updates on rename, and permissions for global memories remain in effect.
`list_memories` already lists multiple memories and keeps its existing interface.

### JetBrains operations

All names in this table have the `jet_brains_` prefix.

| Previous suffix | Bulk suffix | Array parameter |
| --- | --- | --- |
| `find_symbol` | `find_symbols` | `queries` |
| `get_symbols_overview` | `get_symbols_overviews` | `files` |
| `find_referencing_symbols` | `find_referencing_symbols` | `symbols` |
| `find_implementations` | `find_implementations` | `symbols` |
| `find_declaration` | `find_declarations` | `queries` |
| `type_hierarchy` | `type_hierarchies` | `symbols` |
| `run_inspections` | `run_inspections` | `files` |
| `move` | `move_items` | `moves` |
| `rename` | `rename_items` | `renames` |
| `safe_delete` | `safe_delete_items` | `items` |
| `inline_symbol` | `inline_symbols` | `symbols` |

The tool descriptions retain their backend-specific request fields and restrictions. Refactoring
results must be checked for conflicts or refusals even when the batch item is `completed`.
Project/global operations such as inspection listing, debugging, project activation, server
restart, shell execution, and REPL execution retain their existing interfaces. `replace_in_files`
already applies one replacement across discovered files and remains a specialized bulk operation.

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
the internal text reader. NUL-containing decoded text is rejected; use `read_files_raw` for
images and other binary files. Encoded JetBrains external-file identifiers are
not local files and are unsupported; the REPL retains `s.fs.read_file` for those identifiers.

The implementation reuses the existing bounded regular-file reader, including
its path/symlink validation, opened-file identity checks and size bounds. The
whole file is read before selecting lines. This permits complete-file hashes
and limits memory use, but means the byte limit applies even to a short range.
The internal/REPL `s.fs.read_file` still supports streaming ranges; the MCP batch tool retains its byte cap.
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

`read_files` is governed by its own active tool policy. The REPL's fixed toolset and per-item APIs are
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

`list_dirs` is governed by its own active tool policy.

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
line-ending normalization means `read_files_raw` hashes are not interchangeable.

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

The `replace_contents` tool must be enabled, and the project must allow edits,
including for dry runs. Configured language backends retain their existing editor
path. LSP projects explicitly configured with no language servers use Serena's
existing text reader and atomic single-file writer, respecting project encoding
and line-ending settings. Backend failures never silently select that path.

The shared protocol remains version 7; runtime fingerprints include the batch adapter,
operation implementations, and backend mappings. Update and restart bridges and the daemon together while idle.

## Validation scope

Tests cover pure batch behavior and real direct/bridge MCP CLI subprocesses with
the installed SDK in legacy and modern modes. The transport integration fixture
explicitly has no language servers configured. A separate live Python test uses
the locked `ty` server to read, edit, and reread symbols across two files. JetBrains
request forwarding and defaults are covered with a mocked plugin client. Native
Zed/WSL sessions, other live language servers, and a running JetBrains IDE remain
separate validation targets.

## Migration from single-item MCP calls

Use `read_files({"files": [{"relative_path": "README.md"}]})`,
`list_dirs({"directories": [{"relative_path": "."}]})`, and
`replace_contents({"replacements": [{"relative_path": "config.txt", "needle": "old", "repl": "new", "mode": "literal"}]})`.
These examples show tool names followed by their JSON argument objects.

Legacy names in `excluded_tools`, `included_optional_tools`, or `fixed_tools`
map to the bulk replacements in the tables above, as do `read_file`, `list_dir`,
`replace_content`, and the older `replace_regex` name.
Excluding an old name therefore still excludes its replacement. This configuration
migration does not preserve old tool-call endpoints or scalar argument schemas.
Update custom tool descriptions and client allowlists to the bulk names, then
restart all bridges/servers and refresh the client's tool catalog.

Some tool names were already plural, such as `find_referencing_symbols`, `find_implementations`,
and `jet_brains_run_inspections`; those names now also require request arrays. Refresh schemas
as well as names. Use the field names in the new item schemas, such as `name_path_pattern` for
symbol searches. Scalar parameter aliases are not part of the new batch schemas.

The per-item Python/REPL facade methods remain available for programmatic use. For tools using
the common adapter, their existing Python `apply` methods retain scalar signatures, while the
MCP entry points expose arrays. Use the public tool schema when calling through MCP or shared RPC.
`replace_in_files` retains its existing multi-file interface.
