"""Tests for tag_update, ported from qbit_manage's tests/core/test_tags.py (MIT, see NOTICE), plus the tracker-tag
lookup of get_tags(). qBittorrent's "stalledDL" state maps to a downloading torrent (status 4) with is_stalled.
"""

from __future__ import annotations

import logging
import textwrap

import transmission_manage
from modules import transmission as tr
from modules.config import Config
from modules.core import tags as tg

TRACKERS = "tracker:\n  tracker-one.example.org:\n    tag: TrackerOne\n"


class FakeClient:
    def __init__(self, torrents):
        self.items = torrents
        self.calls = []

    def torrents(self, fields, hashes=None):
        return [dict(t) for t in self.items]

    def set_labels(self, hashes, labels):
        self.calls.append((tuple(hashes), tuple(labels)))


class Webhooks:
    def function(self, name, payload):
        pass


def torrent(n=1, labels=None, status=tr.STATUS_SEED, stalled=False, private=False, host="tracker-one.example.org"):
    stats = [{"announce": f"https://{host}/announce"}] if host else []
    return {
        "hash_string": f"{n:040x}",
        "name": f"Tv.Series.S01E0{n}-NOGRP",
        "labels": labels or [],
        "status": status,
        "is_stalled": stalled,
        "is_private": private,
        "tracker_stats": stats,
    }


def run(tmp_path, torrents, settings="", trackers=TRACKERS, dry_run=False):
    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent(trackers + settings), encoding="utf-8")
    config = Config(str(path), {"tag_update": True, "dry_run": dry_run})
    client = FakeClient(torrents)
    stats = tg.run(transmission_manage.Context(client, config, Webhooks(), None))
    return stats, client.calls


def stalled_download(**kwargs):
    return torrent(status=tr.STATUS_DOWNLOAD, stalled=True, **kwargs)


# -- upstream cases ------------------------------------------------------------------------------------


def test_tags_adds_stalled_tag_to_stalled_torrent(tmp_path):
    stats, calls = run(tmp_path, [stalled_download()])
    assert calls == [((f"{1:040x}",), ("TrackerOne", "stalledDL"))]
    assert stats["tagged"] == 2


def test_tags_skips_stalled_tag_when_disabled(tmp_path):
    _, calls = run(tmp_path, [stalled_download()], settings="settings:\n  tag_stalled_torrents: false\n")
    assert calls == [((f"{1:040x}",), ("TrackerOne",))]


def test_tags_removes_stalled_tag_when_no_longer_stalled(tmp_path):
    _, calls = run(tmp_path, [torrent(labels=["TrackerOne", "Stalled"])], settings="settings:\n  stalled_tag: Stalled\n")
    assert calls == [((f"{1:040x}",), ("TrackerOne",))]


def test_tags_dry_run_suppresses_add_tags(tmp_path):
    stats, calls = run(tmp_path, [stalled_download()], dry_run=True)
    assert calls == [] and stats["tagged"] == 2


def test_tags_dry_run_suppresses_remove_tags(tmp_path):
    _, calls = run(tmp_path, [torrent(labels=["TrackerOne", "stalledDL"])], dry_run=True)
    assert calls == []


def test_tags_adds_private_tag_to_private_torrent(tmp_path):
    _, calls = run(tmp_path, [torrent(private=True)], settings="settings:\n  private_tag: private\n")
    assert calls == [((f"{1:040x}",), ("TrackerOne", "private"))]


def test_tags_skips_private_tag_when_torrent_public(tmp_path):
    _, calls = run(tmp_path, [torrent(private=False)], settings="settings:\n  private_tag: private\n")
    assert calls == [((f"{1:040x}",), ("TrackerOne",))]


def test_tags_applies_both_stalled_and_private_tags(tmp_path):
    _, calls = run(tmp_path, [stalled_download(private=True)], settings="settings:\n  private_tag: private\n")
    assert calls == [((f"{1:040x}",), ("TrackerOne", "stalledDL", "private"))]


# -- Transmission specifics ----------------------------------------------------------------------------


def test_stalled_seeding_torrent_is_not_stalled_dl(tmp_path):
    _, calls = run(tmp_path, [torrent(status=tr.STATUS_SEED, stalled=True, labels=["TrackerOne"])])
    assert calls == []


def test_existing_labels_are_kept(tmp_path):
    _, calls = run(tmp_path, [torrent(labels=["keep-me"])])
    assert calls == [((f"{1:040x}",), ("keep-me", "TrackerOne"))]


def test_nothing_to_do_when_tags_are_present(tmp_path):
    stats, calls = run(tmp_path, [torrent(labels=["TrackerOne"])])
    assert calls == [] and stats["tagged"] == 0


# -- get_tags lookup -----------------------------------------------------------------------------------


def test_first_matching_keyword_wins_and_pipe_splits_keywords():
    lookup = tg.TrackerTags({"one.example.org": ["One"], "two.example.net": ["Two"], "backup.example.net": ["Two"]})
    assert lookup.resolve(["https://backup.example.net/announce"])[0] == ["Two"]
    assert lookup.resolve(["https://x.one.example.org/a", "https://two.example.net/a"])[0] == ["One"]


def test_keywords_match_case_sensitively_as_upstream():
    lookup = tg.TrackerTags({"Example.org": ["Tag"]})
    tags, _ = lookup.resolve(["https://example.org/announce"])
    assert tags == ["example.org"]  # no match -> host


def test_other_entry_catches_unmatched_trackers():
    lookup = tg.TrackerTags({"one.example.org": ["One"], "other": ["other"]})
    assert lookup.resolve(["https://unknown.example.com/announce"])[0] == ["other"]


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_unmatched_tracker_without_other_gets_its_host_once():
    logger = logging.getLogger("transmission_manage")
    collect = Collect()
    logger.addHandler(collect)
    try:
        lookup = tg.TrackerTags({"one.example.org": ["One"]})
        assert lookup.resolve(["https://new.example.com:8443/announce"])[0] == ["new.example.com"]
        lookup.resolve(["https://new.example.com:8443/announce"])
    finally:
        logger.removeHandler(collect)
    assert sum("No tags matched" in m for m in collect.messages) == 1


def test_torrent_without_tracker_urls_is_tagged_other():
    assert tg.TrackerTags({}).resolve([]) == (["other"], "No http URL found")


def test_display_url_is_redacted():
    _, url = tg.TrackerTags({"one.example.org": ["One"]}).resolve(["https://one.example.org/announce?pid=abc"])
    assert "abc" not in url
