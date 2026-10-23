"""tag_nohardlinks, ported from qbit_manage's modules/core/tag_nohardlinks.py.

Transmission has no categories, so the nohardlinks section is keyed by Transmission label instead. Everything
else follows upstream: global_options merged into
each label's options, exclude_tags (labels here) skip a torrent, and the label is removed again once hardlinks
appear.
"""

from __future__ import annotations

import logging
import os

from modules import transmission as tr
from modules.util import CheckHardLinks
from modules.util import Failed
from modules.util import path_replace

logger = logging.getLogger("transmission_manage")

FIELDS = ["name", "download_dir", "labels", "percent_done"]


def label_options(nohardlinks, label):
    """Effective options for one label: global exclude_tags are merged, ignore_root_dir is overridden per label."""
    global_options = nohardlinks.get("global_options") or {}
    own = nohardlinks.get(label) or {}
    return {
        "exclude_tags": sorted(set(global_options.get("exclude_tags") or []) | set(own.get("exclude_tags") or [])),
        "ignore_root_dir": own.get("ignore_root_dir", global_options.get("ignore_root_dir", True)),
    }


class TagNoHardLinks:
    def __init__(self, context):
        self.ctx = context
        self.client = context.client
        config = context.config
        self.dry_run = context.dry_run
        self.tag = config.settings["nohardlinks_tag"]
        self.filter_completed = config.settings["tag_nohardlinks_filter_completed"]
        self.nohardlinks = config.nohardlinks
        self.root_dir = config.directory["root_dir"]
        self.remote_dir = config.directory["remote_dir"]
        if not self.root_dir:
            raise Failed("tag_nohardlinks needs directory.root_dir")
        if not self.nohardlinks or set(self.nohardlinks) == {"global_options"}:
            raise Failed("tag_nohardlinks needs at least one label in the nohardlinks section")
        self.checker = CheckHardLinks(self.root_dir, self.remote_dir, config.directory["orphaned_dir"])
        self.stats = {"checked": 0, "no_hardlinks": 0, "tagged": 0, "untagged": 0, "excluded": 0}
        self.events = {"tag_nohardlinks": [], "untag_nohardlinks": []}

    def _set_labels(self, torrent, labels):
        if not self.dry_run:
            self.client.set_labels([torrent["hash_string"]], labels)
        torrent["labels"] = labels

    def run(self):
        torrents = self.client.torrents(FIELDS)
        seen = set()
        for label in (key for key in self.nohardlinks if key != "global_options"):
            options = label_options(self.nohardlinks, label)
            selected = [
                t
                for t in torrents
                if label in (t.get("labels") or [])
                and t["hash_string"] not in seen
                and (not self.filter_completed or (t.get("percent_done") or 0) >= 1)
            ]
            if not selected:
                logger.warning("No torrents with the label '%s' from the nohardlinks section", label)
                continue
            no_hl = 0
            for torrent in selected:
                seen.add(torrent["hash_string"])
                try:
                    no_hl += self._process(torrent, options)
                except tr.TransmissionError as err:
                    logger.error("%s: %s", torrent.get("name"), err)
            logger.info("Label '%s': checked %d torrent(s), %d with no hardlinks", label, len(selected), no_hl)
        for function, items in self.events.items():
            if items:
                self.ctx.webhooks.function(function, {"dry_run": self.dry_run, "torrents": items})
        logger.info(
            "No hardlinks: %d label(s) %s, %d removed",
            self.stats["tagged"],
            "would be added" if self.dry_run else "added",
            self.stats["untagged"],
        )
        return self.stats

    def _process(self, torrent, options):
        self.stats["checked"] += 1
        content = os.path.join(torrent.get("download_dir") or "", torrent.get("name") or "")
        has_no_hardlinks = self.checker.nohardlink(
            path_replace(content, self.root_dir, self.remote_dir), options["ignore_root_dir"]
        )
        labels = list(torrent.get("labels") or [])
        if has_no_hardlinks:
            self.stats["no_hardlinks"] += 1
        if set(labels) & set(options["exclude_tags"]):
            self.stats["excluded"] += 1
            logger.debug("Torrent '%s' has an excluded label; skipping nohardlinks tagging", torrent["name"])
            return int(has_no_hardlinks)
        if has_no_hardlinks and self.tag not in labels:
            self._set_labels(torrent, [*labels, self.tag])
            self.stats["tagged"] += 1
            self.events["tag_nohardlinks"].append({"name": torrent["name"]})
            logger.info("%s label %s: %s", self.tag, "would be added" if self.dry_run else "added", torrent["name"])
        elif not has_no_hardlinks and self.tag in labels:
            self._set_labels(torrent, [item for item in labels if item != self.tag])
            self.stats["untagged"] += 1
            self.events["untag_nohardlinks"].append({"name": torrent["name"]})
            logger.info("%s label removed, hardlinks found now: %s", self.tag, torrent["name"])
        return int(has_no_hardlinks)


def run(context):
    return TagNoHardLinks(context).run()
