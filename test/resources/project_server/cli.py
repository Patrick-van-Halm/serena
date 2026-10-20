"""Minimal child daemon used by startup tests; no language-server or MCP dependencies."""

# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from serena.shared_mcp_recovery import DaemonRecovery, DaemonRecoveryRefusedError, RecoveryEvidence, RecoveryScope


def top_level() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start-project-server"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--log-level", required=True)
    args = parser.parse_args()

    # record each real process, its selected endpoint, and the propagated configuration home
    with Path(os.environ["DAEMON_TEST_STARTS"]).open("a") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "port": args.port, "home": os.getenv("SERENA_HOME")}) + "\n")
    mode = os.getenv("DAEMON_TEST_MODE", "ready")
    if mode == "fail":
        sys.stderr.write("diagnostic padding\n" * 10000)
        sys.stderr.write("AUTH=" + os.environ["DAEMON_TEST_SECRET"] + "\n")
        sys.stderr.write("Actual startup failure: test bind/configuration error\n")
        raise SystemExit(23)
    if mode == "hang":
        time.sleep(60)
    time.sleep(float(os.getenv("DAEMON_TEST_DELAY", "0")))

    class Source:
        completed = False

        def get_recovery_evidence(self, scope):
            if scope.session_id != "chat":
                raise DaemonRecoveryRefusedError("Wrong session")
            return RecoveryEvidence(
                "blocked-call", "replace_symbol_body" if mode == "edit" else "find_symbol",
                mode != "edit", mode in {"blocked", "recovers", "edit"} and not self.completed, 31.0,
            )

    source = Source()
    recovery = DaemonRecovery(source)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != "Bearer " + os.environ["DAEMON_TEST_SECRET"]:
                self.send_response(401)
                self.end_headers()
                return
            if self.path != "/mcp/recovery":
                self.send_response(404)
                self.end_headers()
                return
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            scope = RecoveryScope(data["project_root"], data["context"], data["session_id"])
            payload = {"pid": os.getpid()}
            try:
                if data["action"] == "observe":
                    evidence = recovery.observe(scope)
                    payload.update(state="observed", request_id=evidence.request_id)
                    if mode == "recovers":
                        source.completed = True
                elif data["action"] == "claim":
                    token = recovery.claim(scope, data["request_id"])
                    payload.update(state="claimed", token=token, request_id=data["request_id"])
                else:
                    recovery.release(scope, data["token"])
                    payload.update(state="released")
            except DaemonRecoveryRefusedError as error:
                self.send_response(409)
                self.end_headers()
                self.wfile.write(str(error).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self) -> None:
            if self.headers.get("Authorization") != "Bearer " + os.environ["DAEMON_TEST_SECRET"]:
                self.send_response(401)
                self.end_headers()
                return
            if mode.isdecimal():
                self.send_response(int(mode))
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            if mode == "invalid":
                self.wfile.write(b"not JSON")
                return
            response = {
                "status": "alive",
                "shared_mcp_protocol_version": int(os.getenv("DAEMON_TEST_PROTOCOL", "5")),
                "shared_mcp_build_id": os.getenv("DAEMON_TEST_BUILD", "test-build"),
            }
            if mode != "legacy":
                response["pid"] = int(os.getenv("DAEMON_TEST_PID", str(os.getpid())))
            self.wfile.write(json.dumps(response).encode())

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
