"""Configuration: load config.yml, validate value types and merge CLI commands.

Section and key names follow qbit_manage wherever Transmission allows it. As upstream, keys this port does not
use are ignored; they are logged as warnings so a typo is visible. A value of the wrong type stops the run.
"""

from __future__ import annotations

import logging
import os

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
    "timeout": ((int,), 60),
}

WEBHOOK_KEYS = ("error", "run_start", "run_end", "function")

ORPHANED_SCHEMA = {
    "empty_after_x_days": ((int, type(None)), None),
    "min_file_age_minutes": ((int,), 0),
    "exclude_patterns": ((list, type(None)), None),
    "max_orphaned_files_to_delete": ((int,), 50),
}

# Validated by the module that consumes them once it is ported; accepted as-is until then.
PENDING_SECTIONS = ("share_limits",)

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
                result[part.strip().lower()] = tags
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
            *PENDING_SECTIONS,
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
        self.pending = {name: data.get(name) for name in PENDING_SECTIONS if name in data}

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
