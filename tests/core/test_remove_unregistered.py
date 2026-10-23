import json
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
    def __init__(self, allow=True, keep_data=False, batch=True, classify=False):
        self.allow = allow
        self.keep_data = keep_data
        self.batch = batch
        self.classify = classify
        self.payloads = []
        self.batches = []
        self.classified = []

    def pre_remove(self, payload):
        self.payloads.append(payload)
        return self.allow, payload["delete_data"] and not self.keep_data

    def pre_remove_batch(self, payload):
        self.batches.append(payload)
        return self.batch

    def classify_unregistered(self, payload):
        self.classified.append(payload)
        return self.classify


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


# --- upstream cases -----------------------------------------------------------------------------------
# Ported from qbit_manage's tests/core/test_remove_unregistered.py (MIT, see NOTICE). The BHD-specific checks
# and the hashes filter are not ported, as the code paths they test are not part of this port.


def remover(tmp_path, items=(), settings="", dry_run=False, hooks=None, tag_error=False):
    ctx, client = make_context(
        tmp_path, list(items), {"dry_run": dry_run, "tag_tracker_error": tag_error}, settings=settings, hooks=hooks
    )
    return ru.RemoveUnregistered(ctx, now=lambda: NOW), client


def test_is_within_grace_when_grace_disabled(tmp_path):
    r, _ = remover(tmp_path, settings="settings:\n  rem_unregistered_grace_minutes: 0\n")
    assert r.is_within_grace(torrent(1, [], added=NOW - 100)) == (False, 0.0)


def test_is_within_grace_when_torrent_within_window(tmp_path):
    r, _ = remover(tmp_path, settings="settings:\n  rem_unregistered_grace_minutes: 60\n")
    within, age = r.is_within_grace(torrent(1, [], added=NOW - 30 * 60))
    assert within is True and 29 < age < 31


def test_is_within_grace_when_torrent_outside_window(tmp_path):
    r, _ = remover(tmp_path, settings="settings:\n  rem_unregistered_grace_minutes: 30\n")
    within, age = r.is_within_grace(torrent(1, [], added=NOW - 60 * 60))
    assert within is False and 59 < age < 61


def test_is_within_grace_no_added_on_field(tmp_path):
    r, _ = remover(tmp_path)
    t = torrent(1, [])
    del t["added_date"]
    assert r.is_within_grace(t) == (False, 0.0)


def test_is_within_grace_invalid_added_on_value(tmp_path):
    r, _ = remover(tmp_path)
    assert r.is_within_grace(torrent(1, [], added="not_a_timestamp")) == (False, 0.0)


def test_max_limit_disabled_allows_deletion(tmp_path):
    r, client = remover(tmp_path, settings="settings:\n  rem_unregistered_max_torrents: 0\n")
    r.check_max_limit_and_delete(torrent(1, []), tracker(UNREG), False, set())
    assert client.calls[0][0] == "remove" and r.tracker_del_count == {}


def test_max_limit_not_reached_allows_deletion(tmp_path):
    r, client = remover(tmp_path, settings="settings:\n  rem_unregistered_max_torrents: 5\n")
    r.check_max_limit_and_delete(torrent(1, []), tracker(UNREG, host="t1.example"), False, set())
    assert client.calls[0][0] == "remove" and r.tracker_del_count["t1.example"] == 1


def test_max_limit_reached_skips_deletion(tmp_path):
    r, client = remover(tmp_path, settings="settings:\n  rem_unregistered_max_torrents: 2\n")
    r.tracker_del_count["t1.example"] = 2
    r.check_max_limit_and_delete(torrent(2, []), tracker(UNREG, host="t1.example"), False, set())
    assert client.calls == [] and r.tracker_del_count["t1.example"] == 2


def test_remove_previous_errors_untagged_working_torrent(tmp_path):
    t = torrent(1, [tracker("Success")], labels=["issue"])
    r, client = remover(tmp_path, [t])
    r.run()
    assert client.calls == [("set_labels", (t["hash_string"],), ())]
    assert r.stats["untagged"] == 1


def test_remove_previous_errors_dry_run_suppresses_remove_tags(tmp_path):
    t = torrent(1, [tracker("Success")], labels=["issue"])
    r, client = remover(tmp_path, [t], dry_run=True)
    r.run()
    assert client.calls == [] and r.stats["untagged"] == 1


def test_del_unregistered_cross_seed_with_working_tracker(tmp_path):
    t = torrent(1, [])
    r, client = remover(tmp_path)
    r.check_max_limit_and_delete(t, tracker(UNREG), True, {t["name"]})
    assert client.calls == [("remove", (t["hash_string"],), False)]
    assert (r.stats["deleted"], r.stats["deleted_contents"]) == (1, 0)
    assert r.events["rem_unregistered"][0]["torrents_deleted_and_contents"] is False


def test_del_unregistered_cross_seed_all_broken(tmp_path):
    t = torrent(1, [])
    r, client = remover(tmp_path)
    r.check_max_limit_and_delete(t, tracker(UNREG), True, set())
    assert client.calls == [("remove", (t["hash_string"],), True)]
    assert r.stats["deleted_contents"] == 1


def test_del_unregistered_no_cross_seed_deletes_content(tmp_path):
    t = torrent(1, [])
    r, client = remover(tmp_path)
    r.check_max_limit_and_delete(t, tracker(UNREG), False, {t["name"]})
    assert client.calls == [("remove", (t["hash_string"],), True)]


def test_del_unregistered_dry_run_suppresses_delete(tmp_path):
    r, client = remover(tmp_path, dry_run=True)
    r.check_max_limit_and_delete(torrent(1, []), tracker(UNREG), False, set())
    assert client.calls == [] and r.stats["deleted_contents"] == 1


def test_tag_tracker_error_adds_tag(tmp_path):
    t = torrent(1, [])
    r, client = remover(tmp_path, tag_error=True)
    r.tag_tracker_error(t, tracker("Connection timed out", succeeded=False))
    assert client.calls == [("set_labels", (t["hash_string"],), ("issue",))]
    assert r.stats["tagged"] == 1


def test_tag_tracker_error_dry_run_suppresses_add_tags(tmp_path):
    r, client = remover(tmp_path, tag_error=True, dry_run=True)
    r.tag_tracker_error(torrent(1, []), tracker("Connection timed out", succeeded=False))
    assert client.calls == [] and r.stats["tagged"] == 1


def removals(tmp_path, trackers):
    r, client = remover(tmp_path, [torrent(1, trackers)], settings="settings:\n  rem_unregistered_grace_minutes: 0\n")
    r.run()
    return [c for c in client.calls if c[0] == "remove"]


def test_process_issues_detects_unregistered_when_not_last_tracker(tmp_path):
    trackers = [tracker("some other tracker error", host="a:1"), tracker("err: unregistered torrent", host="b:1")]
    assert removals(tmp_path, trackers)


def test_process_issues_none_msg_earlier_tracker_still_detects(tmp_path):
    first = tracker("x", succeeded=False, host="a:1")
    first["last_announce_result"] = None
    assert removals(tmp_path, [first, tracker("err: unregistered torrent", host="b:1")])


def test_process_issues_single_unregistered_still_deletes(tmp_path):
    assert removals(tmp_path, [tracker("Unregistered torrent")])


def test_process_issues_deletes_despite_host_not_found_sibling(tmp_path):
    trackers = [tracker("Host not found (authoritative)", host="a:1"), tracker("err: unregistered torrent", host="b:1")]
    assert removals(tmp_path, trackers)


def test_process_issues_deletes_when_sibling_tracker_times_out(tmp_path):
    trackers = [tracker("Connection timed out", host="a:1"), tracker("Unregistered torrent", host="b:1")]
    assert removals(tmp_path, trackers)


def test_process_issues_defers_when_tracker_still_updating(tmp_path):
    updating = tracker("", succeeded=False, state=3, host="a:1")  # announcing right now
    assert removals(tmp_path, [updating, tracker("Unregistered torrent", host="b:1")]) == []


def test_process_issues_skips_when_any_tracker_working(tmp_path):
    assert removals(tmp_path, [tracker("Success", host="a:1"), tracker("Unregistered torrent", host="b:1")]) == []


def test_grace_period_blocks_deletion(tmp_path):
    r, _ = remover(tmp_path, settings="settings:\n  rem_unregistered_grace_minutes: 60\n")
    within, age = r.is_within_grace(torrent(1, [], added=NOW - 300))
    assert within is True and 4 < age < 6


def test_unregistered_message_list_entries_are_detected():
    for message in ru.TorrentMessages.UNREGISTERED_MSGS:
        assert list_in_text(message, ru.TorrentMessages.UNREGISTERED_MSGS)


# --- extension hooks ----------------------------------------------------------------------------------


def test_batch_hook_sees_every_planned_removal_before_any_is_made(tmp_path):
    items = [torrent(n, [tracker(UNREG, host=f"t{n}.example:443")]) for n in (1, 2)]
    hooks = Hooks()
    ctx, client = make_context(tmp_path, items, hooks=hooks)
    run(ctx)
    assert [t["hash"] for t in hooks.batches[0]["torrents"]] == [t["hash_string"] for t in items]
    assert hooks.batches[0]["torrents"][0]["status"] == UNREG
    assert len([c for c in client.calls if c[0] == "remove"]) == 2


def test_batch_hook_includes_torrents_past_the_per_tracker_limit(tmp_path):
    items = [torrent(n, [tracker(UNREG)]) for n in (1, 2, 3)]
    hooks = Hooks()
    ctx, client = make_context(tmp_path, items, settings="settings:\n  rem_unregistered_max_torrents: 1\n", hooks=hooks)
    run(ctx)
    assert len(hooks.batches[0]["torrents"]) == 3
    assert len([c for c in client.calls if c[0] == "remove"]) == 1


def test_declined_batch_removes_nothing_but_still_tags(tmp_path):
    items = [torrent(n, [tracker(UNREG)]) for n in (1, 2)]
    hooks = Hooks(batch=False)
    ctx, client = make_context(tmp_path, items, commands={"tag_tracker_error": True}, hooks=hooks)
    stats = run(ctx)
    assert [c for c in client.calls if c[0] == "remove"] == [] and hooks.payloads == []
    assert stats["tagged"] == 2


def test_batch_hook_is_not_called_without_planned_removals(tmp_path):
    items = [torrent(1, [tracker(UNREG)], added=NOW - 60)]
    hooks = Hooks()
    ctx, _ = make_context(tmp_path, items, settings="settings:\n  rem_unregistered_grace_minutes: 10\n", hooks=hooks)
    run(ctx)
    assert hooks.batches == []


def test_pre_remove_can_keep_the_files(tmp_path):
    t = torrent(1, [tracker(UNREG)])
    ctx, client = make_context(tmp_path, [t], hooks=Hooks(keep_data=True))
    stats = run(ctx)
    assert client.calls == [("remove", (t["hash_string"],), False)]
    assert (stats["deleted"], stats["deleted_contents"]) == (1, 0)


def test_classify_hook_marks_an_unknown_message_unregistered(tmp_path):
    t = torrent(1, [tracker("Tracker HTTP response 404 (Not Found)", succeeded=False)])
    hooks = Hooks(classify=True)
    ctx, client = make_context(tmp_path, [t], hooks=hooks)
    run(ctx)
    assert hooks.classified[0]["trackers"] == [{"host": "t1.example.org:443", "message": "Tracker HTTP response 404 (Not Found)"}]
    assert "announce" not in json.dumps(hooks.classified)  # no announce URLs (passkeys) in hook payloads
    assert client.calls == [("remove", (t["hash_string"],), True)]


def test_classify_hook_answer_no_keeps_the_torrent(tmp_path):
    t = torrent(1, [tracker("Tracker HTTP response 404 (Not Found)", succeeded=False)])
    ctx, client = make_context(tmp_path, [t], hooks=Hooks(classify=False))
    run(ctx)
    assert client.calls == []


def test_classify_hook_is_not_asked_about_known_or_never_unregistered_messages(tmp_path):
    items = [
        torrent(1, [tracker(UNREG)]),
        torrent(2, [tracker("Passkey not found", succeeded=False)]),
        torrent(3, [tracker("Tracker is down", succeeded=False)]),
    ]
    hooks = Hooks(classify=True)
    ctx, _ = make_context(tmp_path, items, hooks=hooks)
    run(ctx)
    assert hooks.classified == []


def test_classify_hook_is_only_used_for_rem_unregistered(tmp_path):
    t = torrent(1, [tracker("Tracker HTTP response 404 (Not Found)", succeeded=False)])
    hooks = Hooks(classify=True)
    ctx, client = make_context(tmp_path, [t], commands={"rem_unregistered": False, "tag_tracker_error": True}, hooks=hooks)
    run(ctx)
    assert hooks.classified == [] and [c for c in client.calls if c[0] == "remove"] == []
