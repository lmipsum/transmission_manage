import textwrap
import time

import pytest

import transmission_manage
from modules.config import Config
from modules.core import remove_unregistered as ru
from modules.util import list_in_text

NOW = 1_800_000_000.0
UNREG = "Unregistered torrent"


def tracker(result="Success", succeeded=None, announced=True, state=1, backup=False, host="t1.example.org:443"):
    return {
        "announce": f"https://{host}/announce",
        "host": host,
        "is_backup": backup,
        "has_announced": announced,
        "announce_state": state,
        "last_announce_succeeded": (result == "Success") if succeeded is None else succeeded,
        "last_announce_result": result,
    }


def torrent(n, trackers, labels=None, added=NOW - 86400, done=1.0, files=None, name=None, download_dir="/data"):
    name = name or f"Release.{n}"
    return {
        "hash_string": f"{n:040x}",
        "name": name,
        "status": 6,
        "added_date": added,
        "percent_done": done,
        "labels": labels or [],
        "tracker_stats": trackers,
        "download_dir": download_dir,
        "files": files if files is not None else [{"name": f"{name}/file.mkv"}],
    }


class FakeClient:
    def __init__(self, torrents):
        self.items = torrents
        self.calls = []

    def torrents(self, fields, hashes=None):
        return [dict(t) for t in self.items]

    def set_labels(self, hashes, labels):
        self.calls.append(("set_labels", tuple(hashes), tuple(labels)))

    def remove(self, hashes, delete_data=False):
        self.calls.append(("remove", tuple(hashes), delete_data))


class Webhooks:
    def __init__(self):
        self.sent = []

    def function(self, name, payload):
        self.sent.append((name, payload))


class Hooks:
    def __init__(self, allow=True):
        self.allow = allow
        self.payloads = []

    def pre_remove(self, payload):
        self.payloads.append(payload)
        return self.allow


def make_context(tmp_path, torrents, commands=None, settings="", hooks=None):
    cmd = {"rem_unregistered": True, "tag_tracker_error": False, "dry_run": False}
    cmd.update(commands or {})
    path = tmp_path / "config.yml"
    path.write_text("transmission: {}\n" + textwrap.dedent(settings), encoding="utf-8")
    config = Config(str(path), cmd)
    client = FakeClient(torrents)
    return transmission_manage.Context(client, config, Webhooks(), hooks or Hooks()), client


def run(ctx):
    return ru.RemoveUnregistered(ctx, now=lambda: NOW).run()


# --- classification -----------------------------------------------------------------------------------


def test_one_working_tracker_means_the_torrent_is_working():
    state, _ = ru.classify_trackers([tracker(UNREG, host="a:1"), tracker("Success", host="b:1")])
    assert state == ru.WORKING


@pytest.mark.parametrize("pending", [tracker(announced=False, result=""), tracker("x", succeeded=False, state=3)])
def test_a_tracker_not_yet_reported_is_inconclusive(pending):
    state, _ = ru.classify_trackers([tracker(UNREG, host="a:1"), pending])
    assert state == ru.INCONCLUSIVE


def test_backup_trackers_and_non_announce_urls_are_ignored():
    dht = {"announce": "dht://x", "has_announced": True, "last_announce_succeeded": True}
    state, failing = ru.classify_trackers([tracker(UNREG), tracker("Success", backup=True), dht])
    assert state == ru.FAILING and len(failing) == 1


def test_tracker_down_messages_are_left_out():
    state, failing = ru.classify_trackers([tracker("Tracker is down", succeeded=False)])
    assert state == ru.FAILING and failing == []


def test_ignore_messages_win():
    assert ru.find_unregistered([tracker("Unregistered passkey", succeeded=False)]) is None
    assert ru.find_unregistered([tracker(UNREG, succeeded=False)]) is not None


def test_list_in_text_matches_whole_words_and_phrases():
    assert list_in_text("TRACKER IS DOWN", ["DOWN"])
    assert not list_in_text("DOWNLOAD LIMIT", ["DOWN"])
    assert list_in_text("TORRENT NOT FOUND ON TRACKER", ["TORRENT NOT FOUND"])


# --- removal (upstream behaviour) ---------------------------------------------------------------------


def test_unregistered_torrent_is_deleted_with_its_contents(tmp_path):
    ctx, client = make_context(tmp_path, [torrent(1, [tracker(UNREG)]), torrent(2, [tracker()])])
    stats = run(ctx)
    assert client.calls == [("remove", (f"{1:040x}",), True)]
    assert stats["deleted_contents"] == 1
    assert ctx.webhooks.sent[0][0] == "rem_unregistered"


def test_cross_seed_with_a_working_same_name_torrent_keeps_the_contents(tmp_path):
    files = [{"name": "Show/file.mkv"}]
    items = [torrent(1, [tracker(UNREG)], name="Show", files=files), torrent(2, [tracker()], name="Show", files=files)]
    ctx, client = make_context(tmp_path, items)
    assert run(ctx)["deleted"] == 1
    assert client.calls == [("remove", (f"{1:040x}",), False)]


def test_cross_seed_without_a_working_same_name_torrent_deletes_the_contents(tmp_path):
    files = [{"name": "Show/file.mkv"}]
    items = [torrent(1, [tracker(UNREG)], name="Show", files=files), torrent(2, [tracker(UNREG)], name="Show", files=files)]
    ctx, client = make_context(tmp_path, items)
    run(ctx)
    assert sorted(client.calls) == [("remove", (f"{1:040x}",), True), ("remove", (f"{2:040x}",), True)]


def test_ignore_list_is_matched_against_the_upper_cased_message(tmp_path):
    upper = "settings:\n  rem_unregistered_ignore_list:\n    - UNREGISTERED TORRENT\n"
    ctx, client = make_context(tmp_path, [torrent(1, [tracker(UNREG)])], settings=upper)
    run(ctx)
    assert client.calls == []


def test_grace_period_protects_new_torrents(tmp_path):
    ctx, client = make_context(tmp_path, [torrent(1, [tracker(UNREG)], added=NOW - 120)])
    run(ctx)
    assert client.calls == []


def test_filter_completed_skips_incomplete_torrents(tmp_path):
    settings = "settings:\n  rem_unregistered_filter_completed: true\n"
    ctx, client = make_context(tmp_path, [torrent(1, [tracker(UNREG)], done=0.4)], settings=settings)
    run(ctx)
    assert client.calls == []


def test_per_tracker_limit_skips_the_excess(tmp_path):
    settings = "settings:\n  rem_unregistered_max_torrents: 2\n"
    ctx, client = make_context(tmp_path, [torrent(n, [tracker(UNREG)]) for n in range(1, 5)], settings=settings)
    run(ctx)
    assert len(client.calls) == 2


def test_dry_run_changes_nothing(tmp_path):
    ctx, client = make_context(tmp_path, [torrent(1, [tracker(UNREG)])], commands={"dry_run": True})
    assert run(ctx)["deleted_contents"] == 1
    assert client.calls == []


# --- pre_remove hook ----------------------------------------------------------------------------------


def test_pre_remove_hook_sees_the_planned_removal(tmp_path):
    hooks = Hooks()
    ctx, _ = make_context(tmp_path, [torrent(1, [tracker(UNREG)])], hooks=hooks)
    run(ctx)
    payload = hooks.payloads[0]
    assert payload["hash"] == f"{1:040x}" and payload["delete_data"] is True and payload["status"] == UNREG


def test_declined_removal_is_skipped_and_does_not_count_against_the_limit(tmp_path):
    hooks = Hooks(allow=False)
    settings = "settings:\n  rem_unregistered_max_torrents: 1\n"
    items = [torrent(1, [tracker(UNREG)]), torrent(2, [tracker(UNREG)])]
    ctx, client = make_context(tmp_path, items, settings=settings, hooks=hooks)
    stats = run(ctx)
    assert client.calls == [] and stats["deleted_contents"] == 0
    assert len(hooks.payloads) == 2  # the first refusal did not use up the per-tracker slot


# --- tracker error tags -------------------------------------------------------------------------------


def test_failing_torrent_gets_the_error_tag(tmp_path):
    items = [torrent(1, [tracker("Tracker gave HTTP response code 500", succeeded=False)])]
    ctx, client = make_context(tmp_path, items, commands={"rem_unregistered": False, "tag_tracker_error": True})
    assert run(ctx)["tagged"] == 1
    assert client.calls == [("set_labels", (f"{1:040x}",), ("issue",))]


def test_error_tag_is_removed_once_the_tracker_works_again(tmp_path):
    items = [torrent(1, [tracker()], labels=["issue", "keep"])]
    ctx, client = make_context(tmp_path, items, commands={"tag_tracker_error": True})
    assert run(ctx)["untagged"] == 1
    assert client.calls == [("set_labels", (f"{1:040x}",), ("keep",))]


# --- end to end over HTTP -----------------------------------------------------------------------------


def test_cli_end_to_end_runs_the_module_once(tmp_path, jsonrpc_server):
    jsonrpc_server.torrents = [
        {
            "id": 1,
            "hashString": "d" * 40,
            "name": "Gone.Release",
            "status": 6,
            "addedDate": time.time() - 86400,
            "percentDone": 1.0,
            "labels": [],
            "downloadDir": "/data",
            "files": [{"name": "Gone.Release/a.mkv"}],
            "trackerStats": [tracker(UNREG)],
        }
    ]
    path = tmp_path / "config.yml"
    path.write_text(
        f"transmission:\n  host: '{jsonrpc_server.host}'\n  retries: 0\n"
        "commands:\n  rem_unregistered: true\n  tag_tracker_error: true\n",
        encoding="utf-8",
    )
    assert transmission_manage.main(["-c", str(path)]) == 0
    methods = [r["body"].get("method") for r in jsonrpc_server.requests]
    assert methods.count("torrent_get") == 1  # one run serves both commands
    removes = [r["body"]["params"] for r in jsonrpc_server.requests if r["body"].get("method") == "torrent_remove"]
    assert removes == [{"ids": ["d" * 40], "delete_local_data": True}]
