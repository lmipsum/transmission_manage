import json
import sys

from modules.hooks import Hooks

PAYLOAD = {"command": "rem_unregistered", "hash": "a" * 40, "name": "Example", "delete_data": True}


def script(tmp_path, body):
    path = tmp_path / "hook.py"
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def test_no_hook_always_allows():
    assert Hooks({}).pre_remove(PAYLOAD) == (True, True)


def test_hook_receives_the_payload_and_allows_on_exit_0(tmp_path):
    out = tmp_path / "seen.json"
    body = f"import json, sys\nopen(r'{out}', 'w').write(sys.stdin.read())\n"
    assert Hooks({"pre_remove": script(tmp_path, body)}).pre_remove(PAYLOAD) == (True, True)
    seen = json.loads(out.read_text())
    assert seen["event"] == "pre_remove" and seen["hash"] == "a" * 40 and seen["delete_data"] is True


def test_non_zero_exit_declines(tmp_path):
    assert Hooks({"pre_remove": script(tmp_path, "import sys\nprint('keep it')\nsys.exit(3)\n")}).pre_remove(PAYLOAD)[0] is False


def test_missing_command_declines():
    assert Hooks({"pre_remove": "definitely-not-a-real-command-xyz"}).pre_remove(PAYLOAD)[0] is False


def test_timeout_declines(tmp_path):
    hooks = Hooks({"pre_remove": script(tmp_path, "import time\ntime.sleep(5)\n"), "timeout": 1})
    assert hooks.pre_remove(PAYLOAD)[0] is False


def test_pre_remove_can_keep_the_files(tmp_path):
    body = "import json\nprint('checking')\nprint(json.dumps({'delete_data': False}))\n"
    assert Hooks({"pre_remove": script(tmp_path, body)}).pre_remove(PAYLOAD) == (True, False)


def test_pre_remove_can_never_turn_a_keep_into_a_delete(tmp_path):
    body = "import json\nprint(json.dumps({'delete_data': True}))\n"
    keep = {**PAYLOAD, "delete_data": False}
    assert Hooks({"pre_remove": script(tmp_path, body)}).pre_remove(keep) == (True, False)


def test_pre_remove_ignores_output_that_is_not_a_json_object(tmp_path):
    body = "print('[false]')\nprint('done')\n"
    assert Hooks({"pre_remove": script(tmp_path, body)}).pre_remove(PAYLOAD) == (True, True)


BATCH = {"command": "rem_unregistered", "dry_run": False, "torrents": [{"hash": "a" * 40, "name": "Example"}]}


def test_no_batch_hook_always_allows():
    assert Hooks({}).pre_remove_batch(BATCH) is True


def test_batch_hook_receives_every_planned_removal(tmp_path):
    out = tmp_path / "seen.json"
    body = f"import sys\nopen(r'{out}', 'w').write(sys.stdin.read())\n"
    assert Hooks({"pre_remove_batch": script(tmp_path, body)}).pre_remove_batch(BATCH) is True
    seen = json.loads(out.read_text())
    assert seen["event"] == "pre_remove_batch" and seen["torrents"] == BATCH["torrents"]


def test_batch_hook_declines_on_non_zero_exit_or_timeout(tmp_path):
    assert Hooks({"pre_remove_batch": script(tmp_path, "import sys\nsys.exit(1)\n")}).pre_remove_batch(BATCH) is False
    slow = Hooks({"pre_remove_batch": script(tmp_path, "import time\ntime.sleep(5)\n"), "timeout": 1})
    assert slow.pre_remove_batch(BATCH) is False


CLASSIFY = {"command": "rem_unregistered", "hash": "a" * 40, "name": "Example", "trackers": [{"host": "h", "message": "m"}]}


def test_no_classify_hook_never_marks_unregistered():
    assert Hooks({}).classify_unregistered(CLASSIFY) is False


def test_classify_hook_marks_unregistered_on_exit_0(tmp_path):
    assert Hooks({"classify_unregistered": script(tmp_path, "pass\n")}).classify_unregistered(CLASSIFY) is True
    assert (
        Hooks({"classify_unregistered": script(tmp_path, "import sys\nsys.exit(1)\n")}).classify_unregistered(CLASSIFY) is False
    )
