"""rem_unregistered and tag_tracker_error, ported from qbit_manage's modules/core/remove_unregistered.py.

Behaviour follows upstream; the Transmission-specific part is how tracker state is read (see classify_trackers).
The tracker-specific code path upstream keeps for a single site is not ported. Before each removal the optional
pre_remove hook is consulted.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict

from modules import transmission as tr
from modules.util import TorrentMessages
from modules.util import list_in_text

logger = logging.getLogger("transmission_manage")

FIELDS = ["name", "status", "added_date", "percent_done", "labels", "tracker_stats"]
FILE_FIELDS = ["download_dir", "files"]
REAL_SCHEMES = ("http", "https", "udp", "ws", "wss")

WORKING = "working"
INCONCLUSIVE = "inconclusive"
FAILING = "failing"
NO_TRACKERS = "no_trackers"


def _is_real(tracker):
    scheme = (tracker.get("announce") or "").split(":", 1)[0].lower()
    return scheme in REAL_SCHEMES and not tracker.get("is_backup")


def classify_trackers(tracker_stats):
    """Map Transmission tracker_stats onto qbit_manage's tracker states.

    Returns (state, failing_trackers):
    - WORKING      any real tracker's last announce succeeded (qbit WORKING);
    - INCONCLUSIVE a real tracker has not announced this session, or is queued / announcing right now
                   (qbit NOT_CONTACTED / UPDATING);
    - FAILING      every real tracker reported a failure (qbit NOT_WORKING / TRACKER_ERROR / UNREACHABLE);
                   failing_trackers leaves out those whose message matches EXCEPTIONS_MSGS, as upstream does when
                   it builds its list of torrents with issues;
    - NO_TRACKERS  no real tracker.
    Backup trackers and non-announce URLs are ignored, as upstream ignores pseudo trackers.
    """
    real = [tracker for tracker in tracker_stats or [] if _is_real(tracker)]
    if not real:
        return NO_TRACKERS, []
    if any(tracker.get("last_announce_succeeded") for tracker in real):
        return WORKING, []
    if any(
        not tracker.get("has_announced") or tracker.get("announce_state") in (tr.TRACKER_QUEUED, tr.TRACKER_ACTIVE)
        for tracker in real
    ):
        return INCONCLUSIVE, []
    failing = [
        tracker
        for tracker in real
        if not list_in_text((tracker.get("last_announce_result") or "").upper(), TorrentMessages.EXCEPTIONS_MSGS)
    ]
    return FAILING, failing


def find_unregistered(failing_trackers):
    """First failing tracker whose message says the torrent is gone (every failing tracker is scanned, as upstream)."""
    for tracker in failing_trackers:
        message = (tracker.get("last_announce_result") or "").upper()
        if list_in_text(message, TorrentMessages.UNREGISTERED_MSGS) and not list_in_text(message, TorrentMessages.IGNORE_MSGS):
            return tracker
    return None


def shared_paths(torrents):
    """hash -> True when another torrent uses at least one of the same file paths (upstream has_cross_seed)."""
    owners = defaultdict(set)
    for torrent in torrents:
        for item in torrent.get("files") or []:
            path = os.path.normpath(os.path.join(torrent.get("download_dir") or "", item.get("name") or ""))
            owners[path].add(torrent["hash_string"])
    shared = set()
    for hashes in owners.values():
        if len(hashes) > 1:
            shared |= hashes
    return {torrent["hash_string"]: torrent["hash_string"] in shared for torrent in torrents}


def _tracker_key(tracker):
    return tracker.get("host") or tracker.get("sitename") or "unknown"


class RemoveUnregistered:
    def __init__(self, context, now=time.time):
        self.ctx = context
        self.client = context.client
        settings = context.config.settings
        commands = context.config.commands
        self.cfg_rem_unregistered = commands["rem_unregistered"]
        self.cfg_tag_error = commands["tag_tracker_error"]
        self.dry_run = context.dry_run
        self.tag_error = settings["tracker_error_tag"]
        self.filter_completed = settings["rem_unregistered_filter_completed"]
        self.grace_minutes = settings["rem_unregistered_grace_minutes"]
        self.max_per_tracker = settings["rem_unregistered_max_torrents"]
        self.ignore_list = settings["rem_unregistered_ignore_list"]
        self.now = now
        self.tracker_del_count = {}
        self.stats = {"deleted": 0, "deleted_contents": 0, "tagged": 0, "untagged": 0}
        self.events = {"rem_unregistered": [], "tag_tracker_error": [], "untag_tracker_error": []}

    def _set_labels(self, torrent, labels):
        if not self.dry_run:
            self.client.set_labels([torrent["hash_string"]], labels)
        torrent["labels"] = labels

    def run(self):
        fields = FIELDS + (FILE_FIELDS if self.cfg_rem_unregistered else [])
        torrents = self.client.torrents(fields)
        states = {t["hash_string"]: classify_trackers(t.get("tracker_stats")) for t in torrents}
        shared = shared_paths(torrents) if self.cfg_rem_unregistered else {}
        # Upstream keeps a cross-seeded torrent's data when a torrent with the same name still has a working tracker.
        working_names = {t["name"] for t in torrents if states[t["hash_string"]][0] == WORKING}

        self.remove_previous_errors(torrents, states)
        for torrent in torrents:
            state, failing = states[torrent["hash_string"]]
            if state != FAILING or not failing:
                continue
            try:
                self.process_torrent_issue(torrent, failing, shared.get(torrent["hash_string"], False), working_names)
            except tr.TransmissionError as err:
                logger.error("Remove Unregistered Torrents Error: %s: %s", torrent.get("name"), err)
        for function, items in self.events.items():
            if items:
                self.ctx.webhooks.function(function, {"dry_run": self.dry_run, "torrents": items})
        self._summary()
        return self.stats

    def remove_previous_errors(self, torrents, states):
        """Remove the tracker error label from torrents that have a working tracker again."""
        for torrent in torrents:
            labels = list(torrent.get("labels") or [])
            if states[torrent["hash_string"]][0] == WORKING and self.tag_error in labels:
                self._set_labels(torrent, [label for label in labels if label != self.tag_error])
                self.stats["untagged"] += 1
                self.events["untag_tracker_error"].append({"name": torrent["name"], "tag": self.tag_error})
                logger.info("Previous tagged %s torrent currently has a working tracker: %s", self.tag_error, torrent["name"])

    def process_torrent_issue(self, torrent, failing, has_cross_seed, working_names):
        if self.filter_completed and (torrent.get("percent_done") or 0) < 1:
            return
        unregistered = find_unregistered(failing)
        if self.cfg_rem_unregistered and unregistered is not None:
            message = unregistered.get("last_announce_result") or ""
            if list_in_text(message.upper(), self.ignore_list):
                logger.info("Ignoring unregistered torrent %s due to matching phrase found in ignore list.", torrent["name"])
            else:
                age = self.age_minutes(torrent)
                if self.grace_minutes > 0 and age is not None and age < self.grace_minutes:
                    logger.info(
                        "Skipping removal (within grace %d min, age %.1f min): %s", self.grace_minutes, age, torrent["name"]
                    )
                else:
                    self.check_max_limit_and_delete(torrent, unregistered, has_cross_seed, working_names)

        if self.cfg_tag_error and self.tag_error not in (torrent.get("labels") or []):
            tracker = unregistered if unregistered is not None else failing[0]
            self.tag_tracker_error(torrent, tracker)

    def age_minutes(self, torrent):
        added = torrent.get("added_date")
        if not added:
            return None
        return max(0.0, self.now() - float(added)) / 60

    def check_max_limit_and_delete(self, torrent, tracker, has_cross_seed, working_names):
        host = _tracker_key(tracker)
        if self.max_per_tracker > 0 and self.tracker_del_count.get(host, 0) >= self.max_per_tracker:
            logger.info(
                "Skipping removal (max limit of %d reached for tracker %s): %s", self.max_per_tracker, host, torrent["name"]
            )
            return
        delete_contents = not (has_cross_seed and torrent["name"] in working_names)
        payload = {
            "command": "rem_unregistered",
            "hash": torrent["hash_string"],
            "name": torrent["name"],
            "tracker": host,
            "status": tracker.get("last_announce_result"),
            "delete_data": delete_contents,
            "dry_run": self.dry_run,
        }
        if not self.ctx.hooks.pre_remove(payload):
            return
        if self.max_per_tracker > 0:
            self.tracker_del_count[host] = self.tracker_del_count.get(host, 0) + 1
        self.del_unregistered(torrent, tracker, delete_contents)

    def del_unregistered(self, torrent, tracker, delete_contents):
        if not self.dry_run:
            self.client.remove([torrent["hash_string"]], delete_data=delete_contents)
        self.stats["deleted_contents" if delete_contents else "deleted"] += 1
        self.events["rem_unregistered"].append(
            {
                "name": torrent["name"],
                "tracker": _tracker_key(tracker),
                "status": tracker.get("last_announce_result"),
                "torrents_deleted_and_contents": delete_contents,
            }
        )
        logger.info(
            "%s .torrent %s: %s [%s: %s]",
            "Would delete" if self.dry_run else "Deleted",
            "AND content files" if delete_contents else "but NOT content files",
            torrent["name"],
            _tracker_key(tracker),
            tracker.get("last_announce_result"),
        )

    def tag_tracker_error(self, torrent, tracker):
        self._set_labels(torrent, [*(torrent.get("labels") or []), self.tag_error])
        self.stats["tagged"] += 1
        self.events["tag_tracker_error"].append(
            {"name": torrent["name"], "tracker": _tracker_key(tracker), "status": tracker.get("last_announce_result")}
        )
        logger.info(
            "%s %s: %s [%s: %s]",
            "Not adding tag (dry run)" if self.dry_run else "Added tag",
            self.tag_error,
            torrent["name"],
            _tracker_key(tracker),
            tracker.get("last_announce_result"),
        )

    def _summary(self):
        s = self.stats
        verb = "Did not delete" if self.dry_run else "Deleted"
        if self.cfg_rem_unregistered:
            if s["deleted"] or s["deleted_contents"]:
                if s["deleted"]:
                    logger.info("%s %d .torrent(s) but not content files.", verb, s["deleted"])
                if s["deleted_contents"]:
                    logger.info("%s %d .torrent(s) AND content files.", verb, s["deleted_contents"])
            else:
                logger.info("No unregistered torrents found.")
        if s["untagged"]:
            logger.info("%s %s tags for %d .torrent(s).", verb, self.tag_error, s["untagged"])
        if s["tagged"]:
            logger.info("%d torrents with tracker errors found", s["tagged"])


def run(context):
    return RemoveUnregistered(context).run()
