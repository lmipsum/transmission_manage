"""Tests for share_limits, ported from qbit_manage's tests/core/test_share_limit*.py (MIT, see NOTICE).

Upstream cases keep their names where they apply. qBittorrent tags are Transmission labels; setShareLimits maps to
seed_ratio_mode / seed_idle_mode (-2 global = 0, -1 unlimited = 2, a value = 1); upload limits are KiB/s in the
config and Transmission speed units (1000 bytes unless the session says otherwise) on the torrent. Transmission
has no seeding-time limit, so a reached max_seeding_time stops the torrent (see the Transmission section).
"""

from __future__ import annotations

import os

import pytest
import yaml

import transmission_manage
from modules import transmission as tr
from modules.config import Config
from modules.config import validate_share_limit_action
from modules.core import recheck as rc
from modules.core import share_limits as sl_mod
from modules.util import Failed

NOW = 1_000_000
TRACKER = "http://tracker1.example/announce"


class FakeClient:
    def __init__(self, torrents=(), session=None):
        self.items = list(torrents)
        self.session_values = {"seed_ratio_limited": False, "idle_seeding_limit_enabled": False, "units": {}}
        self.session_values.update(session or {})
        self.calls = []

    def session(self, fields=None):
        return dict(self.session_values)

    def torrents(self, fields, hashes=None):
        if hashes is None:
            return list(self.items)
        return [dict(t) for t in self.items if t["hash_string"] in hashes]

    def set_labels(self, hashes, labels):
        self.calls.append(("set_labels", hashes[0], list(labels)))

    def set_torrent(self, hashes, fields):
        self.calls.append(("set_torrent", hashes[0], dict(fields)))

    def set_session(self, fields):
        self.calls.append(("set_session", None, dict(fields)))

    def start(self, hashes):
        self.calls.append(("start", hashes[0], None))

    def stop(self, hashes):
        self.calls.append(("stop", hashes[0], None))

    def remove(self, hashes, delete_data=False):
        self.calls.append(("remove", hashes[0], delete_data))

    def of(self, name, torrent=None):
        return [c for c in self.calls if c[0] == name and (torrent is None or c[1] == torrent["hash_string"])]


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


def group(**overrides):
    return {"priority": 1, **overrides}


def make(tmp_path, groups=None, torrents=(), dry_run=False, settings=None, session=None, hooks=None, root=None):
    data = {
        "settings": settings or {},
        "share_limits": groups or {"noHL": group()},
        "directory": {"root_dir": root},
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = Config(str(path), {"share_limits": True, "dry_run": dry_run})
    client = FakeClient(torrents, session)
    context = transmission_manage.Context(client, config, Webhooks(), hooks or Hooks())
    return sl_mod.ShareLimits(context, now=lambda: NOW), client


def torrent(
    n=1,
    name=None,
    labels=(),
    ratio=0.0,
    seeding=0,
    seeds=10,
    activity=NOW,
    mode=tr.LIMIT_UNLIMITED,
    limit=0.0,
    up=None,
    status=tr.STATUS_SEED,
    done=1.0,
    size=100,
    download_dir="/data/torrents",
):
    return {
        "hash_string": f"{n:040x}",
        "name": name or f"Tv.Series.S01E0{n}-NOGRP",
        "labels": list(labels),
        "status": status,
        "percent_done": done,
        "total_size": size,
        "upload_ratio": ratio,
        "seed_ratio_mode": mode,
        "seed_ratio_limit": limit,
        "seed_idle_mode": tr.LIMIT_GLOBAL,
        "seed_idle_limit": 30,
        "upload_limited": up is not None,
        "upload_limit": 0 if up is None else round(up * 1024 / 1000),
        "seconds_seeding": seeding,
        "activity_date": activity,
        "download_dir": download_dir,
        "tracker_stats": [{"announce": TRACKER, "seeder_count": seeds, "last_announce_succeeded": True}],
    }


def seed_limit(sl, t, **overrides):
    kwargs = {
        "torrent": t,
        "max_ratio": -1,
        "max_seeding_time": -1,
        "max_last_active": -1,
        "min_seeding_time": 0,
        "min_num_seeds": 0,
        "min_last_active": 0,
        "resume_torrent": True,
        "tracker": TRACKER,
        "reset_upload_speed_on_unmet_minimums": True,
    }
    kwargs.update(overrides)
    return sl.has_reached_seed_limit(**kwargs)


def added(client, t):
    """Labels added by set_labels calls, in order (for torrents that started without labels)."""
    before = []
    result = []
    for _, _, labels in client.of("set_labels", t):
        result += [label for label in labels if label not in before]
        before = labels
    return result


def removed(client, t, initial):
    before = list(initial)
    result = []
    for _, _, labels in client.of("set_labels", t):
        result += [label for label in before if label not in labels]
        before = labels
    return result


def limits_lifted(client, t):
    return any(
        c[2].get("seed_ratio_mode") == tr.LIMIT_UNLIMITED and c[2].get("seed_idle_mode") == tr.LIMIT_UNLIMITED
        for c in client.of("set_torrent", t)
    )


def upload_calls(client, t):
    return [c[2] for c in client.of("set_torrent", t) if "upload_limited" in c[2]]


# -- has_reached_seed_limit: min_num_seeds -------------------------------------------------------------


def test_min_num_seeds_zero_never_gates(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0)
    assert seed_limit(sl, t, min_num_seeds=0)[0] is False
    assert client.of("set_labels") == []


def test_min_num_seeds_met_clears_existing_tag(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=10, labels=[sl.min_num_seeds_tag])
    seed_limit(sl, t, min_num_seeds=5)
    assert removed(client, t, [sl.min_num_seeds_tag]) == [sl.min_num_seeds_tag]


def test_min_num_seeds_not_met_adds_tag_and_clears_limits(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=2, status=tr.STATUS_STOPPED)
    body, _ = seed_limit(sl, t, min_num_seeds=5)
    assert body == ""  # NOT eligible for deletion
    assert added(client, t) == [sl.min_num_seeds_tag]
    assert limits_lifted(client, t)
    assert client.of("start", t)


def test_min_num_seeds_not_met_with_tag_already_present_skips_remediation(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=2, labels=[sl.min_num_seeds_tag], status=tr.STATUS_STOPPED)
    body, _ = seed_limit(sl, t, min_num_seeds=5)
    assert body == ""
    assert client.calls == []


def test_min_num_seeds_no_resume_when_resume_torrent_false(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=2, status=tr.STATUS_STOPPED)
    seed_limit(sl, t, min_num_seeds=5, resume_torrent=False)
    assert client.of("start") == []


def test_unknown_seed_count_counts_as_zero(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=-1)
    seed_limit(sl, t, min_num_seeds=1)
    assert added(client, t) == [sl.min_num_seeds_tag]


# -- has_reached_seed_limit: last_active ---------------------------------------------------------------


def test_last_active_met_returns_false_after_gating(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(activity=NOW - 7200)
    assert seed_limit(sl, t, min_last_active=60)[0] is False
    assert client.of("set_labels") == []


def test_last_active_met_clears_existing_tag(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(activity=NOW - 7200, labels=[sl.last_active_tag])
    seed_limit(sl, t, min_last_active=60)
    assert removed(client, t, [sl.last_active_tag]) == [sl.last_active_tag]


def test_last_active_not_met_adds_warning_tag(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(activity=NOW - 60, status=tr.STATUS_STOPPED)
    body, _ = seed_limit(sl, t, min_last_active=60)
    assert body == ""
    assert added(client, t) == [sl.last_active_tag]
    assert limits_lifted(client, t)
    assert client.of("start", t)


# -- has_reached_seed_limit: max_ratio -----------------------------------------------------------------


def test_ratio_met_and_min_seeding_time_met_returns_body(tmp_path):
    sl, _ = make(tmp_path)
    body, _ = seed_limit(sl, torrent(ratio=2.5, seeding=7200), max_ratio=2.0, min_seeding_time=60)
    assert body and "Ratio vs Max Ratio" in body


def test_ratio_met_but_min_seeding_time_not_met_tags_and_clears(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(ratio=2.5, seeding=1800)
    assert seed_limit(sl, t, max_ratio=2.0, min_seeding_time=60)[0] is False
    assert added(client, t) == [sl.min_seeding_time_tag]
    assert limits_lifted(client, t)


def test_ratio_not_met_no_action(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(ratio=1.0, seeding=99999)
    assert seed_limit(sl, t, max_ratio=2.0)[0] is False
    assert client.of("set_labels") == []


def test_infinite_ratio_meets_the_limit(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(ratio=tr.RATIO_INF), max_ratio=2.0)[0]


def test_crossover_min_seeding_time_tag_removed_when_now_met(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(ratio=2.5, seeding=7200, labels=[sl.min_seeding_time_tag])
    assert seed_limit(sl, t, max_ratio=2.0, min_seeding_time=60)[0]
    assert removed(client, t, [sl.min_seeding_time_tag]) == [sl.min_seeding_time_tag]


def test_global_ratio_enabled_and_met(tmp_path):
    sl, _ = make(tmp_path)
    sl.global_max_ratio_enabled, sl.global_max_ratio = True, 2.0
    body, _ = seed_limit(sl, torrent(ratio=3.0, seeding=99999), max_ratio=-2)
    assert body and "Global Max Ratio" in body


def test_global_ratio_disabled_falls_through(tmp_path):
    sl, _ = make(tmp_path)
    sl.global_max_ratio_enabled, sl.global_max_ratio = False, 2.0
    assert seed_limit(sl, torrent(ratio=3.0), max_ratio=-2)[0] is False


# -- has_reached_seed_limit: max_seeding_time ----------------------------------------------------------


def test_max_seeding_time_met_returns_body(tmp_path):
    sl, _ = make(tmp_path)
    body, _ = seed_limit(sl, torrent(seeding=7200, ratio=0.1), max_seeding_time=60)
    assert body and "Seeding Time vs Max Seed Time" in body


def test_max_seeding_time_not_met_no_action(tmp_path):
    sl, client = make(tmp_path)
    assert seed_limit(sl, torrent(seeding=600, ratio=0.1), max_seeding_time=60)[0] is False
    assert client.calls == []


def test_global_max_seeding_time_disabled_falls_through(tmp_path):
    # Transmission has no global seeding-time limit, so -2 never reads as reached.
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(seeding=7200, ratio=0.1), max_seeding_time=-2)[0] is False


# -- has_reached_seed_limit: dry run, max_last_active --------------------------------------------------


def test_dry_run_suppresses_all_qbit_mutations_in_seed_limit_path(tmp_path):
    sl, client = make(tmp_path, dry_run=True)
    seed_limit(sl, torrent(seeds=0), min_num_seeds=5)
    assert client.calls == []


def test_max_last_active_met_returns_body(tmp_path):
    sl, _ = make(tmp_path)
    body, _ = seed_limit(sl, torrent(activity=NOW - 7200), max_last_active=60)
    assert body and "Inactive Time vs Max Last Active Time" in body


def test_max_last_active_not_met_does_not_return_body(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(activity=NOW - 300), max_last_active=60)[0] is False


def test_max_last_active_minus_one_skips_check(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(activity=0), max_last_active=-1)[0] is False


def test_max_last_active_met_clears_last_active_tag(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(activity=NOW - 7200, labels=[sl.last_active_tag])
    seed_limit(sl, t, max_last_active=60)
    assert removed(client, t, [sl.last_active_tag])


def test_max_last_active_met_but_min_seeding_time_not_met_protects_torrent(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(activity=NOW - 7200, seeding=453600)
    assert seed_limit(sl, t, max_last_active=60, min_seeding_time=8640)[0] is False
    assert added(client, t) == [sl.min_seeding_time_tag]
    assert limits_lifted(client, t)


def test_max_last_active_met_and_min_seeding_time_met_returns_body(tmp_path):
    sl, _ = make(tmp_path)
    body, _ = seed_limit(sl, torrent(activity=NOW - 7200, seeding=518400), max_last_active=60, min_seeding_time=8640)
    assert body and "Inactive Time vs Max Last Active Time" in body


def test_max_last_active_met_but_min_seeding_time_not_met_exclusion_tag_added(tmp_path):
    sl, _ = make(tmp_path)
    _, exclusion = seed_limit(sl, torrent(activity=NOW - 7200, seeding=453600), max_last_active=60, min_seeding_time=8640)
    assert exclusion is True


# -- has_reached_seed_limit: reset_upload_speed_on_unmet_minimums --------------------------------------


def test_reset_upload_speed_on_unmet_minimums_true_resets_speed(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, up=1)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=True)
    assert upload_calls(client, t) == [{"upload_limited": False}]


def test_reset_upload_speed_on_unmet_minimums_false_does_not_reset(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, up=1)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False)
    assert upload_calls(client, t) == []


def test_reset_upload_speed_false_still_adds_tag(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False)
    assert added(client, t) == [sl.min_num_seeds_tag]


def test_reset_upload_speed_false_applies_configured_limit_to_unlimited_torrent(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False, limit_upload_speed=100)
    assert upload_calls(client, t) == [{"upload_limited": True, "upload_limit": 102}]  # 100 KiB/s in kB/s
    assert limits_lifted(client, t)  # the minimum still protects the torrent


def test_reset_upload_speed_false_applies_limit_when_tag_already_present(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, labels=[sl.min_num_seeds_tag])
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False, limit_upload_speed=100)
    assert upload_calls(client, t) == [{"upload_limited": True, "upload_limit": 102}]


def test_reset_upload_speed_false_skips_limit_already_applied(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, up=100)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False, limit_upload_speed=100)
    assert upload_calls(client, t) == []


def test_reset_upload_speed_false_unlimited_group_limit_preserves_existing(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, up=50)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False, limit_upload_speed=-1)
    assert upload_calls(client, t) == []


def test_reset_upload_speed_true_ignores_configured_limit(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0)
    seed_limit(sl, t, min_num_seeds=5, reset_upload_speed_on_unmet_minimums=True, limit_upload_speed=100)
    assert upload_calls(client, t) == [{"upload_limited": False}]


def test_reset_upload_speed_false_dry_run_does_not_apply_limit(tmp_path):
    sl, client = make(tmp_path, dry_run=True)
    seed_limit(sl, torrent(seeds=0), min_num_seeds=5, reset_upload_speed_on_unmet_minimums=False, limit_upload_speed=100)
    assert client.calls == []


# -- has_reached_seed_limit: misc ----------------------------------------------------------------------


def test_empty_tracker_url_does_not_crash(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(ratio=5.0, seeding=99999), max_ratio=2.0, tracker="")[0]


def test_both_min_tags_present_both_removed_when_met(tmp_path):
    sl, client = make(tmp_path)
    initial = [sl.min_seeding_time_tag, sl.min_num_seeds_tag]
    t = torrent(ratio=3.0, seeding=7200, seeds=10, labels=initial)
    assert seed_limit(sl, t, max_ratio=2.0, min_seeding_time=60, min_num_seeds=5)[0]
    assert set(removed(client, t, initial)) == set(initial)


def test_min_num_seeds_and_min_seeding_time_not_met_only_seeds_tag_added(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(seeds=0, seeding=100, ratio=5.0)
    seed_limit(sl, t, min_num_seeds=5, min_seeding_time=60, max_ratio=2.0)
    assert added(client, t) == [sl.min_num_seeds_tag]


def test_exclusion_tag_added_true_when_tag_applied(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(seeds=0), min_num_seeds=5)[1] is True


def test_exclusion_tag_added_false_when_no_gate_fires(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(ratio=1.0, seeding=99999), max_ratio=2.0)[1] is False


def test_exclusion_tag_added_false_when_deletion_eligible(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(ratio=3.0, seeding=7200), max_ratio=2.0, min_seeding_time=60)[1] is False


def test_exclusion_tag_added_false_when_tag_already_present(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(seeds=0, labels=[sl.min_num_seeds_tag]), min_num_seeds=5)[1] is False


def test_exclusion_tag_added_true_for_last_active_gate(tmp_path):
    sl, _ = make(tmp_path)
    assert seed_limit(sl, torrent(activity=NOW - 60), min_last_active=60)[1] is True


# -- set_limits ----------------------------------------------------------------------------------------


def limit_fields(client, t):
    return [c[2] for c in client.of("set_torrent", t) if "seed_ratio_mode" in c[2]]


def test_no_limit_calls_set_share_limits_with_negative_ones(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(mode=tr.LIMIT_SINGLE, limit=1.0)
    sl.set_limits(t, max_ratio=-1, max_seeding_time=-1)
    # ratio -1 = unlimited; the inactive limit goes to the global one (-2), as upstream
    assert limit_fields(client, t) == [{"seed_ratio_mode": tr.LIMIT_UNLIMITED, "seed_idle_mode": tr.LIMIT_GLOBAL}]


def test_global_limit_uses_minus_two(tmp_path):
    sl, client = make(tmp_path)
    t = torrent()
    sl.set_limits(t, max_ratio=-2, max_seeding_time=-2)
    assert limit_fields(client, t)[0]["seed_ratio_mode"] == tr.LIMIT_GLOBAL


def test_hard_ratio_limit_applied(tmp_path):
    sl, client = make(tmp_path)
    t = torrent()
    body = sl.set_limits(t, max_ratio=5.0, max_seeding_time=-1)
    assert any("Max Ratio = 5.0" in line for line in body)
    assert limit_fields(client, t)[0] == {
        "seed_ratio_mode": tr.LIMIT_SINGLE,
        "seed_ratio_limit": 5.0,
        "seed_idle_mode": tr.LIMIT_GLOBAL,
    }


def test_hard_seeding_time_limit_applied(tmp_path):
    sl, client = make(tmp_path)
    t = torrent()
    body = sl.set_limits(t, max_ratio=-1, max_seeding_time=43200)
    assert any("Max Seed Time" in line for line in body)
    # nothing seeding-time related is sent: Transmission has no such limit
    assert all("seeding" not in key for fields in limit_fields(client, t) for key in fields)


def test_tag_applied(tmp_path):
    sl, client = make(tmp_path)
    t = torrent()
    sl.group_tag = "~share_limit_1.noHL"
    sl.update_share_limits_tag_for_torrent(t)
    assert client.of("set_labels", t) == [("set_labels", t["hash_string"], ["~share_limit_1.noHL"])]


def test_upload_limit_unlimited_translates_to_minus_one(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(up=100)
    sl.set_limits(t, max_ratio=-1, max_seeding_time=-1, limit_upload_speed=-1)
    assert upload_calls(client, t) == [{"upload_limited": False}]


def test_upload_limit_positive_translates_to_transmission_units(tmp_path):
    sl, client = make(tmp_path)
    t = torrent()
    sl.set_limits(t, max_ratio=-1, max_seeding_time=-1, limit_upload_speed=500)
    assert upload_calls(client, t) == [{"upload_limited": True, "upload_limit": 512}]


def test_upload_limit_unchanged_skips_call(tmp_path):
    sl, client = make(tmp_path)
    t = torrent(up=500)
    sl.set_limits(t, max_ratio=-1, max_seeding_time=-1, limit_upload_speed=500)
    assert upload_calls(client, t) == []


def test_dry_run_suppresses_all_qbit_mutations(tmp_path):
    sl, client = make(tmp_path, dry_run=True)
    t = torrent()
    sl.group_tag = "~share_limit_1.x"
    sl.set_limits(t, max_ratio=5.0, max_seeding_time=-1, limit_upload_speed=100)
    sl.update_share_limits_tag_for_torrent(t)
    assert client.calls == []


@pytest.mark.parametrize("blocking_tag_attr", ["min_seeding_time_tag", "min_num_seeds_tag", "last_active_tag"])
def test_blocking_tag_short_circuits_set_share_limits(tmp_path, blocking_tag_attr):
    sl, client = make(tmp_path)
    t = torrent(labels=[getattr(sl, blocking_tag_attr)])
    assert sl.set_limits(t, max_ratio=5.0, max_seeding_time=43200) == []
    assert client.of("set_torrent") == []


# -- update_share_limits_for_group ---------------------------------------------------------------------


def run_group(tmp_path, group_options, torrents, **kwargs):
    sl, client = make(tmp_path, groups={"noHL": group(**group_options)}, torrents=torrents, **kwargs)
    sl.update_share_limits_for_group("noHL", sl.share_limits_config["noHL"], torrents)
    return sl, client


def test_new_torrent_gets_tagged_and_limited(tmp_path):
    t = torrent()
    sl, client = run_group(tmp_path, {"max_ratio": 5.0, "max_seeding_time": 43200}, [t])
    assert client.of("set_labels", t)[-1][2] == ["~share_limit_1.noHL"]
    assert limit_fields(client, t)
    assert t["name"] in sl.torrents_updated and sl.stats_tagged == 1


def test_torrent_already_correctly_tagged_is_skipped(tmp_path):
    t = torrent(labels=["~share_limit_1.noHL"], mode=tr.LIMIT_SINGLE, limit=5.0, ratio=0.1, seeding=10)
    sl, client = run_group(tmp_path, {"max_ratio": 5.0, "max_seeding_time": 43200, "limit_upload_speed": -1}, [t])
    assert client.calls == []
    assert t["name"] not in sl.torrents_updated


def test_add_group_to_tag_false_skips_tagging(tmp_path):
    t = torrent()
    _, client = run_group(tmp_path, {"max_ratio": 5.0, "add_group_to_tag": False}, [t])
    assert client.of("set_labels") == []
    assert limit_fields(client, t)


def test_enable_group_upload_speed_divides_across_torrents(tmp_path):
    torrents = [torrent(n) for n in range(1, 5)]
    _, client = run_group(tmp_path, {"limit_upload_speed": 1000, "enable_group_upload_speed": True}, torrents)
    for t in torrents:
        assert upload_calls(client, t) == [{"upload_limited": True, "upload_limit": 256}]  # 250 KiB/s


def test_cleanup_eligible_torrents_queued_into_tdel_dict(tmp_path):
    t = torrent(ratio=3.0, seeding=99999)
    sl, _ = run_group(tmp_path, {"max_ratio": 2.0, "cleanup": True}, [t])
    assert sl.tdel_dict[t["hash_string"]]["torrent"] is t
    assert sl.tdel_dict[t["hash_string"]]["body"]


def test_cleanup_skips_torrents_under_limit(tmp_path):
    sl, _ = run_group(tmp_path, {"max_ratio": 2.0, "cleanup": True}, [torrent(ratio=0.5)])
    assert sl.tdel_dict == {}


def test_multiple_share_limit_tags_triggers_retag(tmp_path):
    t = torrent(labels=["~share_limit_1.noHL", "~share_limit_99.old", "keep"])
    _, client = run_group(tmp_path, {}, [t])
    assert client.of("set_labels", t)[-1][2] == ["keep", "~share_limit_1.noHL"]


def test_custom_tag_replaces_the_generated_tag(tmp_path):
    t = torrent(labels=["~share_limit_1.noHL"])
    _, client = run_group(tmp_path, {"custom_tag": "sl-movies"}, [t])
    assert client.of("set_labels", t)[-1][2] == ["sl-movies"]


def test_throttle_after_limit_lifts_limits_and_sets_speed(tmp_path):
    t = torrent(ratio=3.0)
    _, client = run_group(tmp_path, {"max_ratio": 2.0, "upload_speed_on_limit_reached": 100}, [t])
    assert limits_lifted(client, t)
    assert upload_calls(client, t)[-1] == {"upload_limited": True, "upload_limit": 102}


def test_resume_after_change_starts_a_stopped_complete_torrent(tmp_path):
    t = torrent(status=tr.STATUS_STOPPED)
    _, client = run_group(tmp_path, {"max_ratio": 5.0}, [t])
    assert client.of("start", t)


# -- grouping ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, include_all, include_any, exclude_all, exclude_any, expected",
    [
        (["a"], None, None, None, None, True),
        ([], None, None, None, None, True),
        (["a", "b"], ["a", "b"], None, None, None, True),
        (["a"], ["a", "b"], None, None, None, False),
        (["a", "b", "c"], ["a", "b"], None, None, None, True),
        (["a"], None, ["a", "b"], None, None, True),
        (["b"], None, ["a", "b"], None, None, True),
        (["c"], None, ["a", "b"], None, None, False),
        (["a", "b"], None, None, ["a", "b"], None, False),
        (["a"], None, None, ["a", "b"], None, True),
        (["a"], None, None, None, ["a", "b"], False),
        (["c"], None, None, None, ["a", "b"], True),
        (["a", "b"], ["a"], None, None, ["x"], True),
        (["a", "x"], ["a"], None, None, ["x"], False),
    ],
)
def test_check_tags(tags, include_all, include_any, exclude_all, exclude_any, expected):
    assert sl_mod.check_tags(tags, include_all, include_any, exclude_all, exclude_any) is expected


PRIORITY_GROUPS = {
    "noHL": group(priority=1, include_all_tags=["noHL"]),
    "cross-seed": group(priority=2, include_all_tags=["cross-seed"]),
    "default": group(priority=999),
}


def test_get_share_limit_group_first_priority_wins(tmp_path):
    sl, _ = make(tmp_path, groups=PRIORITY_GROUPS)
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(labels=["noHL", "cross-seed"])) == "noHL"


def test_get_share_limit_group_falls_through_to_default(tmp_path):
    sl, _ = make(tmp_path, groups=PRIORITY_GROUPS)
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(labels=["something-else"])) == "default"


def test_get_share_limit_group_returns_none_when_no_default(tmp_path):
    sl, _ = make(tmp_path, groups={"noHL": group(include_all_tags=["noHL"])})
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(labels=["other"])) is None


def test_get_share_limit_group_respects_size(tmp_path):
    groups = {"big": group(priority=1, min_torrent_size="1GB"), "default": group(priority=2, max_torrent_size=500)}
    sl, _ = make(tmp_path, groups=groups)
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(size=2 * 1024**3)) == "big"
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(size=100)) == "default"
    assert sl_mod.get_share_limit_group(sl.share_limits_config, torrent(size=1000)) is None


def test_assign_torrents_to_group_uses_priority_order(tmp_path):
    items = [
        torrent(1, name="A", labels=["noHL"]),
        torrent(2, name="B", labels=["cross-seed"]),
        torrent(3, name="C", labels=["noHL", "cross-seed"]),
        torrent(4, name="D", labels=["random"]),
    ]
    sl, _ = make(tmp_path, groups=PRIORITY_GROUPS)
    groups = sl.assign_torrents_to_group(items)
    assert {name: [t["name"] for t in ts] for name, ts in groups.items()} == {
        "noHL": ["A", "C"],
        "cross-seed": ["B"],
        "default": ["D"],
    }


def test_assign_torrents_unmatched_dropped_when_no_default(tmp_path):
    sl, _ = make(tmp_path, groups={"noHL": group(include_all_tags=["noHL"])})
    assert sl.assign_torrents_to_group([torrent(labels=["random"])]) == {"noHL": []}


# -- cleanup_torrents_for_group ------------------------------------------------------------------------


def queue(sl, t, body="  Ratio vs Max Ratio: 3.00 >= 2.00"):
    sl.tdel_dict[t["hash_string"]] = {"torrent": t, "content_path": sl_mod.content_path(t), "body": body}


def cleanup_setup(tmp_path, exists=True, **kwargs):
    data = tmp_path / "data"
    t = torrent(name="A", download_dir=str(data))
    if exists:
        (data / "A").mkdir(parents=True)
    sl, client = make(tmp_path, groups={"noHL": group(cleanup=True)}, torrents=[t], **kwargs)
    queue(sl, t)
    return sl, client, t


def test_cleanup_deletes_torrent_and_dispatches_notification(tmp_path):
    sl, client, t = cleanup_setup(tmp_path)
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == [("remove", t["hash_string"], True)]
    name, payload = sl.ctx.webhooks.sent[0]
    assert name == "cleanup_share_limits" and payload["torrents"][0]["name"] == "A"
    assert sl.stats_deleted_contents == 1


def test_cleanup_skips_when_content_path_changed(tmp_path):
    sl, client, t = cleanup_setup(tmp_path)
    client.items = [dict(t, download_dir="/data/torrents/SOMETHING_ELSE")]
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == []


def test_cleanup_skips_a_torrent_that_is_gone(tmp_path):
    sl, client, _ = cleanup_setup(tmp_path)
    client.items = []
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == []


def test_cleanup_with_cross_seed_working_keeps_contents(tmp_path):
    sl, client, t = cleanup_setup(tmp_path)
    sl.cross_seed = {t["hash_string"]: True}
    sl.working_names = {"A"}
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == [("remove", t["hash_string"], False)]
    assert sl.stats_deleted == 1


def test_cleanup_with_no_cross_seed_deletes_contents(tmp_path):
    sl, client, t = cleanup_setup(tmp_path)
    sl.cross_seed = {t["hash_string"]: False}
    sl.working_names = {"A"}
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == [("remove", t["hash_string"], True)]


def test_cleanup_missing_path_deletes_torrent_only(tmp_path):
    sl, client, t = cleanup_setup(tmp_path, exists=False)
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == [("remove", t["hash_string"], False)]


def test_cleanup_dry_run_does_not_call_delete(tmp_path):
    sl, client, _ = cleanup_setup(tmp_path, dry_run=True)
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == []
    assert sl.stats_deleted_contents == 1


def test_cleanup_asks_the_pre_remove_hook(tmp_path):
    hooks = Hooks(allow=False)
    sl, client, t = cleanup_setup(tmp_path, hooks=hooks)
    sl.cleanup_torrents_for_group("noHL", 1)
    assert client.of("remove") == []
    payload = hooks.payloads[0]
    assert payload["command"] == "share_limits" and payload["grouping"] == "noHL"
    assert payload["hash"] == t["hash_string"] and payload["delete_data"] is True
    assert payload["reason"].startswith("Ratio vs Max Ratio")


def test_cleanup_batches_notifications_into_one_per_group(tmp_path):
    data = tmp_path / "data"
    items = [torrent(n, name=f"T{n}", download_dir=str(data)) for n in range(1, 13)]
    sl, _ = make(tmp_path, groups={"noHL": group(cleanup=True)}, torrents=items)
    for t in items:
        queue(sl, t)
    sl.cleanup_torrents_for_group("noHL", 1)
    assert len(sl.ctx.webhooks.sent) == 1
    assert {item["name"] for item in sl.ctx.webhooks.sent[0][1]["torrents"]} == {f"T{n}" for n in range(1, 13)}


# -- update_share_limits end to end --------------------------------------------------------------------


def test_update_share_limits_end_to_end_tags_and_cleans(tmp_path):
    data = tmp_path / "data"
    (data / "B").mkdir(parents=True)
    t_nohl = torrent(1, name="A", labels=["noHL"], download_dir=str(data))
    t_cross = torrent(2, name="B", labels=["cross-seed"], ratio=10.0, seeding=99999, download_dir=str(data))
    t_default = torrent(3, name="C", labels=["random"], download_dir=str(data))
    groups = {
        "noHL": group(priority=1, include_all_tags=["noHL"], max_ratio=5.0, max_seeding_time=43200),
        "cross-seed": group(priority=2, include_all_tags=["cross-seed"], max_ratio=2.0, cleanup=True),
        "default": group(priority=999),
    }
    sl, client = make(tmp_path, groups=groups, torrents=[t_nohl, t_cross, t_default])
    stats = sl.run()
    assert "~share_limit_1.noHL" in client.of("set_labels", t_nohl)[-1][2]
    assert limit_fields(client, t_nohl)
    assert client.of("remove") == [("remove", t_cross["hash_string"], True)]
    assert "~share_limit_999.default" in client.of("set_labels", t_default)[-1][2]
    assert stats["deleted_contents"] == 1


def test_filter_completed_skips_incomplete_torrents(tmp_path):
    t = torrent(done=0.5)
    sl, client = make(tmp_path, torrents=[t])
    sl.run()
    assert client.calls == []


def test_runs_from_the_command_line(tmp_path, jsonrpc_server):
    path = tmp_path / "config.yml"
    path.write_text(
        f"transmission:\n  host: '{jsonrpc_server.host}'\n  retries: 0\n"
        "share_limits:\n  default:\n    priority: 1\n    max_ratio: 2.0\n",
        encoding="utf-8",
    )
    assert transmission_manage.main(["-c", str(path), "--share-limits", "--dry-run"]) == 0


# -- Transmission: seeding-time limit, session defaults ------------------------------------------------


def test_reached_seeding_time_limit_stops_the_torrent(tmp_path):
    t = torrent(seeding=43200 * 60)
    sl, client = run_group(tmp_path, {"max_seeding_time": 43200}, [t])
    assert client.of("stop", t) and sl.stats_stopped == 1


def test_seeding_time_limit_not_reached_keeps_seeding(tmp_path):
    t = torrent(seeding=43199 * 60)
    _, client = run_group(tmp_path, {"max_seeding_time": 43200}, [t])
    assert client.of("stop") == []


def test_seeding_time_stop_is_logged_only_in_dry_run(tmp_path):
    t = torrent(seeding=43200 * 60)
    sl, client = run_group(tmp_path, {"max_seeding_time": 43200}, [t], dry_run=True)
    assert client.calls == [] and sl.stats_stopped == 1


def test_exclusion_tag_lifts_the_seeding_time_limit(tmp_path):
    t = torrent(seeding=43200 * 60, seeds=0)
    _, client = run_group(tmp_path, {"max_seeding_time": 43200, "min_num_seeds": 5}, [t])
    assert client.of("stop") == []


def test_throttling_group_does_not_stop_at_the_seeding_time_limit(tmp_path):
    t = torrent(seeding=43200 * 60)
    _, client = run_group(tmp_path, {"max_seeding_time": 43200, "upload_speed_on_limit_reached": 50}, [t])
    assert client.of("stop") == []


def test_stopped_or_incomplete_torrents_are_not_stopped_again(tmp_path):
    items = [torrent(1, seeding=43200 * 60, status=tr.STATUS_STOPPED), torrent(2, seeding=43200 * 60, done=0.5)]
    _, client = run_group(tmp_path, {"max_seeding_time": 43200, "resume_torrent_after_change": False}, items)
    assert client.of("stop") == []


def test_default_session_limits_are_disabled(tmp_path):
    sl, client = make(tmp_path, session={"seed_ratio_limited": True, "seed_ratio_limit": 2.0})
    sl.read_session()
    assert client.of("set_session") == [("set_session", None, {"seed_ratio_limited": False, "idle_seeding_limit_enabled": False})]
    assert sl.global_max_ratio_enabled is False


def test_default_session_limits_are_kept_when_disabled_in_settings(tmp_path):
    sl, client = make(
        tmp_path,
        settings={"disable_qbt_default_share_limits": False},
        session={"seed_ratio_limited": True, "seed_ratio_limit": 2.0},
    )
    sl.read_session()
    assert client.of("set_session") == []
    assert sl.global_max_ratio_enabled is True and sl.global_max_ratio == 2.0


def test_default_session_limits_untouched_in_dry_run(tmp_path):
    sl, client = make(tmp_path, dry_run=True, session={"idle_seeding_limit_enabled": True})
    sl.read_session()
    assert client.of("set_session") == []


def test_speed_unit_comes_from_the_session(tmp_path):
    sl, client = make(tmp_path, session={"units": {"speed_bytes": 1024}})
    sl.read_session()
    t = torrent()
    sl.set_upload_limit(t, 500)
    assert upload_calls(client, t) == [{"upload_limited": True, "upload_limit": 500}]


def test_recheck_does_not_resume_past_the_seeding_time_limit(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"share_limits": {"default": group(max_seeding_time=60)}}), encoding="utf-8")
    config = Config(str(path), {"recheck": True})
    done = torrent(1, seeding=3600, status=tr.STATUS_STOPPED)
    not_yet = torrent(2, seeding=3599, status=tr.STATUS_STOPPED)
    client = FakeClient([done, not_yet])
    rc.run(transmission_manage.Context(client, config, Webhooks(), None))
    assert client.of("start") == [("start", not_yet["hash_string"], None)]


def test_seeding_time_limit_for_a_torrent_outside_any_group(tmp_path):
    sl, _ = make(tmp_path, groups={"noHL": group(include_all_tags=["noHL"], max_seeding_time=60)})
    assert sl_mod.seeding_time_limit(sl.config, torrent()) == -1
    assert sl_mod.seeding_time_limit(sl.config, torrent(labels=["noHL"])) == 60


# -- configuration -------------------------------------------------------------------------------------


def config_of(tmp_path, share_limits, commands=None, settings=None):
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump({"share_limits": share_limits, "settings": settings or {}}), encoding="utf-8")
    return Config(str(path), commands or {"share_limits": True})


def test_default_value_when_not_specified():
    assert validate_share_limit_action(None, cleanup=False, group="g") == "Default"
    assert validate_share_limit_action("", cleanup=True, group="g") == "Default"


@pytest.mark.parametrize("action", ["Default", "Stop"])
def test_valid_action_passes_validator(action):
    assert validate_share_limit_action(action, cleanup=True, group="g") == action


def test_invalid_action_raises_failed():
    with pytest.raises(Failed, match="'Bogus'.*Valid values are: Default, Stop"):
        validate_share_limit_action("Bogus", cleanup=False, group="g")


@pytest.mark.parametrize("action", ["Remove", "RemoveWithContent", "EnableSuperSeeding"])
def test_actions_transmission_cannot_perform_are_rejected(action):
    with pytest.raises(Failed, match="not supported by Transmission"):
        validate_share_limit_action(action, cleanup=False, group="g")


def test_groups_are_sorted_and_missing_priorities_assigned(tmp_path):
    config = config_of(tmp_path, {"last": None, "second": {"priority": 2}, "first": {"priority": 1}})
    assert list(config.share_limits) == ["first", "second", "last"]
    assert config.share_limits["last"]["priority"] == 3


@pytest.mark.parametrize(
    "share_limits, message",
    [
        ({"a": {"priority": 1}, "b": {"priority": 1}}, "Duplicate priority"),
        ({"a": {"custom_tag": "x"}, "b": {"priority": 2, "custom_tag": "x"}}, "Duplicate custom tag"),
        ({"a": {"custom_tag": "noHL"}}, "Duplicate custom tag"),
        ({"a": {"min_seeding_time": 120, "max_seeding_time": 60, "max_ratio": 1}}, "greater than max_seeding_time"),
        ({"a": {"min_seeding_time": 60}}, "max_ratio must be greater than 0"),
        ({"a": {"min_torrent_size": "2GB", "max_torrent_size": "1GB"}}, "greater than max_torrent_size"),
        ({"a": {"categories": ["movies"]}}, "Transmission has no categories"),
        ({"a": {"max_ratio": -3}}, "max_ratio"),
        ({"a": {"max_seeding_time": "soon"}}, "unable to parse"),
        ({}, "No valid grouping"),
    ],
)
def test_invalid_share_limits_fail(tmp_path, share_limits, message):
    with pytest.raises(Failed, match=message):
        config_of(tmp_path, share_limits)


def test_time_and_size_strings_are_parsed(tmp_path):
    config = config_of(
        tmp_path,
        {"a": {"max_seeding_time": "2h30m", "min_last_active": "1d", "min_torrent_size": "1.5MiB", "max_torrent_size": "2GB"}},
    )
    a = config.share_limits["a"]
    assert (a["max_seeding_time"], a["min_last_active"]) == (150, 1440)
    assert (a["min_torrent_size"], a["max_torrent_size"]) == (int(1.5 * 1024**2), 2 * 1024**3)


def test_last_active_is_the_old_name_of_min_last_active(tmp_path):
    assert config_of(tmp_path, {"a": {"last_active": 30}}).share_limits["a"]["min_last_active"] == 30


def test_share_limits_parsed_only_when_used(tmp_path):
    assert config_of(tmp_path, {"a": {"categories": ["x"]}}, commands={"tag_update": True}).share_limits == {}
    assert config_of(tmp_path, {"a": {}}, commands={"recheck": True}).share_limits["a"]["priority"] == 1


def test_sample_share_limits_validate(tmp_path):
    sample = os.path.join(os.path.dirname(__file__), "..", "..", "config", "config.yml.sample")
    with open(sample, encoding="utf-8") as handle:
        text = handle.read().replace("  share_limits: False", "  share_limits: True")
    path = tmp_path / "config.yml"
    path.write_text(text, encoding="utf-8")
    assert list(Config(str(path)).share_limits) == ["noHL", "default"]
