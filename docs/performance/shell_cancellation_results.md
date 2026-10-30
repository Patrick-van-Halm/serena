# Measured shell cancellation and serial-queue delay

This supplements [the request-to-result investigation](request_to_result_latency.md).
It is a controlled component experiment, not a reproduction of the user's shell
commands or a measurement of model TPS.

## Provenance

Measurements were collected by the read-only [GitHub Actions run 41042409691](https://github.com/Patrick-van-Halm/serena/actions/runs/41042409691)
at investigation commit `54ef5bb5061143fa32ea0cef6961ea4e08e994ea`, using locked
application dependencies. Production source at that commit is unchanged from
`optimizations` commit `db388954b3b72d83a3411fd3d5d86b4caf883c5a`.

Both jobs completed lint, all five analyzer tests, input analysis, and the
component probe. That run's overall failure was exclusively script formatting;
the resulting formatting-only corrections do not change the probe behavior.
The current branch's workflow retains the strict formatting check.

The experiment calls the real `ToolCall`, `TaskExecutor` and shell utility. Each
iteration creates its own temporary directory and Python child, waits for the
child's readiness marker, cancels the call, and submits a following serial task.
The child sleeps for 1.5 seconds; the shell has an independent 3-second timeout.
No existing daemon, project process, provider account or editor session is used.

## Observations

| Python | Iteration | Cancellation after task submission | Following task starts after cancellation |
| --- | ---: | ---: | ---: |
| 3.11.18 | 1 | 0.111385 s | 1.512933 s |
| 3.11.18 | 2 | 0.111011 s | 1.514550 s |
| 3.11.18 | 3 | 0.111142 s | 1.515410 s |
| 3.13.18 | 1 | 0.142070 s | 1.521016 s |
| 3.13.18 | 2 | 0.141805 s | 1.520810 s |
| 3.13.18 | 3 | 0.141932 s | 1.522266 s |

In **all six iterations**, the following task was still waiting 200 milliseconds
after cancellation. The child subsequently printed its completion marker and the
shell utility returned normally. The following task waited essentially the full
remaining lifetime of the child, not merely cancellation-notification overhead.

Raw per-job output and JSON artifacts:

- [Python 3.11 job 124018162403](https://github.com/Patrick-van-Halm/serena/actions/runs/41042409691/job/124018162403), artifact `request-latency-3.11`.
- [Python 3.13 job 124018161745](https://github.com/Patrick-van-Halm/serena/actions/runs/41042409691/job/124018161745), artifact `request-latency-3.13`.

## Interpretation and limits

This confirms that marking an already-running shell call cancelled does not
interrupt its blocking shell wait at the inspected revision, and that the serial
executor remains occupied until the child finishes. It strengthens the priority
of deadline-aware owned-child cleanup described in the main investigation.

It does **not** establish the exact cause of the supplied 391.1-second operation,
measure cancellation delivery through MCP/HTTP, or justify cancelling unrelated
processes. It is not an implemented performance improvement or a benchmark of
Python versions: the two jobs ran on different hosted workers, and six samples
are insufficient for meaningful p95 claims.

The actual worker exit remains the safe admission boundary for subsequent edits.
The proposed improvement is to make cancellation reach owned child execution and
cleanup promptly, not to remove that boundary or replay commands after a timeout.
