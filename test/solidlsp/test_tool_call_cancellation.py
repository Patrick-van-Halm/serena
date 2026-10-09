# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from unittest.mock import MagicMock

import pytest

from serena.util.tool_call import ToolCall, ToolCallInterrupted
from solidlsp.ls_process import LanguageServerInterface


class ControlledLanguageServer(LanguageServerInterface):
    """In-memory LSP transport exercising real request/response handling."""

    def __init__(self, request_timeout: float = 5.0):
        super().__init__(MagicMock(), lambda _: logging.INFO, request_timeout=request_timeout)
        self.messages: Queue[dict] = Queue()

    def is_running(self) -> bool:
        return True

    def _start(self) -> None:
        pass

    def _stop(self, timeout: float) -> None:
        pass

    def _send_payload(self, payload: dict) -> None:
        self.messages.put(payload)

    def respond(self, request: dict, result: str) -> None:
        self._receive_payload({"jsonrpc": "2.0", "id": request["id"], "result": result})


@pytest.mark.parametrize("cancel", [True, False])
def test_tool_interruption_cancels_lsp_request_and_allows_subsequent_work(cancel: bool) -> None:
    server = ControlledLanguageServer()
    call = ToolCall(5.0 if cancel else 0.1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(call.execute, lambda: server.send_request("textDocument/hover", {}))
        request = server.messages.get(timeout=1)
        assert request["method"] == "textDocument/hover"
        if cancel:
            call.cancel()
        with pytest.raises(ToolCallInterrupted):
            first.result(timeout=1)
        notification = server.messages.get(timeout=1)
        assert notification["method"] == "$/cancelRequest"
        assert notification["params"] == {"id": request["id"]}

        # a late response to the cancelled operation must not affect the next request
        server.respond(request, "late")
        second = pool.submit(server.send_request, "textDocument/hover", {})
        next_request = server.messages.get(timeout=1)
        server.respond(next_request, "fresh")
        assert second.result(timeout=1) == "fresh"


def test_language_server_timeout_still_applies_outside_a_tool_call() -> None:
    server = ControlledLanguageServer(request_timeout=0.01)
    with pytest.raises(TimeoutError, match="Request timed out"):
        server.send_request("textDocument/hover", {})
    request = server.messages.get(timeout=1)
    cancellation = server.messages.get(timeout=1)
    assert cancellation["params"] == {"id": request["id"]}


def test_cancelled_tool_does_not_send_a_new_lsp_request() -> None:
    server = ControlledLanguageServer()
    call = ToolCall(1)
    call.cancel()
    with pytest.raises(ToolCallInterrupted, match="outcome=not_started"):
        call.execute(lambda: server.send_request("textDocument/rename", {}))
    # exercise a new request to prove that cancelled admission has no remote effect
    with ThreadPoolExecutor(max_workers=1) as pool:
        fresh = pool.submit(server.send_request, "textDocument/hover", {})
        request = server.messages.get(timeout=1)
        assert request["method"] == "textDocument/hover"
        server.respond(request, "ok")
        assert fresh.result(timeout=1) == "ok"
