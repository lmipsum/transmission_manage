import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TORRENTS = [
    {
        "id": 1,
        "hashString": "a" * 40,
        "name": "Example.Release.One",
        "status": 6,
        "uploadRatio": -2,
        "trackerStats": [{"host": "tracker-one.example.org:443", "lastAnnounceResult": "Success", "isBackup": False}],
    },
    {"id": 2, "hashString": "b" * 40, "name": "Example.Release.Two", "status": 0, "uploadRatio": 0.5, "trackerStats": []},
]


def _to_snake(name):
    import re

    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class FakeTransmission:
    """A tiny Transmission RPC emulator: JSON-RPC 2.0 (4.1+) or the legacy protocol, with the 409 handshake."""

    def __init__(self, mode):
        self.mode = mode  # "jsonrpc" | "legacy"
        self.requests = []
        self.session_id = "sid-1"
        self.expire_session_after = None  # rotate the session id after N successful requests
        self.served = 0
        self.torrents = [dict(t) for t in TORRENTS]
        self.fail_with = None  # (http_code, None) to force an HTTP error
        self.jsonrpc_error = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append({"headers": dict(self.headers), "body": body})
                if server.fail_with:
                    self.send_response(server.fail_with)
                    self.end_headers()
                    return
                if self.headers.get("X-Transmission-Session-Id") != server.session_id:
                    self.send_response(409)
                    self.send_header("X-Transmission-Session-Id", server.session_id)
                    if server.mode == "jsonrpc":
                        self.send_header("X-Transmission-Rpc-Version", "6.0.0")
                    self.end_headers()
                    return
                server.served += 1
                if server.expire_session_after and server.served >= server.expire_session_after:
                    server.session_id = "sid-2"
                    server.expire_session_after = None
                reply = server.reply(body)
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def reply(self, body):
        if self.mode == "jsonrpc":
            if "jsonrpc" not in body:
                return {"result": "method name not recognized"}
            if self.jsonrpc_error:
                return {"jsonrpc": "2.0", "error": self.jsonrpc_error, "id": body.get("id")}
            method, params = body["method"], body.get("params", {})
            return {"jsonrpc": "2.0", "result": self.result(method, params, snake=True), "id": body.get("id")}
        if "jsonrpc" in body and "arguments" not in body:
            return {"result": "method name not recognized"}
        method, params = body["method"], body.get("arguments", {})
        return {"result": "success", "arguments": self.result(method, params, snake=False), "tag": body.get("tag")}

    def result(self, method, params, snake):
        def keys(obj):
            if isinstance(obj, dict):
                return {(_to_snake(k) if snake else k): keys(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [keys(v) for v in obj]
            return obj

        if method in ("session_get", "session-get"):
            return {"version": "4.1.3 (fake)", "rpc_version_semver" if snake else "rpc-version-semver": "6.0.0"}
        if method in ("torrent_get", "torrent-get"):
            torrents = self.torrents
            if "ids" in params:
                torrents = [t for t in torrents if t["hashString"] in params["ids"] or t["id"] in params["ids"]]
            return {"torrents": keys(torrents)}
        return {}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def jsonrpc_server():
    server = FakeTransmission("jsonrpc")
    yield server
    server.close()


@pytest.fixture
def legacy_server():
    server = FakeTransmission("legacy")
    yield server
    server.close()
