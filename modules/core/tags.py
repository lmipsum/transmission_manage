"""tag_update, ported from qbit_manage's modules/core/tags.py and the tracker-tag lookup of get_tags().

Behaviour follows upstream: each torrent gets the tag(s) configured for its tracker, the stalled tag while it is
stalled downloading (removed again afterwards) and the private tag when it is private. Tags are Transmission
labels and are added to the existing ones, never replacing them.

Transmission-specific: "stalledDL" is a downloading torrent (status 4) with is_stalled set; privacy comes from
is_private; tracker URLs come from tracker_stats.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from modules import transmission as tr
from modules.util import redact

logger = logging.getLogger("transmission_manage")

FIELDS = ["name", "labels", "status", "is_stalled", "is_private", "tracker_stats"]
URL_SCHEMES = ("http", "udp", "ws")


def tracker_urls(torrent):
    """Announce URLs of a torrent, as upstream's get_tracker_urls (http, udp and ws schemes)."""
    return [
        t.get("announce") or "" for t in torrent.get("tracker_stats") or [] if (t.get("announce") or "").startswith(URL_SCHEMES)
    ]


class TrackerTags:
    """get_tags(): first configured keyword found in a URL wins; then the 'other' entry; then the tracker's host."""

    def __init__(self, tracker_config):
        self.config = dict(tracker_config or {})
        self.other = self.config.get("other")

    def resolve(self, urls):
        """Return (tags, display_url) for a torrent's tracker URLs."""
        if not urls:
            return list(self.other or ["other"]), "No http URL found"
        display = redact(urls[0])
        for keyword, tags in self.config.items():
            for url in urls:
                if keyword in url:
                    return list(tags), redact(url)
        if self.other:
            return list(self.other), display
        host = urlsplit(urls[0]).hostname or urls[0]
        # Upstream records the host as a new tracker entry, so the warning comes once per host.
        self.config[host] = [host]
        logger.warning("No tags matched for %s. Please check your config.yml file. Setting tag to %s", display, host)
        return [host], display


class Tags:
    def __init__(self, context):
        self.ctx = context
        self.client = context.client
        settings = context.config.settings
        self.dry_run = context.dry_run
        self.stalled_tag = settings["stalled_tag"]
        self.private_tag = settings["private_tag"]
        self.tag_stalled = settings["tag_stalled_torrents"]
        self.lookup = TrackerTags(context.config.tracker)
        self.stats = 0
        self.updated = []

    def run(self):
        logger.info("Updating Tags")
        for torrent in self.client.torrents(FIELDS):
            try:
                self._process(torrent)
            except tr.TransmissionError as err:
                logger.error("%s: %s", torrent.get("name"), err)
        if self.updated:
            self.ctx.webhooks.function("tag_update", {"dry_run": self.dry_run, "torrents": self.updated})
        if self.stats:
            logger.info("%s %d new tags.", "Did not update" if self.dry_run else "Updated", self.stats)
        else:
            logger.info("No new torrents to tag.")
        return {"tagged": self.stats}

    def _process(self, torrent):
        labels = list(torrent.get("labels") or [])
        tracker_tags, url = self.lookup.resolve(tracker_urls(torrent))
        stalled = torrent.get("status") == tr.STATUS_DOWNLOAD and bool(torrent.get("is_stalled"))
        private = bool(torrent.get("is_private"))
        new_labels = list(labels)

        if self.tag_stalled and self.stalled_tag in new_labels and not stalled:
            new_labels.remove(self.stalled_tag)
            logger.info("Torrent Name: %s - Removing Tag: %s [%s]", torrent["name"], self.stalled_tag, url)

        if (
            not labels
            or not all(tag in labels for tag in tracker_tags)
            or (self.tag_stalled and stalled and self.stalled_tag not in labels)
            or (self.private_tag and self.private_tag not in labels and private)
        ):
            to_add = list(tracker_tags)
            if self.tag_stalled and stalled:
                to_add.append(self.stalled_tag)
            if self.private_tag and private:
                to_add.append(self.private_tag)
            if to_add:
                self.stats += len(to_add)
                self.updated.append({"name": torrent["name"], "tags": to_add, "tracker": url})
                logger.info("Torrent Name: %s - New Tags: %s [%s]", torrent["name"], ", ".join(to_add), url)
                new_labels += [tag for tag in to_add if tag not in new_labels]

        if new_labels != labels and not self.dry_run:
            self.client.set_labels([torrent["hash_string"]], new_labels)


def run(context):
    return Tags(context).run()
