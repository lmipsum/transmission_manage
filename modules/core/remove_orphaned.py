"""rem_orphaned and the orphaned-folder cleanup, ported from qbit_manage's modules/core/remove_orphaned.py and
Config.cleanup_dirs("Orphaned Data").

Behaviour follows upstream. Transmission-specific: torrent file paths are download_dir + files[].name, and for
torrents that are not complete the session's incomplete_dir and the ".part" suffix stand in for qBittorrent's
download_path, so files still being downloaded are never taken for orphans. Download directories of torrents take
the place of category save paths when empty folders are removed.
"""

from __future__ import annotations

import logging
import os
import time
import unicodedata
from fnmatch import fnmatch

from modules.util import delete_files
from modules.util import get_root_files
from modules.util import move_files
from modules.util import path_replace
from modules.util import remove_empty_directories

logger = logging.getLogger("transmission_manage")

PARTIAL_SUFFIX = ".part"


def find_orphaned_files(root_files, torrent_files):
    """Diff on-disk files against torrent files, comparing Unicode-NFC-normalised paths (upstream)."""
    torrent_files_nfc = {unicodedata.normalize("NFC", f) for f in torrent_files}
    return {f for f in root_files if unicodedata.normalize("NFC", f) not in torrent_files_nfc}


def torrent_file_paths(torrent, incomplete_dir=None):
    """Every path a torrent's files can occupy (root_dir form)."""
    names = [item.get("name") or "" for item in torrent.get("files") or []]
    bases = [torrent.get("download_dir") or ""]
    complete = (torrent.get("percent_done") or 0) >= 1
    if not complete and incomplete_dir:
        bases.append(incomplete_dir)
    paths = set()
    for base in bases:
        for name in names:
            path = os.path.normpath(os.path.join(base, name))
            paths.add(path)
            if not complete:
                paths.add(path + PARTIAL_SUFFIX)
    return paths


class RemoveOrphaned:
    def __init__(self, context, now=time.time):
        self.ctx = context
        self.client = context.client
        config = context.config
        self.dry_run = context.dry_run
        self.root_dir = config.directory["root_dir"]
        self.remote_dir = config.directory["remote_dir"]
        self.orphaned_dir = config.directory["orphaned_dir"]
        self.orphaned = config.orphaned
        self.now = now
        self.stats = {"orphaned": 0}

    def _host_path(self, path):
        return path_replace(path, self.root_dir, self.remote_dir)

    def run(self):
        logger.info("Checking for orphaned files")
        torrents = self.client.torrents(["name", "download_dir", "files", "percent_done"])
        session = self.client.session(["incomplete_dir", "incomplete_dir_enabled"])
        incomplete_dir = session.get("incomplete_dir") if session.get("incomplete_dir_enabled") else None
        root_files = set(get_root_files(self.root_dir, self.remote_dir, self.orphaned_dir))
        torrent_files = set()
        for torrent in torrents:
            torrent_files |= torrent_file_paths(torrent, incomplete_dir)

        orphaned_files = find_orphaned_files(root_files, torrent_files)
        if self.orphaned["exclude_patterns"]:
            patterns = [path_replace(pattern, self.remote_dir, self.root_dir) for pattern in self.orphaned["exclude_patterns"]]
            orphaned_files = {f for f in orphaned_files if not any(fnmatch(f, pattern) for pattern in patterns)}
        orphaned_files = self._filter_too_new(orphaned_files)

        if not orphaned_files:
            logger.info("No Orphaned Files found.")
            return self.stats
        limit = self.orphaned["max_orphaned_files_to_delete"]
        if limit != -1 and len(orphaned_files) > limit:
            message = (
                f"Too many orphaned files detected ({len(orphaned_files)}). Max Threshold for deletion is set to {limit}. "
                "Aborting deletion to avoid accidental data loss."
            )
            logger.info("Orphaned files detected: %s", sorted(orphaned_files))
            logger.warning(message)
            self.ctx.webhooks.error(message, function="rem_orphaned", critical=False)
            return self.stats

        orphaned_files = sorted(orphaned_files)
        self.stats["orphaned"] = len(orphaned_files)
        delete = self.orphaned["empty_after_x_days"] == 0
        logger.info("%d Orphaned files found", len(orphaned_files))
        for file in orphaned_files:
            logger.info("%s", file)
        logger.info(
            "%s %d Orphaned files%s",
            ("Not deleting" if self.dry_run else "Deleting") if delete else ("Not moving" if self.dry_run else "Moving"),
            len(orphaned_files),
            "" if delete else f" to {self.orphaned_dir}",
        )
        self.ctx.webhooks.function(
            "rem_orphaned",
            {
                "dry_run": self.dry_run,
                "orphaned_files": orphaned_files,
                "orphaned_directory": self.orphaned_dir,
                "total_orphaned_files": len(orphaned_files),
            },
        )
        if self.dry_run:
            return self.stats

        parents = set()
        for file in orphaned_files:
            parent = self._handle(file, delete)
            if parent:
                parents.add(parent)
        if parents:
            logger.info("Removing newly empty directories")
            protected = {self._host_path(t.get("download_dir") or "") for t in torrents} | {self._host_path(self.root_dir)}
            patterns = [path_replace(p, self.remote_dir, self.root_dir) for p in self.orphaned["exclude_patterns"]]
            for directory in parents:
                remove_empty_directories(directory, protected, patterns)
        return self.stats

    def _filter_too_new(self, orphaned_files):
        """Protect candidates whose mtime is younger than min_file_age_minutes (upstream: mtime only)."""
        minimum = self.orphaned["min_file_age_minutes"]
        if minimum <= 0:
            return orphaned_files
        protected = set()
        now = self.now()
        for file in orphaned_files:
            try:
                age = (now - os.stat(self._host_path(file)).st_mtime) / 60
            except OSError as err:
                logger.warning("Error checking file age for %s: %s", file, err)
                continue  # unreadable: left eligible, as upstream
            if age < minimum:
                protected.add(file)
                logger.info(
                    "Skipping orphaned file (too new): %s (age %.1f mins < %d mins)", os.path.basename(file), age, minimum
                )
        if protected:
            logger.info("Protected %d orphaned files from deletion due to age filter", len(protected))
        return orphaned_files - protected

    def _handle(self, file, delete):
        src = self._host_path(file)
        dest = os.path.join(self.orphaned_dir, os.path.relpath(os.path.normpath(file), os.path.normpath(self.root_dir)))
        parent = self._host_path(os.path.dirname(file))
        try:
            if delete:
                delete_files(src)
            else:
                move_files(src, dest, True)
        except PermissionError as err:
            logger.warning("Permission denied processing orphaned file %s: %s. Skipping file.", file, err)
            return None
        except OSError as err:
            logger.error("Error processing orphaned file %s: %s", file, err)
            if delete:
                try:
                    move_files(src, dest, True)
                except OSError as move_err:
                    logger.error("Error moving orphaned file %s: %s", file, move_err)
                    return None
        return parent


def cleanup_orphaned_dir(context, now=time.time):
    """Config.cleanup_dirs("Orphaned Data"): delete files older than empty_after_x_days from orphaned_dir."""
    config = context.config
    days_limit = config.orphaned["empty_after_x_days"]
    orphaned_dir = config.directory["orphaned_dir"]
    if config.commands["skip_cleanup"] or not config.commands["rem_orphaned"] or days_limit is None:
        return 0
    if not orphaned_dir or not os.path.isdir(orphaned_dir):
        return 0
    deleted, size, names = 0, 0, []
    for path in sorted(set(os.path.join(d, f) for d, _s, fs in os.walk(orphaned_dir) for f in fs)):
        try:
            stat = os.stat(path)
        except OSError as err:
            logger.warning("Error accessing file stats for %s: %s", path, err)
            continue
        days = (now() - stat.st_mtime) / 86400
        if days_limit > days:
            continue
        if not context.dry_run:
            try:
                os.remove(path)
            except OSError as err:
                logger.warning("Error deleting %s: %s. Skipping file.", path, err)
                continue
        deleted += 1
        size += stat.st_size
        names.append(os.path.basename(path))
        logger.info(
            "%s %s from %s (Last modified %d days ago).",
            "Did not delete" if context.dry_run else "Deleted",
            os.path.basename(path),
            orphaned_dir,
            round(days),
        )
    if deleted:
        if not context.dry_run:
            remove_empty_directories(orphaned_dir, [orphaned_dir])
        logger.info("%s %d files from the Orphaned Data.", "Did not delete" if context.dry_run else "Deleted", deleted)
        context.webhooks.function(
            "cleanup_dirs",
            {"location": "Orphaned Data", "files": names, "empty_after_x_days": days_limit, "size_in_bytes": size},
        )
    return deleted


def run(context):
    return RemoveOrphaned(context).run()
