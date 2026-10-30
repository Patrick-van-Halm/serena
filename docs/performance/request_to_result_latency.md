# Agent request-to-result latency investigation

## Scope and evidence boundary

Base: `optimizations` at `db388954b3b72d83a3411fd3d5d86b4caf883c5a`.
Branch: `investigate/request-to-result-latency`.

This is an investigation, not a production optimization or a merge request. No
runtime source, execution ordering, authentication, recovery, or model settings
are changed. The input is the user-supplied timing table for two sessions, not a
raw client/provider trace. Its date, model, provider, reasoning effort, actual
command strings, machine load, and timestamp semantics are not established.
Do not infer these from the repository revision or from a later successful run.

`request_latency_observations.json` preserves the reported times, durations, and
three untimed failures, omitting project-specific filenames and command details.
It interprets the displayed times as execution starts for interval arithmetic;
that interpretation needs confirmation against raw logs. Timestamps have only
one-second precision. The terminal `11:18:26` has no tool or duration, so it is not
invented into a final-response event.

## Findings from the supplied table

| Metric | Primary session | Delegated verification |
| --- | ---: | ---: |
| Calls with reported duration | 27 | 18 |
| Additional calls without execution timing | 3 | 0 |
| Sum of reported durations | 719.256 s | 36.143 s |
| Median reported duration | 0.390 s | 0.0205 s |
| Calls shorter than one second | 16 | 14 |
| Shell duration sum | 666.689 s | 35.846 s |
| Observed execution envelope, assuming start timestamps | about 25m 55s | about 2m 17s |
| Union of reported execution intervals | about 11m 59s | about 36s |
| Time not covered by those intervals | about 13m 56s | about 1m 41s |

These are not measurements of full user-request latency. The user submission,
first provider request, tool delivery, and final answer are missing. The primary
session's envelope extends to approximately 11:13:45.1 because the last worker
report lasts 391.1 seconds. The chat may have stopped waiting much earlier.
The later session ends at the last measured tool completion, approximately
11:18:22.262, not at an established final answer.

The interval union avoids double counting apparent overlaps. There is a 0.390 s
nested interval in the primary table and a 0.172 s overlap in verification;
second-resolution timestamps alone can explain small overlaps. They are not
proof of parallel execution on a particular executor.

### Early-request latency

The initial five instruction/memory/file calls consume **0.717 s** of reported
execution across an approximately **41 s** envelope. The analogous five calls in
verification consume **0.028 s** across approximately **17 s**. Optimizing their
server execution cannot explain or remove most of that elapsed time.

The first edit starts at 10:58:38, **10m 48s after the first recorded call**.
The preceding 19 executions total **28.781 s**. Through the third edit, immediately
before the first test command, 22 calls total **36.236 s** across approximately
11m 10s. Large unexplained intervals include:

- approximately 187 s after `src/AGENTS.md` before the next shell call;
- approximately 124 s after a source-file read before the scoped-instruction lookup;
- approximately 74 s after that lookup before the next test-file read.

Do not label these intervals "model thinking" or "low TPS". They may contain
provider latency, tool approvals, queueing before the execution logger, missing
calls, host scheduling, other agent work, or user pauses.

### Late-request latency

Shell calls represent **92.69% of the primary session's summed execution time**.
The four problematic long calls total **665.798 s**: 86.2 s, 136.5 s, 51.998 s,
and 391.1 s. Several results were oversized or failed/cancelled. The later
verification runs the three test groups in **17.935 s combined** and typecheck
in **17.172 s**. This shows a faster observed verification path, not a controlled
speedup: command equivalence, checkout state, caches, resource contention, and
process state were not held constant.

All non-shell executions in the primary table total only **52.567 s**. Making
those executions instantaneous would save at most that much observed execution
work in this trace, before dependency/overlap effects. There may be additional
unmeasured transport or queue delays; this is not a bound on all Serena latency.

## Source-level critical path

For the shared bridge, the inspected path is:

`host -> SDK tool dispatch -> AsyncToolCallRunner -> bridge HTTP request ->`
`daemon admission/runtime lookup -> agent task queue -> project guard ->`
`tool implementation -> render/stats/log/cache work -> HTTP result encoding ->`
`bridge decoding -> SDK conversion/transport -> host -> next model turn`.

The standalone MCP server skips the internal bridge HTTP leg. These are nested
spans, not independent durations to sum blindly.

### P0: shell cancellation does not interrupt the blocking shell wait

Confirmed source observations at the base revision:

- `src/serena/util/tool_call.py`, `AsyncToolCallRunner.run`, cancels the `ToolCall`
  and abandons its async waiter; remote cancellation uses a separate bounded path.
- `src/serena/util/shell.py`, `execute_shell_command`, calls
  `process.communicate(timeout=timeout)` once and never checks `ToolCallContext`.
  Process-tree cleanup is entered on its own `subprocess.TimeoutExpired` only.
- `src/serena/repl/api/shell_api.py`, `ShellApi.execute_shell_command`, derives that
  shell timeout from configured `tool_timeout - 1`, not the remaining call budget.
- `src/serena/task_executor.py` intentionally waits for the actual worker to exit
  before admitting the next serial execution. Cancelling a Future is not enough.
- `src/serena/project_server.py`, `_call_mcp_tool`, refuses new admission when an
  interrupted worker is still running. This is a safety mechanism, not a reason
  to restart the daemon automatically.

Together these create a concrete head-of-line blocking path: the client stops
waiting, but a shell process can keep the worker occupied until it completes or
its independent timeout expires. Queue time can consume the outer deadline
without reducing the inner shell timeout. This mechanism is compatible with the
reported long cancelled call; the original incident is not proven without its
request IDs, deadline and cancellation timestamps, and process trace.

Candidate fix: have the shell utility own a deadline-aware communication loop,
checking the existing call context and using the lesser of explicit shell timeout
and remaining call deadline. On cancellation, terminate/drain only the child tree
created by that invocation using the existing owned-process cleanup. Keep the
execution guard until actual cleanup completes. Do not replay a command, infer
that its edits were rolled back, kill unrelated listeners, or let a cancelled
shell overlap subsequent edits. Bounded polling need not add a fixed delay to
successful commands: pipe completion wakes the waiter.

Acceptance coverage: cancellation before launch; cancellation after child-ready;
queue time exhausting the deadline; shorter explicit timeout; large simultaneous
stdout/stderr; child descendants inheriting pipes; graceful shutdown and kill
fallback; no orphaned owned children; unchanged exit-code/content behavior;
subsequent serial work resumes only after cleanup. Test Windows separately.

`../../scripts/latency/probe_shell_cancellation.py` exercises the actual shell
utility, `ToolCall`, and serial executor with a private 1.5-second child. It records
post-cancellation delay for the next task. It is diagnostic, not a test that
requires the defect to remain. It does not execute the user's commands or touch
the user's project, Zed, daemon, or existing processes.

### P1: remove avoidable agent turns, not necessary instructions

The bootstrap and pre-edit timings make agent round trips the first measurement
priority. Keep mandatory instructions and precedence intact, but avoid separately
asking the model what to read when a host can deterministically supply the known
instruction chain and relevant command memory.

A candidate bounded bootstrap/read bundle should return individually labelled
results and errors under the existing path permissions, with a total output
budget. Preserve global/root/scoped instruction precedence and do not read child
scope files before their applicable instructions. Cache only with canonical
project/worktree, session/context, permission settings and content invalidation;
never reuse a stale instruction bundle merely because project names match.

After bootstrap, batch independent, already-known file reads into one model turn
where the host supports it. This primarily saves model round trips; the current
serial executor does not become parallel simply because calls arrive together.
Do not batch dependent edits or launch parallel tests/builds against shared
mutable output directories.

A short deterministic verification plan can run the three targeted test groups,
typecheck, and formatter once with explicit non-watch commands and compact
summaries. Supply this plan to a verifier with a bounded handoff rather than
making it rediscover the repository. Verify model/effort inheritance before
assuming delegation is faster. The supplied verification is a different task,
not evidence of a faster inference model.

### P1: reduce expensive searches and unusable outputs at the producer

`src/serena/repl/api/fs_api.py`, `FsApi.search_for_pattern`, completes
`project.search_project_files_for_pattern(...)` before constructing its renderer.
`max_answer_chars` goes to the renderer, not the search. Lowering the returned
character limit therefore does not bound scanning/matching work.

The existing `PatternMatchesRenderer` already estimates candidate sizes and has
progressive summaries; do not propose that existing optimization as a new fix.
The remaining candidate is a producer-side, explicit bounded search mode with
file/match/time budgets and honest `truncated`/continuation information. Preserve
regex semantics, ignore rules, access restrictions and cancellation. Never return
an incomplete search as proof that a symbol or reference does not exist.

Immediately prefer the known domain/path and file glob instead of a broad root
search. In this table the broad search is 21.868 s, while the three later scoped
searches are 0.946 s, 1.184 s and 0.206 s. These are different queries, not an A/B
benchmark. There is no evidence requiring a speculative language-server preload
for basic text retrieval.

`src/serena/repl/api/shell_api.py`, `ShellCommandOutputRenderer`, limits output
only after command completion and JSON serialization. The utility collects
stdout/stderr in memory. A bounded stdout/stderr spool with a compact summary,
exit code, truncation indicator and controlled log-tail retrieval would reduce
wasted retries and output pressure. Keep detailed logs access-controlled and do
not discard failure diagnostics just to make responses small.

### P2: measure post-processing and transport before micro-optimizing

`Tool.apply_ex` performs usage recording, full textual result logging, and
`save_all_caches(force=False)` before returning. Each belongs in its own span;
conditional cache saves are not proven slow by this trace. Move or bound work
only after its contribution is measured, retaining cache correctness.

`SharedMCPDaemonClient.call_tool` uses top-level `requests.post` rather than a
long-lived per-worker connection pool. Connection reuse is a candidate for warm
loopback calls, but there is no evidence it explains multi-minute gaps. Pooling
must respect concurrent workers and response closure and must not add retries to
non-idempotent tool POSTs. Likewise, do not increase the 16 worker slots or remove
project serialization as a shortcut to "higher TPS".

## TPS: what is and is not measurable

Serena receives tool invocations and returns tool results; the MCP dispatch path
does not run the host's inference model. `SerenaAgent.record_tool_usage` receives
stringified tool arguments/results, not provider generation timestamps or the
full model request. Even an accurate tokenizer over tool results cannot turn
that into model TPS. The supplied logs provide neither model identity nor token
counts, so **actual model TPS is unavailable**.

Measure separate quantities at the host/provider boundary:

| Metric | Definition |
| --- | --- |
| User request-to-result | Final answer rendered minus user request submitted |
| Time to first visible token | First content token received minus model request sent |
| Model request latency | Provider response completed minus model request sent |
| Visible streaming rate | Tokens after the first content chunk divided by first-to-last content-chunk duration |
| Effective request throughput | Provider-reported output tokens divided by complete model request latency |
| Tool request-to-result | Host receives tool result minus host dispatches the tool |
| Cancellation drain | Actual worker exit minus cancellation receipt |

A chunk is not a token. Use the provider's tokenizer and matching streamed-token
counts, account for the first chunk, and treat single-chunk responses as
unmeasurable streaming rate. Keep hidden reasoning-token accounting separate;
provider output usage can include reasoning that is not visible in the stream.
Effective throughput includes waiting/prefill/reasoning and is not pure decode
speed. Do not compare different tokenizers or service tiers as though they were
identical workloads.

A model with higher TPS can still finish later if it generates more tokens,
performs extra tool turns, retries, or waits behind a long-running command. The
optimization objective is **time to a correct, verified result**, not raw TPS.
Lower effort/faster verification models are A/B candidates for procedural tasks,
not a reason to remove reasoning from risky edits or change the main model
without measuring correctness.

For native Zed Agent, current documentation exposes separate subagent model,
thinking, effort and speed settings. Its defaults inherit parent settings.
These controls do not configure external ACP agents; those generally own their
model configuration. Establish which path produced this trace before changing
settings. No settings were changed in this investigation.

## Instrumentation and benchmark plan

Carry a trace/session/request ID from host dispatch through bridge, daemon queue,
worker and returned result. Log structured events for:

`user_submit`, `model_send`, `first_content`, `model_end`, `tool_dispatch`,
`bridge_receive`, `worker_admitted`, `http_send`, `daemon_receive`, `runtime_ready`,
`task_enqueue`, `task_start`, `guard_acquired`, `apply_end`, `render_end`,
`stats_end`, `cache_end`, `response_encoded`, `host_tool_result`, `cancel_received`,
`worker_exit`, and `final_render`.

Some current tools render inside `apply`, so isolate that as a child span rather
than pretending it occurs after `apply_end`. Use monotonic durations inside each
process and correlate boundaries by request ID. Do not subtract unrelated
monotonic clocks across processes/machines, or sum nested transport/worker spans.
Log request IDs/outcomes even when no task starts. `unknown` on the bridge means
possible dispatch, not proof of language-server execution.

Record provider/model/version, effort/speed, parent/subagent identity, input and
output tokens, reasoning tokens where exposed, cached input tokens, retries,
approval delays, result bytes and truncation. Avoid raw prompts, secrets, tool
arguments and file contents in performance telemetry. Keep it opt-in, bounded
and locally retained. Instrument client-completed and worker-exited separately.

Benchmark the same checkout and command list with correctness checks, cold/warm
runs, and fixed provider/model/effort. Collect p50/p95 by stage across repeated
samples, not only averages from different tasks. Test one agent and 2/4 agents
sharing a project, then independent projects. Include bootstrap, a small read,
scoped/broad search, targeted tests, cancellation behind a queued read, and native
image output. Record CPU/RSS/I/O and live child processes during long commands.

Priority order:

1. Establish host/provider and queue/worker timings; reproduce shell cancellation.
2. Fix cancellation drain and remaining-deadline propagation with owned cleanup.
3. Reduce sequential bootstrap/retrieval turns and repeated verification discovery.
4. Add bounded producer-side search and useful bounded shell output.
5. Optimize measured serialization, logging, cache-save and connection overhead.

## Reproduction and validation

From the repository root:

```sh
python scripts/latency/analyze.py docs/performance/request_latency_observations.json
python -m unittest discover -s scripts/latency -p 'test_*.py' -v
uv run python scripts/latency/probe_shell_cancellation.py
```

The offline report uses only the Python standard library. Its tests cover interval
union, session separation, missing timings, zero/unknown distinctions, invalid
values and reproduction of the supplied totals. It explicitly outputs null for
model TPS and full user-request latency.

The branch's read-only `Request latency investigation` workflow installs locked
application dependencies on Python 3.11 and 3.13, checks script lint/format, runs
the report tests and executes the real-component cancellation probe. Probe JSON
artifacts report observations rather than declaring the original incident solved.
No full language-server suite, native Zed session or WSL networking benchmark is
implied by these checks.

## External primary references

- OpenAI, [Latency optimization](https://developers.openai.com/api/docs/guides/latency-optimization): fewer generated tokens and fewer sequential model requests are distinct levers from inference rate.
- Python, [subprocess communication](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.communicate): communication waits for child completion; timeout handling and child termination are separate responsibilities.
- Zed, [Agent settings](https://zed.dev/docs/ai/agent-settings): native subagent model/effort/speed configuration and external-agent boundary.
- Zed, [Zed Agent](https://zed.dev/docs/ai/zed-agent): native, external ACP and terminal-agent configuration ownership.

Repository observations above are pinned to the base revision, not assumed to
describe an unverified installed Serena build in the supplied session.
