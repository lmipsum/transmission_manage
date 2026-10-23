import json
import sys

from modules.hooks import Hooks

PAYLOAD = {"command": "rem_unregistered", "hash": "a" * 40, "name": "Example", "delete_data": True}


def script(tmp_path, body):
    path = tmp_path / "hook.py"
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def test_no_hook_always_allows():
    assert Hooks({}).pre_remove(PAYLOAD) is True


def test_hook_receives_the_payload_and_allows_on_exit_0(tmp_path):
    out = tmp_path / "seen.json"
    body = f"import json, sys\nopen(r'{out}', 'w').write(sys.stdin.read())\n"
    assert Hooks({"pre_remove": script(tmp_path, body)}).pre_remove(PAYLOAD) is True
    seen = json.loads(out.read_text())
    assert seen["event"] == "pre_remove" and seen["hash"] == "a" * 40 and seen["delete_data"] is True


def test_non_zero_exit_declines(tmp_path):
    assert Hooks({"pre_remove": script(tmp_path, "import sys\nprint('keep it')\nsys.exit(3)\n")}).pre_remove(PAYLOAD) is False


def test_missing_command_declines():
    assert Hooks({"pre_remove": "definitely-not-a-real-command-xyz"}).pre_remove(PAYLOAD) is False


def test_timeout_declines(tmp_path):
    hooks = Hooks({"pre_remove": script(tmp_path, "import time\ntime.sleep(5)\n"), "timeout": 1})
    assert hooks.pre_remove(PAYLOAD) is False
