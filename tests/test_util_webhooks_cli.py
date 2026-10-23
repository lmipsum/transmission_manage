import json
import logging
import textwrap
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

import transmission_manage
from modules.webhooks import Webhooks

# --- webhooks -----------------------------------------------------------------------------------------


class FakeHook:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.bodies = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                hook.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(hook.statuses.pop(0) if hook.statuses else 200)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_webhook_retries_server_errors_then_succeeds():
    hook = FakeHook([500, 502, 200])
    try:
        assert Webhooks({"error": hook.url}, sleep=lambda s: None).error("boom")
        assert len(hook.bodies) == 3
        assert hook.bodies[0]["function"] == "run_error"
    finally:
        hook.close()


def test_webhook_does_not_retry_client_errors():
    hook = FakeHook([400])
    try:
        assert not Webhooks({"run_start": hook.url}, sleep=lambda s: None).run_start(0, True)
        assert len(hook.bodies) == 1
    finally:
        hook.close()


def test_webhook_failure_never_raises():
    hooks = Webhooks({"error": "http://127.0.0.1:9/x"}, timeout=1, sleep=lambda s: None)
    assert hooks.error("boom") is False
    assert Webhooks({}).error("nothing configured") is False


def test_function_webhook_goes_to_the_function_named_in_it():
    hook = FakeHook([200, 200])
    try:
        hooks = Webhooks({"function": {"share_limits": hook.url}}, sleep=lambda s: None)
        assert hooks.function("cleanup_share_limits", {"torrents": ["A"]})
        assert hooks.function("share_limits", {"torrents": ["B"]})
        assert not hooks.function("rem_orphaned", {"orphaned_files": []})
        assert [body["function"] for body in hook.bodies] == ["cleanup_share_limits", "share_limits"]
    finally:
        hook.close()


def test_webhook_error_body_is_redacted():
    hook = FakeHook([200])
    try:
        Webhooks({"error": hook.url}).error("bad https://e.org/announce?authkey=s3cret")
        assert "s3cret" not in json.dumps(hook.bodies)
    finally:
        hook.close()


# --- CLI ----------------------------------------------------------------------------------------------


def config_file(tmp_path, host, extra=""):
    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent(f"transmission:\n  host: '{host}'\n  retries: 0\n  timeout: 5\n{extra}"), encoding="utf-8")
    return str(path)


def test_cli_runs_and_skips_unported_commands(tmp_path, jsonrpc_server, caplog):
    path = config_file(tmp_path, jsonrpc_server.host, "commands:\n  dry_run: true\n  rem_unregistered: true\n")
    caplog.set_level(logging.INFO, logger="transmission_manage")
    logging.getLogger("transmission_manage").propagate = True
    try:
        rc = transmission_manage.main(["-c", path])
    finally:
        logging.getLogger("transmission_manage").propagate = False
    assert rc == 0


def test_cli_bad_config_exits_2(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("transmission:\n  timeout: soon\n", encoding="utf-8")
    assert transmission_manage.main(["-c", str(path)]) == 2


def test_cli_unreachable_daemon_exits_1_and_reports(tmp_path):
    hook = FakeHook([200])
    try:
        extra = f"webhooks:\n  error: '{hook.url}'\n"
        assert transmission_manage.main(["-c", config_file(tmp_path, "127.0.0.1:9", extra)]) == 1
        assert hook.bodies and hook.bodies[0]["function"] == "run_error"
    finally:
        hook.close()


def test_cli_version(capsys):
    assert transmission_manage.main(["--version"]) == 0
    assert transmission_manage.__version__ in capsys.readouterr().out
