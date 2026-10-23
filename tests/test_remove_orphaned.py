import os
import textwrap
import time
import unicodedata

import pytest

import transmission_manage
from modules.config import Config
from modules.core import remove_orphaned as ro
from modules.util import Failed

DAY = 86400


def write(path, age_days=10, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


class FakeClient:
    def __init__(self, torrents, session=None):
        self.items = torrents
        self.sess = session or {}

    def torrents(self, fields, hashes=None):
        return [dict(t) for t in self.items]

    def session(self, fields=None):
        return dict(self.sess)


class Webhooks:
    def __init__(self):
        self.functions, self.errors = [], []

    def function(self, name, payload):
        self.functions.append((name, payload))

    def error(self, text, function=None, critical=True):
        self.errors.append(text)


def context(tmp_path, root, torrents, orphaned="", commands=None, remote=None, session=None):
    remote_line = f"  remote_dir: '{remote.as_posix()}'\n" if remote else ""
    path = tmp_path / "config.yml"
    path.write_text(f"directory:\n  root_dir: '{root.as_posix()}'\n{remote_line}" + textwrap.dedent(orphaned), encoding="utf-8")
    cmd = {"rem_orphaned": True, "dry_run": False}
    cmd.update(commands or {})
    config = Config(str(path), cmd)
    return transmission_manage.Context(FakeClient(torrents, session), config, Webhooks(), None)


def tor(download_dir, *names, done=1.0):
    return {
        "hash_string": "a" * 40,
        "name": names[0],
        "download_dir": str(download_dir),
        "percent_done": done,
        "files": [{"name": n} for n in names],
    }


# --- helpers ------------------------------------------------------------------------------------------


def test_nfc_and_nfd_names_are_the_same_file():
    nfd = unicodedata.normalize("NFD", "/r/Árvíztűrő.mkv")
    assert ro.find_orphaned_files({nfd, "/r/other.mkv"}, {"/r/Árvíztűrő.mkv"}) == {"/r/other.mkv"}


def test_incomplete_torrent_paths_cover_incomplete_dir_and_part_suffix():
    paths = ro.torrent_file_paths(tor("/dl", "A/a.mkv", done=0.5), incomplete_dir="/inc")
    assert {os.path.normpath(p) for p in ["/dl/A/a.mkv", "/dl/A/a.mkv.part", "/inc/A/a.mkv", "/inc/A/a.mkv.part"]} <= paths
    assert ro.torrent_file_paths(tor("/dl", "A/a.mkv"), incomplete_dir="/inc") == {os.path.normpath("/dl/A/a.mkv")}


# --- rem_orphaned -------------------------------------------------------------------------------------


def test_orphans_are_moved_keeping_their_relative_path(tmp_path):
    root = tmp_path / "dl"
    write(root / "Kept" / "kept.mkv")
    write(root / "Gone" / "gone.mkv")
    write(root / "Gone" / ".DS_Store")
    ctx = context(tmp_path, root, [tor(root, "Kept/kept.mkv")], 'orphaned:\n  exclude_patterns: ["**/.DS_Store"]\n')
    assert ro.run(ctx)["orphaned"] == 1
    moved = root / "orphaned_data" / "Gone" / "gone.mkv"
    assert moved.exists() and not (root / "Gone" / "gone.mkv").exists()
    assert time.time() - moved.stat().st_mtime < 60  # mtime refreshed, so the purge counts from the move
    assert (root / "Gone" / ".DS_Store").exists()  # excluded
    assert (root / "Kept" / "kept.mkv").exists()
    assert ctx.webhooks.functions[0][0] == "rem_orphaned"


def test_newly_empty_folders_are_removed_but_download_dirs_are_kept(tmp_path):
    root = tmp_path / "dl"
    dl_dir = root / "complete"
    write(dl_dir / "Gone" / "gone.mkv")
    write(dl_dir / "Kept.mkv")
    ro.run(context(tmp_path, root, [tor(dl_dir, "Kept.mkv")]))
    assert not (dl_dir / "Gone").exists()
    assert dl_dir.exists()


def test_files_in_the_orphaned_folder_are_never_orphans(tmp_path):
    root = tmp_path / "dl"
    write(root / "orphaned_data" / "old" / "x.mkv")
    ctx = context(tmp_path, root, [])
    assert ro.run(ctx)["orphaned"] == 0


def test_age_protection_skips_recent_files(tmp_path):
    root = tmp_path / "dl"
    write(root / "fresh.mkv", age_days=0)
    ctx = context(tmp_path, root, [], "orphaned:\n  min_file_age_minutes: 30\n")
    assert ro.run(ctx)["orphaned"] == 0 and (root / "fresh.mkv").exists()


def test_too_many_orphans_aborts_without_moving_anything(tmp_path):
    root = tmp_path / "dl"
    for n in range(3):
        write(root / f"o{n}.mkv")
    ctx = context(tmp_path, root, [], "orphaned:\n  max_orphaned_files_to_delete: 2\n")
    assert ro.run(ctx)["orphaned"] == 0
    assert all((root / f"o{n}.mkv").exists() for n in range(3))
    assert "Too many orphaned files" in ctx.webhooks.errors[0]


def test_empty_after_zero_deletes_immediately(tmp_path):
    root = tmp_path / "dl"
    write(root / "gone.mkv")
    ro.run(context(tmp_path, root, [], "orphaned:\n  empty_after_x_days: 0\n"))
    assert not (root / "gone.mkv").exists() and not (root / "orphaned_data" / "gone.mkv").exists()


def test_dry_run_moves_nothing(tmp_path):
    root = tmp_path / "dl"
    write(root / "gone.mkv")
    ctx = context(tmp_path, root, [], commands={"dry_run": True})
    assert ro.run(ctx)["orphaned"] == 1 and (root / "gone.mkv").exists()


def test_incomplete_download_in_the_incomplete_dir_is_not_an_orphan(tmp_path):
    root = tmp_path / "dl"
    write(root / "incomplete" / "New" / "a.mkv.part", age_days=1)
    torrents = [tor(root / "complete", "New/a.mkv", done=0.3)]
    session = {"incomplete_dir": str(root / "incomplete"), "incomplete_dir_enabled": True}
    assert ro.run(context(tmp_path, root, torrents, session=session))["orphaned"] == 0


def test_remote_dir_maps_transmission_paths_to_local_ones(tmp_path):
    local = tmp_path / "local"
    write(local / "Kept.mkv")
    write(local / "gone.mkv")
    # Transmission reports /data/..., this tool sees the same tree under tmp_path/local.
    ctx = context(tmp_path, tmp_path / "container", [tor("/data", "Kept.mkv")], remote=local)
    ctx.config.directory["root_dir"] = "/data"
    ctx.config.directory["orphaned_dir"] = str(local / "orphaned_data")
    assert ro.run(ctx)["orphaned"] == 1
    assert (local / "orphaned_data" / "gone.mkv").exists() and (local / "Kept.mkv").exists()


# --- cleanup of the orphaned folder -------------------------------------------------------------------


def test_cleanup_deletes_only_files_older_than_the_limit(tmp_path):
    root = tmp_path / "dl"
    write(root / "orphaned_data" / "Old" / "old.mkv", age_days=90)
    write(root / "orphaned_data" / "new.mkv", age_days=1)
    ctx = context(tmp_path, root, [], "orphaned:\n  empty_after_x_days: 60\n")
    assert ro.cleanup_orphaned_dir(ctx) == 1
    assert not (root / "orphaned_data" / "Old").exists() and (root / "orphaned_data" / "new.mkv").exists()


@pytest.mark.parametrize("commands", [{"skip_cleanup": True}, {"dry_run": True}])
def test_cleanup_respects_skip_cleanup_and_dry_run(tmp_path, commands):
    root = tmp_path / "dl"
    old = write(root / "orphaned_data" / "old.mkv", age_days=90)
    ctx = context(tmp_path, root, [], "orphaned:\n  empty_after_x_days: 60\n", commands=commands)
    ro.cleanup_orphaned_dir(ctx)
    assert old.exists()


def test_cleanup_without_empty_after_never_deletes(tmp_path):
    root = tmp_path / "dl"
    old = write(root / "orphaned_data" / "old.mkv", age_days=900)
    assert ro.cleanup_orphaned_dir(context(tmp_path, root, [])) == 0 and old.exists()


# --- config -------------------------------------------------------------------------------------------


def test_config_defaults_and_root_dir_requirement(tmp_path):
    ctx = context(tmp_path, tmp_path / "dl", [])
    assert ctx.config.directory["orphaned_dir"] == os.path.join(str(tmp_path / "dl").replace("\\", "/"), "orphaned_data")
    assert ctx.config.orphaned["max_orphaned_files_to_delete"] == 50
    assert ctx.config.orphaned["exclude_patterns"] == [f"**{os.sep}orphaned_data{os.sep}*"]
    path = tmp_path / "bare.yml"
    path.write_text("transmission: {}\n", encoding="utf-8")
    with pytest.raises(Failed, match="root_dir"):
        Config(str(path), {"rem_orphaned": True})
