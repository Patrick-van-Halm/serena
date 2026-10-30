"""Apply the reviewed follow-up fixes to the investigation branch."""
from pathlib import Path


def edit(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, (path, old, text.count(old))
    target.write_text(text.replace(old, new), encoding="utf-8")


edit("src/serena/repl/api/shell_api.py",
     "        return self._limit_length(obj.result_.model_dump_json(), [lambda: self._preview(obj.result_)])",
     '''        full = obj.result_.model_dump_json()
        limit = self._get_max_answer_chars()
        if 0 < limit < len(full):
            preview = self._preview(obj.result_)
            if len(preview) <= limit:
                # the preview already marks truncation; generic shortening adds a non-JSON prefix
                return preview
        return self._limit_length(full)''')
edit("test/serena/test_request_latency.py",
     '@pytest.mark.parametrize("stderr", [None, "warning\\n" * 1000 + "FINAL ERROR\\n"])',
     '@pytest.mark.parametrize("stderr", [None, "warning\\n" * 1000 + "FINAL ERROR\\n"], ids=["no-stderr", "stderr"])')
edit("test/serena/test_request_latency.py",
     '''                    with anyio.fail_after(4):
                        result = await client.call_tool("read_file", {"relative_path": "hello.txt"})
                    assert not result.is_error and "SDK migration works" in text_of(result)''',
     '''                    with anyio.fail_after(4):
                        for _ in range(80):
                            result = await client.call_tool("read_file", {"relative_path": "hello.txt"})
                            if not result.is_error:
                                break
                            # the daemon refuses new work while cleanup still owns the execution guard
                            message = text_of(result)
                            assert "Shared runtime is still finishing interrupted call" in message, message
                            assert "the new operation was not submitted" in message, message
                            # retry only this read probe, never the interrupted shell or a modification
                            await anyio.sleep(0.05)
                    assert not result.is_error and "SDK migration works" in text_of(result), text_of(result)''')
path = Path("test/serena/test_request_latency.py")
text = path.read_text(encoding="utf-8")
text += '''

@pytest.mark.parametrize("content", ['"\\\\\\n\\t' * 1000, "界🙂" * 1000])
def test_preview_bound_applies_to_serialized_json(content: str) -> None:
    result = ShellCommandResult(stdout=content, stderr=content, return_code=9, cwd="/project")
    agent = SimpleNamespace(serena_config=SimpleNamespace(default_max_tool_answer_chars=500))
    rendered = ShellCommandOutput(result, ShellCommandOutputRenderer(agent)).represent()
    assert len(rendered) <= 500
    payload = json.loads(rendered)
    assert payload["return_code"] == 9 and payload["output_truncated"] is True


def test_tiny_preview_limit_preserves_existing_fallback() -> None:
    result = ShellCommandResult(stdout="output" * 100, return_code=0, cwd="/project")
    agent = SimpleNamespace(serena_config=SimpleNamespace(default_max_tool_answer_chars=10))
    rendered = ShellCommandOutput(result, ShellCommandOutputRenderer(agent)).represent()
    assert rendered.startswith("The answer is too long")


def test_latency_tracing_is_disabled_by_default(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SERENA_TRACE_TOOL_LATENCY", raising=False)
    caplog.set_level(logging.INFO, logger="serena.util.tool_call")
    call = ToolCall(5)
    call.trace("test")
    assert call.execute(lambda: "result") == "result"
    assert not any(record.getMessage().startswith("Tool latency: ") for record in caplog.records)
'''
path.write_text(text, encoding="utf-8")
print("Reviewed latency follow-up fixes applied.")
