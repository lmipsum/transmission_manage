"""Tests for recheck, ported from qbit_manage's tests/core/test_recheck.py (MIT, see NOTICE).

Upstream cases keep their names where they apply; qBittorrent's paused state maps to Transmission status 0, its
progress to percent_done, its per-torrent ratio limit to seed_ratio_mode 1 + seed_ratio_limit.
"""

from __future__ import annotations

import textwrap

import transmission_manage
from modules import transmission as tr
from modules.config import Config
from modules.core import recheck as rc


class FakeClient:
    def __init__(self, torrents):
        self.items = torrents
        self.calls = []

    def torrents(self, fields, hashes=None):
        return [dict(t) for t in self.items]

    def start(self, hashes):
        self.calls.append(("start", tuple(hashes)))

    def verify(self, hashes):
        self.calls.append(("verify", tuple(hashes)))


class Webhooks:
    def function(self, name, payload):
        pass


def torrent(n, name="Tv.Series.S01E01-NOGRP", status=tr.STATUS_STOPPED, done=1.0, ratio=0.0, mode=0, limit=0.0, size=100):
    return {
        "hash_string": f"{n:040x}",
        "name": name,
        "status": status,
        "percent_done": done,
        "upload_ratio": ratio,
        "seed_ratio_mode": mode,
        "seed_ratio_limit": limit,
        "size_when_done": size,
    }


def run(tmp_path, torrents, dry_run=False):
    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent("transmission: {}\n"), encoding="utf-8")
    config = Config(str(path), {"recheck": True, "dry_run": dry_run})
    client = FakeClient(torrents)
    stats = rc.run(transmission_manage.Context(client, config, Webhooks(), None))
    return stats, client.calls


# -- resume completed paused torrents ------------------------------------------------------------------


def test_recheck_resumes_completed_paused_torrent_with_no_limits(tmp_path):
    stats, calls = run(tmp_path, [torrent(1, mode=2)])
    assert calls == [("start", (f"{1:040x}",))] and stats["resumed"] == 1


def test_global_ratio_mode_counts_as_no_limit(tmp_path):
    # upstream treats qBittorrent's -2 (use global) like -1: no per-torrent limit
    stats, calls = run(tmp_path, [torrent(1, mode=0, ratio=5.0)])
    assert calls == [("start", (f"{1:040x}",))]


def test_recheck_resumes_with_unmet_ratio_limit(tmp_path):
    _, calls = run(tmp_path, [torrent(1, mode=1, ratio=1.5, limit=2.0)])
    assert calls == [("start", (f"{1:040x}",))]


def test_recheck_does_not_resume_once_the_ratio_limit_is_met(tmp_path):
    stats, calls = run(tmp_path, [torrent(1, mode=1, ratio=2.0, limit=2.0)])
    assert calls == [] and stats["resumed"] == 0


def test_infinite_ratio_meets_any_limit(tmp_path):
    _, calls = run(tmp_path, [torrent(1, mode=1, ratio=tr.RATIO_INF, limit=2.0)])
    assert calls == []


def test_idle_limit_is_not_a_seeding_time_limit(tmp_path):
    # Transmission has no seeding-time limit; seed_idle_* is ignored, so this resumes
    t = torrent(1, mode=2)
    t.update({"seed_idle_mode": 1, "seed_idle_limit": 1})
    _, calls = run(tmp_path, [t])
    assert calls == [("start", (f"{1:040x}",))]


# -- recheck incomplete paused torrents ----------------------------------------------------------------


def test_recheck_rechecks_incomplete_paused_torrent_if_complete_on_disk(tmp_path):
    items = [torrent(1, done=0.0), torrent(2, status=tr.STATUS_SEED, done=1.0)]
    stats, calls = run(tmp_path, items)
    assert calls == [("verify", (f"{1:040x}",))] and stats["rechecked"] == 1


def test_recheck_needs_a_complete_torrent_with_the_same_name(tmp_path):
    items = [torrent(1, done=0.0), torrent(2, name="Other.Release", status=tr.STATUS_SEED)]
    _, calls = run(tmp_path, items)
    assert calls == []


def test_recheck_skips_if_already_checking(tmp_path):
    items = [torrent(1, status=tr.STATUS_CHECK, done=0.0), torrent(2, status=tr.STATUS_SEED)]
    _, calls = run(tmp_path, items)
    assert calls == []


def test_partially_downloaded_torrent_is_left_alone(tmp_path):
    items = [torrent(1, done=0.4), torrent(2, status=tr.STATUS_SEED)]
    _, calls = run(tmp_path, items)
    assert calls == []


# -- dry-run suppression -------------------------------------------------------------------------------


def test_recheck_dry_run_suppresses_resume(tmp_path):
    stats, calls = run(tmp_path, [torrent(1, mode=2)], dry_run=True)
    assert calls == [] and stats["resumed"] == 1


def test_recheck_dry_run_suppresses_recheck(tmp_path):
    items = [torrent(1, done=0.0), torrent(2, status=tr.STATUS_SEED)]
    stats, calls = run(tmp_path, items, dry_run=True)
    assert calls == [] and stats["rechecked"] == 1


# -- no-op cases and order -----------------------------------------------------------------------------


def test_recheck_no_op_when_no_paused_torrents(tmp_path):
    stats, calls = run(tmp_path, [torrent(1, status=tr.STATUS_SEED)])
    assert calls == [] and stats == {"resumed": 0, "rechecked": 0}


def test_recheck_no_op_when_empty_torrent_list(tmp_path):
    stats, calls = run(tmp_path, [])
    assert calls == [] and stats == {"resumed": 0, "rechecked": 0}


def test_stopped_torrents_are_processed_smallest_first(tmp_path):
    items = [torrent(1, name="Big", size=900), torrent(2, name="Small", size=10), torrent(3, name="Mid", size=50)]
    _, calls = run(tmp_path, items)
    assert [c[1][0] for c in calls] == [f"{2:040x}", f"{3:040x}", f"{1:040x}"]


def test_recheck_skips_when_command_disabled(tmp_path, jsonrpc_server):
    jsonrpc_server.torrents = [
        {"id": 1, "hashString": "e" * 40, "name": "X", "status": 0, "percentDone": 1.0, "seedRatioMode": 2}
    ]
    path = tmp_path / "config.yml"
    path.write_text(
        f"transmission:\n  host: '{jsonrpc_server.host}'\n  retries: 0\ncommands:\n  recheck: false\n", encoding="utf-8"
    )
    assert transmission_manage.main(["-c", str(path)]) == 0
    assert not any(r["body"].get("method") == "torrent_start" for r in jsonrpc_server.requests)
