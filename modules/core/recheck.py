"""recheck, ported from qbit_manage's modules/core/recheck.py.

Behaviour follows upstream: stopped torrents are walked smallest first; a complete one is resumed unless its
share limits are already met, and one at 0% is verified when a torrent with the same name is complete.

Transmission-specific: a per-torrent ratio limit exists only when seed_ratio_mode is 1; the global (0) and
unlimited (2) modes count as no limit, as upstream treats qBittorrent's -2 (global) and -1 (none). Transmission
has no seeding-time limit (its idle limit measures inactivity, not seeding time), so that half of upstream's check
always reads as no limit.
"""

from __future__ import annotations

import logging

from modules import transmission as tr

logger = logging.getLogger("transmission_manage")

FIELDS = ["name", "status", "percent_done", "size_when_done", "upload_ratio", "seed_ratio_mode", "seed_ratio_limit"]
SEED_RATIO_SINGLE = 1


def ratio_limit(torrent):
    """The torrent's own ratio limit, or -1 when it has none (global or unlimited mode)."""
    if torrent.get("seed_ratio_mode") == SEED_RATIO_SINGLE:
        return float(torrent.get("seed_ratio_limit") or 0)
    return -1


def should_resume(torrent):
    """Upstream: resume when no limit is set, or the set limits are not reached yet."""
    limit = ratio_limit(torrent)
    if limit < 0:
        return True
    ratio = tr.ratio(torrent)
    return (ratio if ratio is not None else 0.0) < limit


class ReCheck:
    def __init__(self, context):
        self.ctx = context
        self.client = context.client
        self.dry_run = context.dry_run
        self.stats = {"resumed": 0, "rechecked": 0}
        self.events = {"resumed": [], "rechecked": []}

    def run(self):
        logger.info("Rechecking Paused Torrents")
        torrents = self.client.torrents(FIELDS)
        complete_names = {t["name"] for t in torrents if (t.get("percent_done") or 0) >= 1}
        stopped = sorted(
            (t for t in torrents if t.get("status") == tr.STATUS_STOPPED), key=lambda t: t.get("size_when_done") or 0
        )
        for torrent in stopped:
            try:
                self._process(torrent, complete_names)
            except tr.TransmissionError as err:
                logger.error("%s: %s", torrent.get("name"), err)
        for kind, names in self.events.items():
            if names:
                self.ctx.webhooks.function("recheck", {"dry_run": self.dry_run, "action": kind, "torrents": names})
        return self.stats

    def _process(self, torrent, complete_names):
        name = torrent["name"]
        progress = torrent.get("percent_done") or 0
        if progress >= 1:
            if should_resume(torrent):
                self.stats["resumed"] += 1
                self.events["resumed"].append(name)
                logger.info("%s - %s", "Not Resuming" if self.dry_run else "Resuming", name)
                if not self.dry_run:
                    self.client.start([torrent["hash_string"]])
            else:
                logger.debug(
                    "Not resuming %s: ratio %.2f has reached the limit %.2f",
                    name,
                    tr.ratio(torrent) or 0.0,
                    ratio_limit(torrent),
                )
        elif progress == 0 and name in complete_names and torrent.get("status") not in (tr.STATUS_CHECK_WAIT, tr.STATUS_CHECK):
            self.stats["rechecked"] += 1
            self.events["rechecked"].append(name)
            logger.info("%s - %s", "Not Rechecking" if self.dry_run else "Rechecking", name)
            if not self.dry_run:
                self.client.verify([torrent["hash_string"]])


def run(context):
    return ReCheck(context).run()
