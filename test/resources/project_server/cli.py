"""Minimal child daemon used by startup tests; no language-server or MCP dependencies."""

# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

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
                "shared_mcp_protocol_version": 4,
                "shared_mcp_build_id": os.getenv("DAEMON_TEST_BUILD", "test-build"),
            }
            if mode != "legacy":
                response["pid"] = int(os.getenv("DAEMON_TEST_PID", str(os.getpid())))
            self.wfile.write(json.dumps(response).encode())

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
