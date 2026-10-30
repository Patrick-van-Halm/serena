"""Apply reviewed, assertion-checked latency instrumentation and output changes."""
from pathlib import Path


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if new in text:
        return
    assert text.count(old) == count, (path, old, text.count(old))
    target.write_text(text.replace(old, new), encoding="utf-8")


edit("src/serena/util/tool_call.py", "import logging\nimport math\n", "import json\nimport logging\nimport math\nimport os\n")
edit("src/serena/util/tool_call.py", "        self._deadline = time.monotonic() + timeout\n", "        self._created_at = time.monotonic()\n        self._deadline = self._created_at + timeout\n        self._trace_enabled = os.environ.get('SERENA_TRACE_TOOL_LATENCY', '').lower() in {'1', 'true', 'yes'}\n        self._trace_span = uuid4().hex if self._trace_enabled else ''\n")
edit("src/serena/util/tool_call.py", "    @property\n    def remaining(self) -> float:\n", '''    def trace(self, event: str, *, tool_name: str | None = None, session_id: str | None = None) -> None:
        """Emit opt-in timing metadata without tool arguments, content, or exception messages."""
        if not self._trace_enabled:
            return
        record = {
            "event": event,
            "request_id": self.request_id,
            "span_id": self._trace_span,
            "pid": os.getpid(),
            "elapsed_ms": round((time.monotonic() - self._created_at) * 1000, 3),
            "remaining_ms": round(self.remaining * 1000, 3),
            "local_outcome": self.outcome,
        }
        if tool_name is not None:
            record["tool"] = tool_name
        if session_id is not None:
            record["session_id"] = session_id
        log.info("Tool latency: %s", json.dumps(record, separators=(",", ":")))

    @property
    def remaining(self) -> float:
''')
edit("src/serena/util/tool_call.py", "        try:\n            # expose the call to cooperative downstream operations", "        self.trace('executor_dequeued')\n        try:\n            # expose the call to cooperative downstream operations")
edit("src/serena/util/tool_call.py", "                    self.check()\n                    result = function()", "                    self.check()\n                    self.trace('execution_started')\n                    result = function()")
edit("src/serena/util/tool_call.py", "                self._finished = True\n\n    def wait", "                self._finished = True\n            self.trace('execution_finished')\n\n    def wait")
edit("src/serena/util/tool_call.py", "        call = ToolCall(timeout)\n\n        def invoke", "        call = ToolCall(timeout)\n        call.trace('mcp_received')\n\n        def invoke")
edit("src/serena/util/tool_call.py", "                with ToolCallContext(call).activate():\n                    return function()", "                call.trace('worker_admitted')\n                with ToolCallContext(call).activate():\n                    return function()")
edit("src/serena/util/tool_call.py", "            call.check()\n            raise\n\n    @classmethod", "            call.check()\n            raise\n        finally:\n            call.trace('mcp_finished')\n\n    @classmethod")
edit("src/serena/tools/tools_base.py", "                # record only a textual summary of binary content", "                call_context.call.trace('apply_finished', tool_name=self.get_name(), session_id=session_id)\n\n                # record only a textual summary of binary content")
edit("src/serena/tools/tools_base.py", "        call_context = ToolCallContext.current() or ToolCallContext(ToolCall(float(timeout)))\n", "        call_context = ToolCallContext.current() or ToolCallContext(ToolCall(float(timeout)))\n        call_context.call.trace('tool_received', tool_name=self.get_name(), session_id=session_id)\n")
edit("src/serena/tools/tools_base.py", "            tool_call_error = ToolCallError(msg)\n        if catch_exceptions:", "            tool_call_error = ToolCallError(msg)\n        finally:\n            call_context.call.trace('tool_wait_finished', tool_name=self.get_name(), session_id=session_id)\n        if catch_exceptions:")
edit("src/serena/shared_mcp_calls.py", "        call = ToolCall(timeout, request_id)\n        call.check()", "        call = ToolCall(timeout, request_id)\n        call.trace('daemon_received', session_id=session_id)\n        call.check()")
edit("src/serena/shared_mcp_protocol.py", '    "serena/util/tool_call.py",\n', '    "serena/util/tool_call.py",\n    "serena/util/shell.py",\n    "serena/repl/api/shell_api.py",\n')
edit("src/serena/repl/api/shell_api.py", "import os.path\n", "import json\nimport os.path\n")
edit("src/serena/repl/api/shell_api.py", '''class ShellCommandOutputRenderer(Renderer[ShellCommandOutput]):
    def render(self, obj: ShellCommandOutput) -> str:
        return self._limit_length(obj.result_.model_dump_json())
''', '''class ShellCommandOutputRenderer(Renderer[ShellCommandOutput]):
    """Preserve exit status and useful excerpts when complete shell output will not fit."""

    @staticmethod
    def _excerpt(text: str | None, retained: int) -> str | None:
        if text is None or len(text) <= retained:
            return text
        if retained == 0:
            return ""
        head = retained // 3
        tail = retained - head
        return text[:head] + "\\n... output omitted ...\\n" + text[-tail:]

    def _preview(self, result: ShellCommandResult) -> str:
        # fit serialized JSON rather than estimating unescaped string length
        limit = self._get_max_answer_chars()
        stdout_count = len(result.stdout)
        stderr_count = len(result.stderr or "")

        def encode(retained: int) -> str:
            stdout_budget = min(stdout_count, retained // 2)
            stderr_budget = min(stderr_count, retained - stdout_budget)
            stdout_budget = min(stdout_count, retained - stderr_budget)
            payload = result.model_dump()
            payload.update(
                stdout=self._excerpt(result.stdout, stdout_budget),
                stderr=self._excerpt(result.stderr, stderr_budget),
                output_truncated=True,
                stdout_original_chars=stdout_count,
                stderr_original_chars=stderr_count,
            )
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        low, high = 0, max(0, min(limit, stdout_count + stderr_count))
        best = encode(0)
        while low <= high:
            retained = (low + high) // 2
            candidate = encode(retained)
            if len(candidate) <= limit:
                best = candidate
                low = retained + 1
            else:
                high = retained - 1
        return best

    def render(self, obj: ShellCommandOutput) -> str:
        return self._limit_length(obj.result_.model_dump_json(), [lambda: self._preview(obj.result_)])
''')
edit("src/serena/tools/cmd_tools.py", '''        :param max_answer_chars: if the output is longer than this number of characters,
            no content will be returned. -1 means using the default value, don't adjust unless there is no other way to get the content
            required for the task.
''', '''        :param max_answer_chars: maximum response length; -1 uses the configured default.
            Oversized output is shortened to marked excerpts while retaining the exit status and working directory
            whenever that metadata fits. Do not rerun a command solely because its output was shortened.
''')
print("Latency changes applied.")
