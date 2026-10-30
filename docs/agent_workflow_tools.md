# Agent workflow tools

Implementation branch: `feat/agent-workflow-tools`.

## Multiple directory listings

`list_dirs` retrieves up to eight known directories in one serial, read-only call:

```json
{
  "directories": [
    {"relative_path": "src/features/models", "recursive": false},
    {"relative_path": "test", "recursive": true, "max_depth": 2}
  ],
  "max_entries_per_dir": 200,
  "max_total_chars": 20000
}
```

Each result has its request index, requested path, `dirs`, `files`, `symlinks`,
`error`, `truncated`, and `reasons`. A missing, inaccessible, or non-directory
path does not discard successful siblings. Paths in the listing are relative
to the canonical project root. Explicit outside-project roots require the
existing global `full_access_mode` setting; resolved roots are validated too.

Discovered symlinks are listed but not followed. An explicitly requested symlink
root is resolved and validated before traversal. Recursive depth counts levels
below the requested root; depth zero lists immediate entries but no descendants.
`skip_ignored_files` defaults to true for each request.

The inspected-entry limit includes ignored entries. It bounds traversal work,
not filesystem call latency. Results are sorted after enumeration; a limited
subset follows filesystem enumeration order and is not a repeatable snapshot.
A truncated result must not be used to prove that another path is absent.

The whole serialized JSON response respects `max_total_chars` (2048..200000).
Up to 5000 entries may be inspected per directory. Requests whose path metadata
cannot fit are rejected before traversal. Cancellation/deadline errors propagate
rather than being misreported as missing paths. The normal project executor
continues to serialize this operation with edits.

The tool requires `list_dir` to be enabled in the active classic tool context.
It does not bypass that exclusion. The REPL's fixed tool interface is unchanged.
Restart the bridge and daemon together while idle after updating the installation;
the runtime fingerprint covers the new implementation. The wire protocol remains 7.

The remaining proposed agent-workflow tools are not implemented by this change.
