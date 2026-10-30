# Agent workflow tools

Implementation branch: `feat/agent-workflow-tools`.

## Implemented

- `list_dirs`: multiple bounded directory listings with independent errors.
- `replace_contents`: different replacements across explicit files, with full
  preflight, dry-run previews, original-content hashes, and partial-write reporting.

See [Agent batch tools](agent_batch_tools.md) for arguments, examples, limits,
permissions, transport behavior, and validation scope.

`replace_contents` addresses the text-replacement portion of the proposed
heterogeneous `apply_edits` capability; it is not a generic symbolic-edit transaction.

## Remaining proposal scope

The following capabilities are not implemented by this branch yet:

- `read_files` and scoped `get_task_context`.
- Operation status, retained results, managed commands, and structured checks.
- Bounded/resumable pattern search and structured change inspection.
- Composed symbol context, memory content search, and transformed image inspection.

The feature branch has not been merged into `optimizations`. Passing the batch-tool
tests does not establish completion or validation of the remaining proposal.
