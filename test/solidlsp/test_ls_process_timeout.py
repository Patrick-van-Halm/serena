import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import LanguageServerInterface


class _TimeoutInterface(LanguageServerInterface):
    def __init__(self) -> None:
        super().__init__(
            ls_id=LanguageServerId.PYTHON,
            determine_log_level=lambda line: 20,
            request_timeout=0.01,
        )
        self.sent = []

    def is_running(self) -> bool:
        return True

    def _start(self) -> None:
        pass

    def _stop(self, timeout: float) -> None:
        pass

    def _send_payload(self, payload) -> None:
        self.sent.append(payload)


def test_timed_out_lsp_request_is_removed_and_cancelled() -> None:
    interface = _TimeoutInterface()

    with pytest.raises(TimeoutError):
        interface.send_request("textDocument/hover", {"x": 1})

    assert interface._pending_requests == {}
    assert any(
        payload.get("method") == "$/cancelRequest" and payload.get("params") == {"id": 1}
        for payload in interface.sent
    )
