"""Configuration: load config.yml, validate value types and merge CLI commands.

Section and key names follow qbit_manage wherever Transmission allows it. As upstream, keys this port does not
use are ignored; they are logged as warnings so a typo is visible. A value of the wrong type stops the run.
"""

from __future__ import annotations

import logging
import os
import re

from pytimeparse2 import parse as parse_time

from modules.util import Failed
from modules.util import load_yaml

logger = logging.getLogger("transmission_manage")

COMMANDS = (
    "dry_run",
    "recheck",
    "tag_update",
    "rem_unregistered",
    "tag_tracker_error",
    "rem_orphaned",
    "tag_nohardlinks",
    "share_limits",
    "skip_cleanup",
)

# key -> (accepted types, default)
TRANSMISSION_SCHEMA = {
    "host": ((str,), "localhost:9091"),
    "rpc_path": ((str,), "/transmission/rpc"),
    "user": ((str, type(None)), None),
    "pass": ((str, type(None)), None),
    "ssl": ((bool,), False),
    "protocol": ((str,), "auto"),
    "timeout": ((int,), 30),
    "retries": ((int,), 2),
}

SETTINGS_SCHEMA = {
    "tracker_error_tag": ((str,), "issue"),
    "nohardlinks_tag": ((str,), "noHL"),
    "stalled_tag": ((str, type(None)), "stalledDL"),
    "private_tag": ((str, type(None)), None),
    "tag_stalled_torrents": ((bool,), True),
    "share_limits_tag": ((str,), "~share_limit"),
    "share_limits_min_seeding_time_tag": ((str,), "MinSeedTimeNotReached"),
    "share_limits_min_num_seeds_tag": ((str,), "MinSeedsNotMet"),
    "share_limits_last_active_tag": ((str,), "LastActiveLimitNotReached"),
    "share_limits_filter_completed": ((bool,), True),
    "disable_qbt_default_share_limits": ((bool,), True),
    "tag_nohardlinks_filter_completed": ((bool,), True),
    "rem_unregistered_filter_completed": ((bool,), False),
    "rem_unregistered_ignore_list": ((list, type(None)), None),
    "rem_unregistered_grace_minutes": ((int,), 10),
    "rem_unregistered_max_torrents": ((int,), 10),
}

DIRECTORY_SCHEMA = {
    "root_dir": ((str, type(None)), None),
    "remote_dir": ((str, type(None)), None),
    "torrents_dir": ((str, type(None)), None),
    "orphaned_dir": ((str, type(None)), None),
}

HOOKS_SCHEMA = {
    "pre_remove": ((str, type(None)), None),
    "pre_remove_batch": ((str, type(None)), None),
    "classify_unregistered": ((str, type(None)), None),
    "timeout": ((int,), 60),
}

WEBHOOK_KEYS = ("error", "run_start", "run_end", "function")

ORPHANED_SCHEMA = {
    "empty_after_x_days": ((int, type(None)), None),
    "min_file_age_minutes": ((int,), 0),
    "exclude_patterns": ((list, type(None)), None),
    "max_orphaned_files_to_delete": ((int,), 50),
}

# share_limit_action values: qBittorrent's ShareLimitAction enum. Transmission can only stop a torrent when a
# limit is reached, so the other actions have no equivalent; cleanup: true covers removal.
SHARE_LIMIT_ACTIONS = ("Default", "Stop")
UNSUPPORTED_SHARE_LIMIT_ACTIONS = ("Remove", "RemoveWithContent", "EnableSuperSeeding")

# Group keys as upstream; 'categories' is rejected separately because Transmission has none.
SHARE_LIMIT_KEYS = {
    "priority",
    "include_all_tags",
    "include_any_tags",
    "exclude_all_tags",
    "exclude_any_tags",
    "categories",
    "min_torrent_size",
    "max_torrent_size",
    "cleanup",
    "max_ratio",
    "max_seeding_time",
    "max_last_active",
    "min_seeding_time",
    "limit_upload_speed",
    "upload_speed_on_limit_reached",
    "enable_group_upload_speed",
    "min_num_seeds",
    "share_limit_action",
    "last_active",
    "min_last_active",
    "resume_torrent_after_change",
    "add_group_to_tag",
    "custom_tag",
    "reset_upload_speed_on_unmet_minimums",
}

QBIT_ONLY_SECTIONS = {
    "qbt": "use the 'transmission' section",
    "cat": "Transmission has no categories",
    "cat_change": "Transmission has no categories",
    "recyclebin": "not ported",
    "apprise": "not ported; use plain webhook URLs",
    "notifiarr": "not ported; use plain webhook URLs",
}


def _ignored(where, keys):
    for key in sorted(keys):
        logger.warning("Config: '%s%s' is not used by this port and is ignored", where, key)


def _check_section(name, data, schema):
    data = data or {}
    if not isinstance(data, dict):
        raise Failed(f"Config error: '{name}' must be a mapping")
    _ignored(f"{name}.", set(data) - set(schema))
    result = {}
    for key, (types, default) in schema.items():
        value = data.get(key, default)
        if isinstance(value, bool) and bool not in types:
            raise Failed(f"Config error: '{name}.{key}' must not be a boolean")
        if not isinstance(value, types):
            expected = " or ".join("null" if t is type(None) else t.__name__ for t in types)
            raise Failed(f"Config error: '{name}.{key}' must be {expected}, not {type(value).__name__}")
        result[key] = value
    return result


def _check_tracker(data):
    data = data or {}
    if not isinstance(data, dict):
        raise Failed("Config error: 'tracker' must be a mapping of URL keyword -> options")
    result = {}
    for keyword, options in data.items():
        if not isinstance(options, dict) or "tag" not in options:
            raise Failed(f"Config error: tracker '{keyword}' needs a 'tag'")
        _ignored(f"tracker.{keyword}.", set(options) - {"tag"})
        tags = options["tag"]
        tags = [tags] if isinstance(tags, str) else tags
        if not isinstance(tags, list) or not tags or not all(isinstance(tag, str) and tag for tag in tags):
            raise Failed(f"Config error: tracker '{keyword}' tag must be a string or a non-empty list of strings")
        # qbit_manage allows several URL keywords per entry, separated by '|'.
        for part in str(keyword).split("|"):
            if part.strip():
                result[part.strip()] = tags  # matched case-sensitively, as upstream
    return result


def _check_nohardlinks(data):
    """nohardlinks: <label>: {exclude_tags, ignore_root_dir} plus optional global_options (labels replace categories)."""
    if data is None:
        return {}
    if isinstance(data, list):  # qbit_manage also accepts a plain list of names
        data = {name: None for name in data}
    if not isinstance(data, dict):
        raise Failed("Config error: 'nohardlinks' must be a mapping of label -> options")
    result = {}
    for label, options in data.items():
        options = options or {}
        if not isinstance(options, dict):
            raise Failed(f"Config error: nohardlinks '{label}' options must be a mapping")
        # ignore_category_dir needs category save paths, which Transmission does not have.
        _ignored(f"nohardlinks.{label}.", set(options) - {"exclude_tags", "ignore_root_dir"})
        exclude = options.get("exclude_tags") or []
        if not isinstance(exclude, list) or not all(isinstance(tag, str) for tag in exclude):
            raise Failed(f"Config error: nohardlinks '{label}' exclude_tags must be a list of labels")
        if "ignore_root_dir" in options and not isinstance(options["ignore_root_dir"], bool):
            raise Failed(f"Config error: nohardlinks '{label}' ignore_root_dir must be true or false")
        result[str(label)] = {key: options[key] for key in ("exclude_tags", "ignore_root_dir") if key in options}
    return result


def validate_share_limit_action(raw, cleanup, group):
    """Upstream's validation of share_limit_action, limited to the actions Transmission can perform.

    Upstream also rejects cleanup combined with a removing action; those actions are rejected outright here.
    """
    if raw is not None and raw != "" and (not isinstance(raw, str) or raw not in SHARE_LIMIT_ACTIONS):
        if raw in UNSUPPORTED_SHARE_LIMIT_ACTIONS:
            raise Failed(
                f"Config Error: share_limit_action '{raw}' for the grouping '{group}' is not supported by "
                "Transmission, which can only stop a torrent; use cleanup: true to remove torrents."
            )
        raise Failed(
            f"Config Error: invalid share_limit_action '{raw}' for the grouping '{group}'. "
            f"Valid values are: {', '.join(SHARE_LIMIT_ACTIONS)}."
        )
    return raw or "Default"


def _time_minutes(value, name, min_int):
    """Upstream time_parse: an int of at least min_int is minutes; a string such as '2h30m' is parsed."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= min_int:
        return value
    try:
        seconds = parse_time(value) if isinstance(value, str) else None
    except Exception:  # pytimeparse2 raises on some malformed input
        seconds = None
    if seconds is None:
        raise Failed(f"Config Error: unable to parse {name}, must be a valid time format")
    return int(seconds / 60)


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmMgGtTpP]i?[bB])?\s*$")
_SIZE_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4, "p": 1024**5}


def _size_bytes(value, name):
    """Upstream size_parse: a number is bytes; '500MB' and '500MiB' both mean binary multiples."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    match = _SIZE.match(value) if isinstance(value, str) else None
    if not match:
        raise Failed(f"Config Error: unable to parse {name}, must be a valid size format like '500MB', '4GB', or '1024'")
    unit = (match.group(2) or "b")[0].lower()
    return int(float(match.group(1)) * _SIZE_UNITS[unit])


def _tag_list(value, name):
    if value is None:
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",")]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise Failed(f"Config Error: {name} must be a list of tags")


def _number(value, name, min_value, default, types=(int, float)):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, types) or value < min_value:
        raise Failed(f"Config Error: {name} must be a number of at least {min_value}")
    return value


def _flag(value, name, default):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise Failed(f"Config Error: {name} must be true or false")
    return value


def _sort_share_limits(data):
    """Groups in priority order; a missing priority becomes the next free one, a duplicate is an error."""
    groups = []
    for name, options in data.items():
        if options is not None and not isinstance(options, dict):
            raise Failed(f"Config Error: share_limits grouping '{name}' must be a mapping")
        groups.append((name, dict(options or {})))
    groups.sort(key=lambda item: item[1].get("priority", float("inf")))
    priorities = set()
    for name, options in groups:
        if "priority" in options:
            priority = options["priority"]
            if isinstance(priority, bool) or not isinstance(priority, (int, float)):
                raise Failed(f"Config Error: priority of the grouping '{name}' must be a number")
            if priority in priorities:
                raise Failed(
                    f"Config Error: Duplicate priority '{priority}' found in share_limits for the grouping '{name}'. "
                    "Priority must be a unique value and greater than or equal to 1"
                )
        else:
            priority = (max(priorities) if priorities else 0) + 1
            logger.warning("Priority not defined for the grouping '%s' in share_limits. Setting priority to %s", name, priority)
            options["priority"] = priority
        priorities.add(priority)
    return groups


def _check_share_limits(data, settings):
    """share_limits groupings as upstream reads them, in priority order. Labels stand in for tags."""
    if not isinstance(data, dict) or not data:
        raise Failed("Config Error: share_limits. No valid grouping found.")
    ignore_tags = {
        settings["nohardlinks_tag"],
        settings["tracker_error_tag"],
        settings["share_limits_min_seeding_time_tag"],
        settings["share_limits_min_num_seeds_tag"],
        settings["share_limits_last_active_tag"],
        settings["share_limits_tag"],
        settings["private_tag"],
    }
    custom_tags = []
    result = {}
    for group, options in _sort_share_limits(data):
        where = f"share_limits.{group}"
        _ignored(f"{where}.", set(options) - SHARE_LIMIT_KEYS)
        if options.get("categories"):
            raise Failed(
                f"Config Error: {where}.categories cannot be used, Transmission has no categories; "
                "filter by label with include_all_tags / include_any_tags instead"
            )
        cfg = {"priority": options["priority"]}
        for key in ("include_all_tags", "include_any_tags", "exclude_all_tags", "exclude_any_tags"):
            cfg[key] = _tag_list(options.get(key), f"{where}.{key}")
        for key in ("min_torrent_size", "max_torrent_size"):
            cfg[key] = None if options.get(key) is None else _size_bytes(options[key], f"{where}.{key}")
        cfg["cleanup"] = _flag(options.get("cleanup"), f"{where}.cleanup", False)
        cfg["max_ratio"] = _number(options.get("max_ratio"), f"{where}.max_ratio", -2, -1)
        for key, min_int, default in (("max_seeding_time", -2, -1), ("max_last_active", -1, -1), ("min_seeding_time", 0, 0)):
            value = options.get(key)
            cfg[key] = default if value is None else _time_minutes(value, f"{where}.{key}", min_int)
        cfg["limit_upload_speed"] = _number(options.get("limit_upload_speed"), f"{where}.limit_upload_speed", -1, -1, (int,))
        cfg["upload_speed_on_limit_reached"] = _number(
            options.get("upload_speed_on_limit_reached"), f"{where}.upload_speed_on_limit_reached", -1, 0, (int,)
        )
        cfg["enable_group_upload_speed"] = _flag(
            options.get("enable_group_upload_speed"), f"{where}.enable_group_upload_speed", False
        )
        cfg["min_num_seeds"] = _number(options.get("min_num_seeds"), f"{where}.min_num_seeds", 0, 0, (int,))
        cfg["share_limit_action"] = validate_share_limit_action(options.get("share_limit_action"), cfg["cleanup"], group)
        # 'last_active' is the old name of min_last_active and wins when both are set, as upstream.
        last_active = options.get("last_active") or options.get("min_last_active")
        cfg["min_last_active"] = 0 if last_active is None else _time_minutes(last_active, f"{where}.min_last_active", 0)
        for key in ("resume_torrent_after_change", "add_group_to_tag", "reset_upload_speed_on_unmet_minimums"):
            cfg[key] = _flag(options.get(key), f"{where}.{key}", True)
        custom_tag = options.get("custom_tag")
        if custom_tag is not None and not isinstance(custom_tag, str):
            raise Failed(f"Config Error: {where}.custom_tag must be a string")
        cfg["custom_tag"] = custom_tag or None
        if cfg["custom_tag"]:
            if cfg["custom_tag"] in custom_tags or cfg["custom_tag"] in ignore_tags:
                raise Failed(
                    f"Config Error: Duplicate custom tag '{cfg['custom_tag']}' found in share_limits for the grouping "
                    f"'{group}'. Custom tag must be a unique value."
                )
            custom_tags.append(cfg["custom_tag"])
        _check_share_limit_values(cfg, group)
        result[str(group)] = cfg
    return result, custom_tags


def _check_share_limit_values(cfg, group):
    min_size, max_size = cfg["min_torrent_size"], cfg["max_torrent_size"]
    if min_size is not None and max_size is not None and min_size > max_size:
        raise Failed(
            f"Config Error: min_torrent_size ({min_size} bytes) is greater than max_torrent_size ({max_size} bytes) "
            f"for the grouping '{group}'."
        )
    if cfg["min_seeding_time"] > 0 and cfg["max_seeding_time"] != -1 and cfg["min_seeding_time"] > cfg["max_seeding_time"]:
        raise Failed(
            f"Config Error: min_seeding_time ({cfg['min_seeding_time']}) is greater than max_seeding_time "
            f"({cfg['max_seeding_time']}) for the grouping '{group}'. min_seeding_time must be less than or equal to "
            "max_seeding_time or max_seeding_time must be unlimited (-1)."
        )
    if cfg["min_seeding_time"] > 0 and cfg["max_ratio"] <= 0:
        raise Failed(
            f"Config Error: min_seeding_time ({cfg['min_seeding_time']}) is set, but max_ratio ({cfg['max_ratio']}) is "
            f"not set for the grouping '{group}'. max_ratio must be greater than 0 when min_seeding_time is set."
        )


def _check_webhooks(data):
    data = data or {}
    if not isinstance(data, dict):
        raise Failed("Config error: 'webhooks' must be a mapping")
    _ignored("webhooks.", set(data) - set(WEBHOOK_KEYS))

    def url(path, value):
        if value in (None, ""):
            return None
        if value in ("notifiarr", "apprise"):
            logger.warning("Config: 'webhooks.%s: %s' is not ported and is ignored", path, value)
            return None
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise Failed(f"Config error: 'webhooks.{path}' must be an http(s) URL")
        return value

    result = {key: url(key, data.get(key)) for key in ("error", "run_start", "run_end")}
    functions = data.get("function") or {}
    if not isinstance(functions, dict):
        raise Failed("Config error: 'webhooks.function' must be a mapping of command -> URL")
    result["function"] = {name: url(f"function.{name}", value) for name, value in functions.items()}
    return result


class Config:
    """Validated configuration. ``commands`` holds the effective command switches for this run."""

    def __init__(self, path, cli_commands=None):
        data = load_yaml(path)
        for name, why in QBIT_ONLY_SECTIONS.items():
            if name in data:
                logger.warning("Config: section '%s' is ignored (%s)", name, why)
        known = {
            "commands",
            "transmission",
            "settings",
            "directory",
            "tracker",
            "nohardlinks",
            "webhooks",
            "hooks",
            "orphaned",
            "share_limits",
            *QBIT_ONLY_SECTIONS,
        }
        _ignored("", set(data) - known)

        self.commands = self._merge_commands(data.get("commands"), cli_commands or {})
        self.transmission = _check_section("transmission", data.get("transmission"), TRANSMISSION_SCHEMA)
        if self.transmission["protocol"] not in ("auto", "jsonrpc", "legacy"):
            raise Failed("Config error: 'transmission.protocol' must be auto, jsonrpc or legacy")
        self.settings = _check_section("settings", data.get("settings"), SETTINGS_SCHEMA)
        self.settings["rem_unregistered_ignore_list"] = list(self.settings["rem_unregistered_ignore_list"] or [])
        self.directory = _check_section("directory", data.get("directory"), DIRECTORY_SCHEMA)
        self.orphaned = _check_section("orphaned", data.get("orphaned"), ORPHANED_SCHEMA)
        self._resolve_orphaned()
        self.tracker = _check_tracker(data.get("tracker"))
        self.nohardlinks = _check_nohardlinks(data.get("nohardlinks"))
        self.webhooks = _check_webhooks(data.get("webhooks"))
        self.hooks = _check_section("hooks", data.get("hooks"), HOOKS_SCHEMA)
        # recheck reads the groups too: the seeding-time limit exists only in this config (see share_limits).
        self.share_limits = {}
        self.share_limits_custom_tags = []
        if self.commands["share_limits"] or (self.commands["recheck"] and data.get("share_limits")):
            self.share_limits, self.share_limits_custom_tags = _check_share_limits(data.get("share_limits"), self.settings)

    def _resolve_orphaned(self):
        """Upstream defaults: orphaned_dir is remote_dir/orphaned_data, and its own files are never orphans."""
        orphaned = self.orphaned
        orphaned["exclude_patterns"] = list(orphaned["exclude_patterns"] or [])
        if orphaned["max_orphaned_files_to_delete"] < -1:
            raise Failed("Config error: 'orphaned.max_orphaned_files_to_delete' must be -1 or more")
        if orphaned["min_file_age_minutes"] < 0:
            raise Failed("Config error: 'orphaned.min_file_age_minutes' must be 0 or more")
        if not self.commands["rem_orphaned"]:
            return
        root = self.directory["root_dir"]
        if not root:
            raise Failed("Config error: rem_orphaned needs directory.root_dir")
        if not self.directory["orphaned_dir"]:
            self.directory["orphaned_dir"] = os.path.join(self.directory["remote_dir"] or root, "orphaned_data")
        pattern = f"**{os.sep}{os.path.basename(self.directory['orphaned_dir'].rstrip(os.sep))}{os.sep}*"
        if pattern not in orphaned["exclude_patterns"]:
            orphaned["exclude_patterns"].append(pattern)

    @staticmethod
    def _merge_commands(config_commands, cli_commands):
        # As in qbit_manage, a commands section in the config file overrides the command line.
        commands = {name: bool(cli_commands.get(name, False)) for name in COMMANDS}
        if config_commands is None:
            return commands
        if not isinstance(config_commands, dict):
            raise Failed("Config error: 'commands' must be a mapping")
        _ignored("commands.", set(config_commands) - set(COMMANDS))
        for name, value in config_commands.items():
            if name not in COMMANDS:
                continue
            if not isinstance(value, bool):
                raise Failed(f"Config error: 'commands.{name}' must be true or false")
            commands[name] = value
        logger.info("Commands from the config file override the command line")
        return commands
