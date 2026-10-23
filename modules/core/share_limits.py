"""share_limits, ported from qbit_manage's modules/core/share_limits.py.

Behaviour follows upstream: every torrent is matched to the first share limit group (in priority order) whose
tag and size filters it passes, gets the group tag, and has the group's limits applied. The minimum seeding time,
minimum seeds and minimum inactive time protect a torrent with a tag and lift its limits; once a limit is reached
the torrent is removed (cleanup) or its upload speed throttled. Removals go through the pre_remove hook.

Transmission-specific:
- tags are labels and Transmission has no categories, so groups filter by label and size only;
- the ratio limit maps to seed_ratio_mode / seed_ratio_limit (-2 global = mode 0, -1 none = mode 2) and the
  inactive limit to seed_idle_mode / seed_idle_limit; Transmission stops a torrent that reaches them;
- Transmission has no seeding-time limit, so this module enforces max_seeding_time itself: a torrent that has
  seeded at least that long is stopped on the next run, as qBittorrent would stop it the moment it is reached;
- share_limit_action can only be Default or Stop, since stopping is all Transmission does at a limit;
- upload speeds are configured in KiB/s as upstream and converted to Transmission's speed unit.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from time import time

from modules import transmission as tr
from modules.core.remove_unregistered import WORKING
from modules.core.remove_unregistered import classify_trackers
from modules.core.remove_unregistered import shared_paths
from modules.core.tags import tracker_urls
from modules.util import path_replace
from modules.util import redact

logger = logging.getLogger("transmission_manage")

FIELDS = [
    "name",
    "labels",
    "status",
    "percent_done",
    "total_size",
    "upload_ratio",
    "seed_ratio_mode",
    "seed_ratio_limit",
    "seed_idle_mode",
    "seed_idle_limit",
    "upload_limit",
    "upload_limited",
    "seconds_seeding",
    "activity_date",
    "download_dir",
    "tracker_stats",
]
CLEANUP_FIELDS = ["files"]
SESSION_FIELDS = ["seed_ratio_limited", "seed_ratio_limit", "idle_seeding_limit_enabled", "units"]


def is_tag_in_torrent(check_tag, labels, exact=True):
    """Upstream is_tag_in_torrent on a label list: membership, or the labels containing check_tag."""
    labels = list(labels or [])
    if exact:
        return check_tag in labels
    return [label for label in labels if check_tag in label]


def ratio_limit(torrent):
    """The torrent's ratio limit in upstream terms: the value, -2 for the global limit or -1 for none."""
    mode = torrent.get("seed_ratio_mode")
    if mode == tr.LIMIT_SINGLE:
        return float(torrent.get("seed_ratio_limit") or 0)
    return -2 if mode == tr.LIMIT_GLOBAL else -1


def num_complete(torrent):
    """Seeds in the swarm as the trackers report them (upstream num_complete); 0 when no tracker knows."""
    counts = [tr.known_count(t.get("seeder_count")) for t in torrent.get("tracker_stats") or []]
    return max((count for count in counts if count is not None), default=0)


def content_path(torrent):
    return os.path.join(torrent.get("download_dir") or "", torrent.get("name") or "")


def get_share_limit_group(share_limits, torrent):
    """Name of the first group whose filters the torrent passes, or None."""
    labels = set(torrent.get("labels") or [])
    for group_name, group_config in share_limits.items():
        if check_tags(
            labels,
            group_config["include_all_tags"],
            group_config["include_any_tags"],
            group_config["exclude_all_tags"],
            group_config["exclude_any_tags"],
        ) and check_size(torrent, group_config["min_torrent_size"], group_config["max_torrent_size"]):
            return group_name
    return None


def check_tags(tags, include_all_tags=None, include_any_tags=None, exclude_all_tags=None, exclude_any_tags=None):
    tags = set(tags)
    if include_all_tags and not set(include_all_tags).issubset(tags):
        return False
    if include_any_tags and not set(include_any_tags) & tags:
        return False
    if exclude_all_tags and set(exclude_all_tags).issubset(tags):
        return False
    if exclude_any_tags and set(exclude_any_tags) & tags:
        return False
    return True


def check_size(torrent, min_size, max_size):
    if min_size is None and max_size is None:
        return True
    size = torrent.get("total_size")
    if size is None:
        return False
    if min_size is not None and size < min_size:
        return False
    if max_size is not None and size > max_size:
        return False
    return True


def seeding_time_limit(config, torrent):
    """The seeding-time limit (minutes) this port enforces for a torrent, or -1 when there is none.

    It is the group's max_seeding_time, unless an exclusion tag has lifted the limits or the group throttles
    instead of stopping (upstream clears the limits then). recheck uses it in place of qBittorrent's
    per-torrent seeding_time_limit.
    """
    settings = config.settings
    group = get_share_limit_group(config.share_limits, torrent) if config.share_limits else None
    if group is None:
        return -1
    group_config = config.share_limits[group]
    exclusion_tags = (
        settings["share_limits_min_seeding_time_tag"],
        settings["share_limits_min_num_seeds_tag"],
        settings["share_limits_last_active_tag"],
    )
    if any(tag in (torrent.get("labels") or []) for tag in exclusion_tags):
        return -1
    if not group_config["cleanup"] and group_config["upload_speed_on_limit_reached"] != 0:
        return -1
    return group_config["max_seeding_time"] if group_config["max_seeding_time"] >= 0 else -1


class ShareLimits:
    def __init__(self, context, now=time):
        self.ctx = context
        self.client = context.client
        self.config = context.config
        settings = context.config.settings
        self.dry_run = context.dry_run
        self.now = now
        self.stats_tagged = 0
        self.stats_deleted = 0
        self.stats_deleted_contents = 0
        self.stats_stopped = 0
        self.filter_completed = settings["share_limits_filter_completed"]
        self.root_dir = context.config.directory["root_dir"]
        self.remote_dir = context.config.directory["remote_dir"]
        self.share_limits_config = context.config.share_limits
        self.share_limits_tag = settings["share_limits_tag"]
        self.share_limits_custom_tags = context.config.share_limits_custom_tags
        self.min_seeding_time_tag = settings["share_limits_min_seeding_time_tag"]
        self.min_num_seeds_tag = settings["share_limits_min_num_seeds_tag"]
        self.last_active_tag = settings["share_limits_last_active_tag"]
        self.disable_default_share_limits = settings["disable_qbt_default_share_limits"]
        self.group_tag = None
        self.torrents_updated = []
        self.tdel_dict = {}
        self.torrent_hash_checked = []
        self.global_max_ratio_enabled = False
        self.global_max_ratio = -1
        # Transmission has no global seeding-time limit.
        self.global_max_seeding_time_enabled = False
        self.global_max_seeding_time = -1
        self.speed_bytes = 1000  # bytes per Transmission speed unit (kB/s by default)
        self.cross_seed = {}
        self.working_names = set()

    # --- Transmission calls ---------------------------------------------------------------------------

    def _set_labels(self, torrent, labels):
        if not self.dry_run:
            self.client.set_labels([torrent["hash_string"]], labels)
            torrent["labels"] = labels

    def add_tags(self, torrent, tag):
        self._set_labels(torrent, [*(torrent.get("labels") or []), tag])

    def remove_tags(self, torrent, tags):
        tags = [tags] if isinstance(tags, str) else list(tags)
        self._set_labels(torrent, [label for label in torrent.get("labels") or [] if label not in tags])

    def set_share_limits(self, torrent, ratio_limit, inactive_seeding_time_limit):
        """qBittorrent's setShareLimits in Transmission terms (-2 global, -1 unlimited, else the limit)."""
        fields = {}
        for value, mode_key, limit_key in (
            (ratio_limit, "seed_ratio_mode", "seed_ratio_limit"),
            (inactive_seeding_time_limit, "seed_idle_mode", "seed_idle_limit"),
        ):
            if value == -2:
                fields[mode_key] = tr.LIMIT_GLOBAL
            elif value < 0:
                fields[mode_key] = tr.LIMIT_UNLIMITED
            else:
                fields[mode_key] = tr.LIMIT_SINGLE
                fields[limit_key] = value
        self.client.set_torrent([torrent["hash_string"]], fields)
        torrent.update(fields)

    def upload_limit(self, torrent):
        """The torrent's upload limit in KiB/s, or -1 when unlimited (upstream treats 0 as unlimited too)."""
        if not torrent.get("upload_limited"):
            return -1
        kib = round((torrent.get("upload_limit") or 0) * self.speed_bytes / 1024)
        return -1 if kib == 0 else kib

    def set_upload_limit(self, torrent, kib):
        if kib == -1:
            fields = {"upload_limited": False}
        else:
            fields = {"upload_limited": True, "upload_limit": round(kib * 1024 / self.speed_bytes)}
        self.client.set_torrent([torrent["hash_string"]], fields)
        torrent.update(fields)

    def resume(self, torrent):
        if torrent.get("status") == tr.STATUS_STOPPED:
            self.client.start([torrent["hash_string"]])

    # --- run ------------------------------------------------------------------------------------------

    def run(self):
        start_time = time()
        logger.info("Updating Share Limits based on priority")
        if self.dry_run:
            logger.warning("Share Limits will not be applied with dry_run and could be inaccurate unless manually adding tags.")
        self.read_session()
        any_cleanup = any(group["cleanup"] for group in self.share_limits_config.values())
        torrents = self.client.torrents(FIELDS + (CLEANUP_FIELDS if any_cleanup else []))
        if any_cleanup:
            # Upstream keeps the contents of a cross-seeded torrent whose same-name twin is still working.
            self.cross_seed = shared_paths(torrents)
            self.working_names = {t["name"] for t in torrents if classify_trackers(t.get("tracker_stats"))[0] == WORKING}
        if self.filter_completed:
            torrents = [t for t in torrents if (t.get("percent_done") or 0) >= 1]
        groups = self.assign_torrents_to_group(torrents)
        for group_name, group_config in self.share_limits_config.items():
            self.torrents_updated = []
            self.tdel_dict = {}
            group_torrents = groups[group_name]
            logger.info(
                "Updating Share Limits for [Group %s] [Priority %s] [Torrents (%d)]",
                group_name,
                group_config["priority"],
                len(group_torrents),
            )
            if not group_torrents:
                continue
            self.update_share_limits_for_group(group_name, group_config, group_torrents)
            if self.torrents_updated:
                self.ctx.webhooks.function(
                    "share_limits",
                    {
                        "dry_run": self.dry_run,
                        "title": f"Updating Share Limits for {group_name}. Priority {group_config['priority']}",
                        "body": f"Updated {len(self.torrents_updated)} torrents.",
                        "grouping": group_name,
                        "torrents": self.torrents_updated,
                        "torrent_tag": self.group_tag,
                        "torrent_max_ratio": group_config["max_ratio"],
                        "torrent_max_seeding_time": group_config["max_seeding_time"],
                        "torrent_max_last_active": group_config["max_last_active"],
                        "torrent_min_seeding_time": group_config["min_seeding_time"],
                        "torrent_min_num_seeds": group_config["min_num_seeds"],
                        "torrent_limit_upload_speed": group_config["limit_upload_speed"],
                        "torrent_min_last_active": group_config["min_last_active"],
                    },
                )
            if group_config["cleanup"] and self.tdel_dict:
                self.cleanup_torrents_for_group(group_name, group_config["priority"])
        logger.debug("Share limits command completed in %.2f seconds", time() - start_time)
        return {
            "updated": self.stats_tagged,
            "stopped": self.stats_stopped,
            "deleted": self.stats_deleted,
            "deleted_contents": self.stats_deleted_contents,
        }

    def read_session(self):
        """Global limits, and upstream's disable_qbt_default_share_limits applied to Transmission's session."""
        session = self.client.session(SESSION_FIELDS)
        self.speed_bytes = (session.get("units") or {}).get("speed_bytes") or 1000
        if self.disable_default_share_limits and (session.get("seed_ratio_limited") or session.get("idle_seeding_limit_enabled")):
            logger.info("Disabling Transmission default share limits to allow transmission_manage to manage share limits.")
            if not self.dry_run:
                self.client.set_session({"seed_ratio_limited": False, "idle_seeding_limit_enabled": False})
                session["seed_ratio_limited"] = False
        self.global_max_ratio_enabled = bool(session.get("seed_ratio_limited"))
        self.global_max_ratio = session.get("seed_ratio_limit", -1)

    def assign_torrents_to_group(self, torrents):
        logger.info("Assigning torrents to share limit groups...")
        groups = {name: [] for name in self.share_limits_config}
        for torrent in torrents:
            grouping = get_share_limit_group(self.share_limits_config, torrent)
            logger.debug("Torrent: %s - Share Limit Group: %s", torrent["name"], grouping)
            if grouping:
                groups[grouping].append(torrent)
        return groups

    def update_share_limits_for_group(self, group_name, group_config, torrents):
        group_config = dict(group_config)
        group_upload_speed = group_config["limit_upload_speed"]
        if group_upload_speed <= 0:
            group_config["limit_upload_speed"] = -1
        elif group_config["enable_group_upload_speed"]:
            group_config["limit_upload_speed"] = round(group_upload_speed / len(torrents))
            logger.debug(
                "enable_group_upload_speed set to True. Setting limit_upload_speed to %s / %d = %s kB/s",
                group_upload_speed,
                len(torrents),
                group_config["limit_upload_speed"],
            )
        if group_config["add_group_to_tag"]:
            self.group_tag = group_config["custom_tag"] or f"{self.share_limits_tag}_{group_config['priority']}.{group_name}"
        else:
            self.group_tag = None

        for torrent in torrents:
            try:
                self._update_torrent(torrent, group_config)
            except tr.TransmissionError as err:
                logger.error("%s: %s", torrent.get("name"), err)

    def _update_torrent(self, torrent, group_config):
        t_name = torrent["name"]
        t_hash = torrent["hash_string"]
        urls = tracker_urls(torrent)
        tracker = redact(urls[0]) if urls else ""
        check_max_ratio = group_config["max_ratio"] != ratio_limit(torrent)
        # Transmission has no per-torrent seeding-time limit to compare: it is enforced by this module instead.
        check_max_seeding_time = False
        torrent_upload_limit = self.upload_limit(torrent)
        check_limit_upload_speed = group_config["limit_upload_speed"] != torrent_upload_limit
        hash_not_prev_checked = t_hash not in self.torrent_hash_checked
        labels = torrent.get("labels") or []

        if self.group_tag:
            if group_config["custom_tag"]:
                share_limits_not_yet_tagged = not is_tag_in_torrent(self.group_tag, labels)
            else:
                share_limits_not_yet_tagged = not is_tag_in_torrent(self.group_tag, labels, exact=False)
            check_multiple_share_limits_tag = any(
                custom_tag != self.group_tag and is_tag_in_torrent(custom_tag, labels)
                for custom_tag in self.share_limits_custom_tags
            )
            other_tags = len(is_tag_in_torrent(self.share_limits_tag, labels, exact=False))
            if (group_config["custom_tag"] and other_tags > 0) or (not group_config["custom_tag"] and other_tags > 1):
                check_multiple_share_limits_tag = True
        else:
            share_limits_not_yet_tagged = False
            check_multiple_share_limits_tag = False

        logger.debug(
            "Evaluating share limits for torrent '%s': labels %s | max ratio %s vs %s | seeding %s min | "
            "upload %s vs %s | not yet tagged %s | multiple tags %s",
            t_name,
            labels,
            group_config["max_ratio"],
            ratio_limit(torrent),
            round((torrent.get("seconds_seeding") or 0) / 60),
            group_config["limit_upload_speed"],
            torrent_upload_limit,
            share_limits_not_yet_tagged,
            check_multiple_share_limits_tag,
        )

        tor_reached_seed_limit, exclusion_tag_added = self.has_reached_seed_limit(
            torrent=torrent,
            max_ratio=group_config["max_ratio"],
            max_seeding_time=group_config["max_seeding_time"],
            max_last_active=group_config["max_last_active"],
            min_seeding_time=group_config["min_seeding_time"],
            min_num_seeds=group_config["min_num_seeds"],
            min_last_active=group_config["min_last_active"],
            resume_torrent=group_config["resume_torrent_after_change"],
            tracker=tracker,
            reset_upload_speed_on_unmet_minimums=group_config["reset_upload_speed_on_unmet_minimums"],
            limit_upload_speed=group_config["limit_upload_speed"],
        )
        self.update_share_limits_tag_for_torrent(torrent)

        if self._should_update_torrent(
            check_max_ratio,
            check_max_seeding_time,
            check_limit_upload_speed,
            share_limits_not_yet_tagged,
            check_multiple_share_limits_tag,
            tor_reached_seed_limit,
            group_config,
            hash_not_prev_checked,
            exclusion_tag_added,
            torrent,
        ):
            logger.info("Torrent Name: %s", t_name)
            logger.info("  Tracker: %s", tracker)
            if self.group_tag:
                logger.info("  Added Tag: %s", self.group_tag)
            self.process_share_limits_for_torrent(torrent, group_config, tor_reached_seed_limit, torrent_upload_limit)
            self.stats_tagged += 1
            self.torrents_updated.append(t_name)

        self.enforce_seeding_time_limit(torrent, group_config)
        self.torrent_hash_checked.append(t_hash)

    def _should_update_torrent(
        self,
        check_max_ratio,
        check_max_seeding_time,
        check_limit_upload_speed,
        share_limits_not_yet_tagged,
        check_multiple_share_limits_tag,
        tor_reached_seed_limit,
        group_config,
        hash_not_prev_checked,
        exclusion_tag_added,
        torrent,
    ):
        if not hash_not_prev_checked:
            return False
        # The limits were cleared when the exclusion tag was added; do not reinstate them in the same run.
        if exclusion_tag_added:
            return False
        reached_and_acting = tor_reached_seed_limit and (
            group_config["cleanup"] or group_config["upload_speed_on_limit_reached"] != 0
        )
        needs_update = (
            check_max_ratio
            or check_max_seeding_time
            or check_limit_upload_speed
            or share_limits_not_yet_tagged
            or check_multiple_share_limits_tag
            or reached_and_acting
        )
        if not needs_update:
            return False
        labels = torrent.get("labels") or []
        no_exclusion_tags = (
            not is_tag_in_torrent(self.min_seeding_time_tag, labels)
            and not is_tag_in_torrent(self.min_num_seeds_tag, labels)
            and not is_tag_in_torrent(self.last_active_tag, labels)
        )
        return no_exclusion_tags or share_limits_not_yet_tagged or check_multiple_share_limits_tag or reached_and_acting

    def update_share_limits_tag_for_torrent(self, torrent):
        """Replace any previous share limit labels with this group's label. Returns True if labels changed."""
        if self.dry_run:
            return False
        labels = list(torrent.get("labels") or [])
        new_labels = [
            label for label in labels if self.share_limits_tag not in label and label not in self.share_limits_custom_tags
        ]
        if self.group_tag:
            new_labels.append(self.group_tag)
        if new_labels == labels:
            return False
        self._set_labels(torrent, new_labels)
        return True

    def process_share_limits_for_torrent(self, torrent, group_config, tor_reached_seed_limit, torrent_upload_limit):
        if tor_reached_seed_limit:
            if group_config["cleanup"]:
                self.tdel_dict[torrent["hash_string"]] = {
                    "torrent": torrent,
                    "content_path": path_replace(content_path(torrent), self.root_dir, self.remote_dir),
                    "body": tor_reached_seed_limit,
                }
            else:
                throttle_kib = group_config.get("upload_speed_on_limit_reached", 0)
                if throttle_kib == 0:
                    logger.debug("Skipping throttle for %s: upload_speed_on_limit_reached not configured", torrent["name"])
                    self.set_limits(
                        torrent=torrent,
                        max_ratio=group_config["max_ratio"],
                        max_seeding_time=group_config["max_seeding_time"],
                        limit_upload_speed=group_config["limit_upload_speed"],
                    )
                    return
                if throttle_kib < -1:
                    logger.warning(
                        "Invalid upload_speed_on_limit_reached value: %s. Must be >= -1. Skipping throttle for %s",
                        throttle_kib,
                        torrent["name"],
                    )
                    return
                if throttle_kib != torrent_upload_limit:
                    logger.info("  Cleanup: False [Meets Share Limits]")
                    logger.info(
                        "  Applied upload throttle after limits reached: %s",
                        "unlimited" if throttle_kib == -1 else f"{throttle_kib} kB/s",
                    )
                    if not self.dry_run:
                        # Lift the limits so the torrent keeps seeding at the throttled speed.
                        self.set_share_limits(torrent, ratio_limit=-1, inactive_seeding_time_limit=-1)
                        self.set_upload_limit(torrent, throttle_kib)
        else:
            self.set_limits(
                torrent=torrent,
                max_ratio=group_config["max_ratio"],
                max_seeding_time=group_config["max_seeding_time"],
                limit_upload_speed=group_config["limit_upload_speed"],
            )

        if (torrent.get("percent_done") or 0) >= 1 and group_config["resume_torrent_after_change"] and not self.dry_run:
            self.resume(torrent)

    def set_limits(self, torrent, max_ratio, max_seeding_time, limit_upload_speed=None, do_print=True):
        body = []
        if limit_upload_speed is not None and limit_upload_speed != -1:
            body.append(f"  Limit UL Speed: {limit_upload_speed} kB/s")
        if max_ratio == -2 and max_seeding_time == -2:
            body.append("  Share Limit: Use Global Share Limit")
        elif max_ratio == -1 and max_seeding_time == -1:
            body.append("  Share Limit: Set No Share Limit")
        elif max_ratio != ratio_limit(torrent) and max_seeding_time < 0:
            body.append(f"  Share Limit: Max Ratio = {max_ratio}")
        elif max_seeding_time >= 0 and max_ratio < 0:
            body.append(f"  Share Limit: Max Seed Time = {timedelta(minutes=max_seeding_time)}")
        elif max_seeding_time >= 0:
            body.append(f"  Share Limit: Max Ratio = {max_ratio}, Max Seed Time = {timedelta(minutes=max_seeding_time)}")
        if not self.dry_run:
            labels = torrent.get("labels") or []
            # Exclusion tags keep the limits lifted; their upload speed is handled in has_reached_seed_limit.
            if any(
                is_tag_in_torrent(tag, labels)
                for tag in (self.min_seeding_time_tag, self.min_num_seeds_tag, self.last_active_tag)
            ):
                return []
            if limit_upload_speed is not None and limit_upload_speed != self.upload_limit(torrent):
                self.set_upload_limit(torrent, limit_upload_speed)
            # max_seeding_time is not sent: Transmission has none, enforce_seeding_time_limit applies it.
            self.set_share_limits(torrent, ratio_limit=max_ratio, inactive_seeding_time_limit=-2)
        if do_print:
            for line in body:
                logger.info(line)
        return body

    def enforce_seeding_time_limit(self, torrent, group_config):
        """Stop a torrent that reached the group's seeding-time limit, which Transmission cannot do itself."""
        limit = seeding_time_limit(self.config, torrent)
        if limit < 0 or torrent.get("status") == tr.STATUS_STOPPED or (torrent.get("percent_done") or 0) < 1:
            return
        seeding = torrent.get("seconds_seeding") or 0
        if seeding < limit * 60:
            return
        self.stats_stopped += 1
        logger.info(
            "%s %s: Seeding Time vs Max Seed Time: %s >= %s",
            "Would stop" if self.dry_run else "Stopping",
            torrent["name"],
            timedelta(seconds=seeding),
            timedelta(minutes=limit),
        )
        if not self.dry_run:
            self.client.stop([torrent["hash_string"]])
            torrent["status"] = tr.STATUS_STOPPED

    def has_reached_seed_limit(
        self,
        torrent,
        max_ratio,
        max_seeding_time,
        max_last_active,
        min_seeding_time,
        min_num_seeds,
        min_last_active,
        resume_torrent,
        tracker,
        reset_upload_speed_on_unmet_minimums,
        limit_upload_speed=None,
    ):
        """Upstream's check. Returns (body, exclusion_tag_added); body is False when no limit is reached."""
        body = ""
        exclusion_tag_added = False
        seeding_time = torrent.get("seconds_seeding") or 0
        ratio = tr.ratio(torrent)
        ratio = 0.0 if ratio is None else ratio

        def _remove_tag(tag):
            if is_tag_in_torrent(tag, torrent.get("labels")) and not self.dry_run:
                self.remove_tags(torrent, tag)

        def _add_tag_and_reset_limits(tag, reason_msg):
            nonlocal exclusion_tag_added
            if not is_tag_in_torrent(tag, torrent.get("labels")):
                exclusion_tag_added = True
                logger.info("Torrent Name: %s", torrent["name"])
                logger.info("  Tracker: %s", tracker)
                logger.info("  %s", reason_msg)
                logger.info("  Adding Tag: %s", tag)
                if not self.dry_run:
                    self.add_tags(torrent, tag)
                    self.set_share_limits(torrent, ratio_limit=-1, inactive_seeding_time_limit=-1)
                    if reset_upload_speed_on_unmet_minimums:
                        self.set_upload_limit(torrent, -1)
                    if resume_torrent:
                        self.resume(torrent)
            if not reset_upload_speed_on_unmet_minimums and limit_upload_speed is not None and limit_upload_speed > 0:
                if self.upload_limit(torrent) != limit_upload_speed:
                    if not exclusion_tag_added:
                        logger.info("Torrent Name: %s", torrent["name"])
                        logger.info("  Tracker: %s", tracker)
                    logger.info("  Limit UL Speed: %s kB/s", limit_upload_speed)
                    if not self.dry_run:
                        self.set_upload_limit(torrent, limit_upload_speed)

        def _inactive_minutes():
            return round((int(self.now()) - (torrent.get("activity_date") or 0)) / 60)

        def _has_reached_min_seeding_time_limit():
            if seeding_time >= min_seeding_time * 60:
                _remove_tag(self.min_seeding_time_tag)
                return True
            _add_tag_and_reset_limits(
                self.min_seeding_time_tag,
                f"Min seed time not met: {timedelta(seconds=seeding_time)} <= {timedelta(minutes=min_seeding_time)}. "
                "Removing Share Limits so Transmission can continue seeding.",
            )
            return False

        def _is_less_than_min_num_seeds():
            seeds = num_complete(torrent)
            if min_num_seeds == 0 or seeds >= min_num_seeds:
                _remove_tag(self.min_num_seeds_tag)
                return False
            _add_tag_and_reset_limits(
                self.min_num_seeds_tag,
                f"Min number of seeds not met: Total Seeds ({seeds}) < min_num_seeds({min_num_seeds}). "
                "Removing Share Limits so Transmission can continue seeding.",
            )
            return True

        def _has_reached_min_last_active_time_limit():
            inactive = _inactive_minutes()
            if inactive >= min_last_active:
                _remove_tag(self.last_active_tag)
                return True
            _add_tag_and_reset_limits(
                self.last_active_tag,
                f"Min inactive time not met: {timedelta(minutes=inactive)} <= {timedelta(minutes=min_last_active)}. "
                "Removing Share Limits so Transmission can continue seeding.",
            )
            return False

        def _has_reached_seeding_time_limit():
            nonlocal body
            if max_seeding_time is None or max_seeding_time == -1:
                return False
            if max_seeding_time >= 0:
                limit = max_seeding_time
            elif max_seeding_time == -2 and self.global_max_seeding_time_enabled:
                limit = self.global_max_seeding_time
            else:
                _remove_tag(self.min_seeding_time_tag)
                return False
            if seeding_time >= limit * 60 and _has_reached_min_seeding_time_limit():
                body += f"  Seeding Time vs Max Seed Time: {timedelta(seconds=seeding_time)} >= {timedelta(minutes=limit)}"
                return True
            return False

        def _has_reached_max_last_active_time_limit():
            nonlocal body
            inactive = _inactive_minutes()
            if max_last_active is not None and max_last_active != -1:
                if (
                    inactive >= max_last_active
                    and _has_reached_min_last_active_time_limit()
                    and _has_reached_min_seeding_time_limit()
                ):
                    body += (
                        f"  Inactive Time vs Max Last Active Time: {timedelta(minutes=inactive)} >= "
                        f"{timedelta(minutes=max_last_active)}"
                    )
                    return True
            return False

        if min_num_seeds is not None and _is_less_than_min_num_seeds():
            return body, exclusion_tag_added
        if min_last_active is not None and not _has_reached_min_last_active_time_limit():
            return body, exclusion_tag_added
        if max_ratio is not None and max_ratio != -1:
            if max_ratio >= 0:
                if ratio >= max_ratio and _has_reached_min_seeding_time_limit():
                    body += f"  Ratio vs Max Ratio: {ratio:.2f} >= {max_ratio:.2f}"
                    return body, exclusion_tag_added
            elif max_ratio == -2 and self.global_max_ratio_enabled and _has_reached_min_seeding_time_limit():
                if ratio >= self.global_max_ratio:
                    body += f"  Ratio vs Global Max Ratio: {ratio:.2f} >= {self.global_max_ratio:.2f}"
                    return body, exclusion_tag_added
        if _has_reached_seeding_time_limit():
            return body, exclusion_tag_added
        if _has_reached_max_last_active_time_limit():
            return body, exclusion_tag_added
        return False, exclusion_tag_added

    def cleanup_torrents_for_group(self, group_name, priority):
        """Remove the torrents that reached their limits, keeping shared contents as upstream does."""
        logger.info("Cleaning up torrents that have reached ratio/seed limit for %s. Priority %s", group_name, priority)
        removed = []
        current = {t["hash_string"]: t for t in self.client.torrents(["name", "download_dir"], list(self.tdel_dict))}
        for t_hash, item in self.tdel_dict.items():
            torrent = item["torrent"]
            t_name = torrent["name"]
            # Double check that the content path is the same before we delete anything.
            now = current.get(t_hash)
            if now is None or path_replace(content_path(now), self.root_dir, self.remote_dir) != item["content_path"]:
                logger.info("Skipping %s: it moved or is gone since the limits were checked", t_name)
                continue
            urls = tracker_urls(torrent)
            tracker = redact(urls[0]) if urls else ""
            if os.path.exists(item["content_path"]):
                delete_contents = not (self.cross_seed.get(t_hash) and t_name in self.working_names)
                reason = "" if delete_contents else "Reason: is cross-seed"
            else:
                delete_contents = False
                reason = f"Reason: path does not exist [path={item['content_path']}]."
            payload = {
                "command": "share_limits",
                "hash": t_hash,
                "name": t_name,
                "tracker": tracker,
                "grouping": group_name,
                "reason": item["body"].strip(),
                "delete_data": delete_contents,
                "dry_run": self.dry_run,
            }
            allowed, delete_contents = self.ctx.hooks.pre_remove(payload)
            if not allowed:
                continue
            if not delete_contents and not reason:
                reason = "Reason: kept by the pre_remove hook"
            if not self.dry_run:
                self.client.remove([t_hash], delete_data=delete_contents)
            if delete_contents:
                self.stats_deleted_contents += 1
            else:
                self.stats_deleted += 1
            removed.append({"name": t_name, "tracker": tracker, "torrents_deleted_and_contents": delete_contents})
            logger.info("Torrent Name: %s", t_name)
            logger.info("  Tracker: %s", tracker)
            logger.info("%s", item["body"])
            logger.info("  Cleanup: True [Meets Share Limits]")
            logger.info(
                "  %s .torrent %s %s",
                "Would delete" if self.dry_run else "Deleted",
                "AND content files." if delete_contents else "but NOT content files.",
                reason,
            )
        if removed:
            self.ctx.webhooks.function(
                "cleanup_share_limits",
                {"dry_run": self.dry_run, "title": "Share limit removal", "grouping": group_name, "torrents": removed},
            )


def run(context):
    return ShareLimits(context).run()
