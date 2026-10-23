import os
import textwrap

import pytest

import transmission_manage
from modules.config import Config
from modules.core import tag_nohardlinks as tnh
from modules.util import CheckHardLinks
from modules.util import Failed
from modules.util import path_replace


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "downloads"
    media = tmp_path / "media"
    media.mkdir()
    root.mkdir()
    return root, media


# --- CheckHardLinks -----------------------------------------------------------------------------------


def test_unlinked_file_has_no_hardlinks(layout):
    root, _ = layout
    f = write(root / "single.mkv", 100)
    assert CheckHardLinks(str(root)).nohardlink(str(f)) is True


def test_link_outside_root_counts(layout):
    root, media = layout
    f = write(root / "single.mkv", 100)
    os.link(f, media / "single.mkv")
    assert CheckHardLinks(str(root)).nohardlink(str(f)) is False


def test_links_inside_root_are_ignored_unless_disabled(layout):
    root, _ = layout
    f = write(root / "a" / "file.mkv", 100)
    os.link(f, root / "second-copy.mkv")
    checker = CheckHardLinks(str(root))
    assert checker.nohardlink(str(f), ignore_root_dir=True) is True
    assert checker.nohardlink(str(f), ignore_root_dir=False) is False


def test_small_linked_extra_does_not_decide_for_a_folder(layout):
    root, media = layout
    folder = root / "Release"
    write(folder / "movie.mkv", 1000)
    sub = write(folder / "movie.srt", 10)  # below 10% of the largest file
    os.link(sub, media / "movie.srt")
    assert CheckHardLinks(str(root)).nohardlink(str(folder)) is True


def test_linked_main_file_marks_the_folder_linked(layout):
    root, media = layout
    folder = root / "Release"
    main = write(folder / "movie.mkv", 1000)
    write(folder / "sample.mkv", 50)
    os.link(main, media / "movie.mkv")
    assert CheckHardLinks(str(root)).nohardlink(str(folder)) is False


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_unreadable_or_empty_paths_fail_closed(layout, make):
    root, _ = layout
    target = root / "nothing"
    if make == "empty":
        target.mkdir()
    assert CheckHardLinks(str(root)).nohardlink(str(target)) is False


def test_remote_dir_is_walked_when_set(layout, tmp_path):
    root, media = layout
    f = write(root / "a.mkv", 10)
    os.link(f, root / "b.mkv")
    checker = CheckHardLinks("/container/downloads", remote_dir=str(root))
    assert checker.nohardlink(str(f)) is True  # the in-root twin was counted from remote_dir


def test_path_replace():
    assert path_replace("/container/downloads/x/y.mkv", "/container/downloads", "/host/dl") == os.path.normpath(
        "/host/dl/x/y.mkv"
    )
    assert path_replace("/elsewhere/y.mkv", "/container/downloads", "/host/dl") == "/elsewhere/y.mkv"
    assert path_replace("/a/b", "/a", None) == "/a/b"


# --- module -------------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, torrents):
        self.items = torrents
        self.calls = []

    def torrents(self, fields, hashes=None):
        return [dict(t) for t in self.items]

    def set_labels(self, hashes, labels):
        self.calls.append((tuple(hashes), tuple(labels)))


class Hooks:
    def function(self, name, payload):
        pass


def tor(n, download_dir, name, labels, done=1.0):
    return {"hash_string": f"{n:040x}", "name": name, "download_dir": str(download_dir), "labels": labels, "percent_done": done}


def context(tmp_path, root, torrents, nohardlinks, commands=None):
    path = tmp_path / "config.yml"
    path.write_text(
        f"directory:\n  root_dir: '{root.as_posix()}'\n" + textwrap.dedent(nohardlinks),
        encoding="utf-8",
    )
    cmd = {"tag_nohardlinks": True, "dry_run": False}
    cmd.update(commands or {})
    config = Config(str(path), cmd)
    client = FakeClient(torrents)
    return transmission_manage.Context(client, config, Hooks(), None), client


SECTION = "nohardlinks:\n  label-a:\n  label-b:\n    exclude_tags: [manual]\n"


def test_labels_unlinked_torrents_and_skips_excluded_ones(tmp_path, layout):
    root, media = layout
    write(root / "Gone.mkv", 10)
    linked = write(root / "Kept.mkv", 10)
    os.link(linked, media / "Kept.mkv")
    write(root / "Manual.mkv", 10)
    items = [
        tor(1, root, "Gone.mkv", ["label-a"]),
        tor(2, root, "Kept.mkv", ["label-a"]),
        tor(3, root, "Manual.mkv", ["label-b", "manual"]),
        tor(4, root, "Other.mkv", ["unrelated"]),
    ]
    ctx, client = context(tmp_path, root, items, SECTION)
    stats = tnh.run(ctx)
    assert client.calls == [((f"{1:040x}",), ("label-a", "noHL"))]
    assert stats["tagged"] == 1 and stats["excluded"] == 1 and stats["checked"] == 3


def test_label_is_removed_once_hardlinks_appear(tmp_path, layout):
    root, media = layout
    f = write(root / "Now.Imported.mkv", 10)
    os.link(f, media / "Now.Imported.mkv")
    ctx, client = context(tmp_path, root, [tor(1, root, "Now.Imported.mkv", ["label-a", "noHL"])], SECTION)
    assert tnh.run(ctx)["untagged"] == 1
    assert client.calls == [((f"{1:040x}",), ("label-a",))]


def test_dry_run_and_incomplete_filter(tmp_path, layout):
    root, _ = layout
    write(root / "A.mkv", 10)
    write(root / "B.mkv", 10)
    items = [tor(1, root, "A.mkv", ["label-a"]), tor(2, root, "B.mkv", ["label-a"], done=0.5)]
    ctx, client = context(tmp_path, root, items, SECTION, commands={"dry_run": True})
    stats = tnh.run(ctx)
    assert client.calls == [] and stats["tagged"] == 1 and stats["checked"] == 1


def test_torrent_with_two_configured_labels_is_checked_once(tmp_path, layout):
    root, _ = layout
    write(root / "Both.mkv", 10)
    ctx, client = context(tmp_path, root, [tor(1, root, "Both.mkv", ["label-a", "label-b"])], SECTION)
    assert tnh.run(ctx)["checked"] == 1 and len(client.calls) == 1


def test_global_options_merge():
    section = {"global_options": {"exclude_tags": ["g"], "ignore_root_dir": False}, "label-a": {"exclude_tags": ["r"]}}
    assert tnh.label_options(section, "label-a") == {"exclude_tags": ["g", "r"], "ignore_root_dir": False}


def test_needs_root_dir_and_a_label(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("nohardlinks:\n  label-a:\n", encoding="utf-8")
    ctx = transmission_manage.Context(FakeClient([]), Config(str(path), {"tag_nohardlinks": True}), Hooks(), None)
    with pytest.raises(Failed, match="root_dir"):
        tnh.run(ctx)
