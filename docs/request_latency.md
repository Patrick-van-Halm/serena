# Request-to-result latency investigation

Base: `optimizations` at `db388954b3b72d83a3411fd3d5d86b4caf883c5a`.
Work branch: `perf/request-to-result-latency`. This work is not merged automatically.

## What the supplied timeline establishes

The supplied durations are execution observations, not a complete host/model trace.
The first session's 27 listed durations total 719.256 seconds; its five shell
calls total 666.689 seconds (92.69%). These sums are not wall-clock totals: calls
can overlap, and the log timestamp convention was not supplied. The median listed
call duration is 0.390 seconds, so the long tail matters much more than the median.

The first five setup calls total 0.717 seconds, while their listed timestamps
span 41 seconds. The first shell call is 228 seconds after initial_instructions.
The delegated session's first five setup calls total 0.028 seconds but span
17 seconds; its first shell call is 23 seconds after initial_instructions.
This is time not explained by the reported execution durations, not proof of
model reasoning time: host scheduling, approvals, queue time before execution
logging, network/model latency, and unreported activity remain possible.

The delegated test commands took 9.817 + 6.718 + 1.400 = 17.935 seconds;
type checking took 17.172 seconds. These were not controlled, identical-command
repeats of the earlier compound commands, so no causal speedup factor follows.

No model generation token counts, first-token timestamps, reasoning effort,
provider, prompt-cache hits, or host request-start timestamps were supplied.
Neither model tokens/second nor the complete user-request-to-result latency
can be calculated from these logs. Tool output token estimates are not model TPS.

## Confirmed source findings and changes

### Cancelled shell commands can retain the serial executor

`util/shell.py` originally made one blocking `Popen.communicate(timeout=timeout)`
call without observing `ToolCallContext`. `ShellApi` computed that timeout from
the configured full tool timeout, rather than time remaining on the original
request. Cancellation of the outer waiter did not make this helper stop early.
The executor intentionally retains execution protection until real work exits.
This creates head-of-line blocking; increasing the thread pool is not a safe fix.

The helper now checks cancellation and the inherited remaining deadline before
spawning and at most every 100 ms while waiting. That is a polling interval, not
a guarantee that process termination finishes within 100 ms. It terminates only
its owned process tree using existing cleanup logic before returning control.
No overlapping edits, automatic write replay, or unrelated-process termination
is introduced. Commands can have partial side effects before cancellation;
verify state before retrying an interrupted modification.

During cleanup the shared daemon can still reject another operation with
`Shared runtime is still finishing interrupted call` and explicitly state that
the new operation was not submitted. This guard is intentionally retained. The
integration test retries only a read probe with that exact unsubmitted refusal,
within four seconds; it never retries the interrupted shell or a write.

### Oversized results discard useful completed work

`ShellCommandOutputRenderer` originally replaced oversized output with the generic
length error. After waiting for a test process, an agent could lose both its exit
status and the final summary and be tempted to rerun it.

The renderer now returns a serialized-JSON-bounded preview retaining `return_code`,
`cwd`, leading/trailing stdout and stderr excerpts, original character counts,
and `output_truncated: true`. Complete output remains available in the REPL result
object; responses that already fit are unchanged. A limit too small for even the
metadata still uses the existing length-error fallback. This bounds the returned
preview, not subprocess capture memory or command runtime. It does not rerun work.

### Execution logs omit queueing and return-path time

Tool application logging happens inside the executor task. A request rejected or
cancelled in the queue need not have an execution entry. Timing only the tool body
also misses usage accounting, logging, and cache-save work before it returns.

Enable **new, opt-in** metadata tracing in both bridge and daemon environments:

```bash
export SERENA_TRACE_TOOL_LATENCY=1
```

Restart the relevant processes to inherit it; exporting in another terminal does
not change Zed's environment or an already-running daemon. Events appear in the
normal Serena logs as `Tool latency: { ... }`. They contain request ID, span ID,
PID, elapsed milliseconds, remaining allowance, local execution outcome, and
(where known) the tool/session identity. They do not add tool arguments, command
strings, file contents, output payloads, or exception messages. Other pre-existing
Serena log statements retain their existing behavior.

Events:

- `mcp_received`, `worker_admitted`, `mcp_finished`: wrapper entry, admission to
  blocking work, and completion of the wrapper wait (including cancellation).
- `daemon_received`: daemon request registration, before execution checks.
- `tool_received`, `executor_dequeued`, `execution_started`: submission, dequeue,
  and successful acquisition of the execution guard.
- `apply_finished`, `execution_finished`, `tool_wait_finished`: successful tool
  body completion, actual worker exit, and waiter completion including errors.

Compute stage differences within the same PID/span. Join bridge and daemon
records by request ID; do not sum their overlapping end-to-end durations or
subtract monotonic timestamps from separate processes. `local_outcome` is not a
transport status: a bridge may have received a successful remote response while
its local conservative remote-dispatch state still says `unknown`. A caller's
`mcp_finished` may precede `execution_finished` after interruption. Missing
`apply_finished` on failure is expected.

The measurement starts when Serena's tool wrapper begins, after SDK argument
validation, and ends before SDK result conversion, serialization, and transport
flush. It does not measure the user's initial prompt, model processing, IDE
approval, final-token emission, or complete client-observed tool latency. Measure
those at the host and correlate tool-call IDs for the full critical path.

## Highest-value next optimizations

1. Reduce unnecessary model/tool round trips. Once applicable instructions are
   understood, request small batches of already-known independent reads rather
   than interleaving each millisecond read with another model turn. Respect nested
   AGENTS.md scope and dependencies; do not guess instruction locations or batch
   dependent edits. No extra token-budget prompting is necessary.
2. Scope broad searches by directory/glob. `FsApi.search_for_pattern` obtains the
   matches before constructing its output renderer: a smaller `max_answer_chars`
   is an answer limit, not a bound on search computation. A bounded/streaming
   search needs separate correctness work, particularly for multiline regexes.
3. Run explicit, narrow test commands in non-watch mode and separate verification
   from large process/diff dumps. Preserve exit codes and collect summaries from
   existing results rather than rerunning completed commands for output.
4. Compare agent configurations with identical tasks, model versions, and
   cold/warm cache conditions. Record user prompt receipt, model request start,
   first/last token, input/output/reasoning tokens when available, host dispatch,
   client tool-result receipt, and final answer completion. Report model decoding
   TPS separately from task latency, tool throughput, and effective output tokens
   per total wall second. A faster model or reduced reasoning effort must still
   pass the same task-correctness checks.

Avoid increasing global parallelism or releasing the project guard on timeout:
these changes can overlap edits and merely move work into contention. This branch
does not alter model selection, reasoning effort, tool availability, or global
concurrency. The initial instructions now include small independent read batches,
scoped searches, focused verification, and reuse of completed command results.
TPS measurement guidance applies only when a performance investigation is requested;
profiling is not a prerequisite for ordinary coding work.

## Reproduction and validation

The latency workflow uses the installed locked SDK/application dependencies,
real subprocesses, real stdio direct/bridge clients, and existing daemon tests.
It tests owned descendant cleanup, preservation of unrelated processes, queued
follow-up progress, remaining deadlines, byte/text capture across polling,
JSON preview bounds, and correlated timing events without returned content.

`uv run python scripts/benchmark_request_latency.py` compares the shell helper
from the immutable baseline against the patched helper in the same environment.
Five samples per implementation cancel a ready subprocess that would otherwise
sleep 1.2 seconds, then time the next result from the serial executor. This is
cancellation-to-next-result latency, not general task acceleration or model TPS.
The baseline is read from Git history; run from a checkout containing that commit.

Installed-stack validation of source commit `d3862967411cec43fcc42219b0ea81f6859d68d5`
succeeded in Actions run `41046020728` on Linux with Python 3.11 and 3.13,
including full application/test type checks. The benchmark artifacts from that run
contain these five-sample medians:

| Python | Original helper | Patched helper | Reduction |
| --- | ---: | ---: | ---: |
| 3.11 | 1223.113 ms | 101.484 ms | 91.7% |
| 3.13 | 1231.036 ms | 67.524 ms | 94.5% |

The permanent workflow is read-only, checks affected-file lint/formatting, and
publishes JUnit and benchmark artifacts. It also regenerates and checks the prompt
factory after template changes and runs on pushes to `optimizations` as well as
the development branch. One-time source transformation machinery has been removed.
Benchmark samples are observations, not machine-independent performance guarantees.

Native Zed/WSL behavior, the user's actual repository/test commands, and model
latency are not reproduced by these tests. Normal long-running command time,
network-mounted filesystem delays, regex CPU time, and SDK transport flush time
are not solved by this patch.

The shared wire protocol remains 7. Runtime fingerprints cover the changed shell
and rendering code; update and restart bridges/daemon together while idle. The
investigation branch is independent of `optimizations` until explicitly merged.
