#!/usr/bin/env python3
"""transmission_manage: qbit_manage's torrent chores for Transmission.

One-shot by design: run it from cron or a systemd timer. Command-line flags mirror qbit_manage; a commands
section in the config file overrides them.
"""

from __future__ import annotations

import argparse
import sys
import time

from modules.config import Config
from modules.core import recheck
from modules.core import remove_orphaned
from modules.core import remove_unregistered
from modules.core import tag_nohardlinks
from modules.hooks import Hooks
from modules.logs import setup_logging
from modules.transmission import TransmissionClient
from modules.transmission import TransmissionError
from modules.util import Failed
from modules.webhooks import Webhooks

__version__ = "0.1.0.dev0"

# Execution order follows qbit_manage: fix state first, then tag, then remove.
COMMAND_ORDER = (
    "recheck",
    "tag_update",
    "rem_unregistered",
    "tag_tracker_error",
    "tag_nohardlinks",
    "share_limits",
    "rem_orphaned",
)

# command name -> callable(context) -> dict of stats. Filled in as modules are ported. As in qbit_manage, one
# module can serve several commands; it then runs once and reads the command switches itself.
COMMANDS = {
    "recheck": recheck.run,
    "rem_unregistered": remove_unregistered.run,
    "tag_tracker_error": remove_unregistered.run,
    "tag_nohardlinks": tag_nohardlinks.run,
    "rem_orphaned": remove_orphaned.run,
}


def build_parser():
    parser = argparse.ArgumentParser(prog="transmission_manage", description=__doc__.splitlines()[0])
    parser.add_argument("-c", "--config-file", dest="config_file", default="config/config.yml", help="Path to config.yml")
    parser.add_argument("-lf", "--log-file", dest="log_file", default=None, help="Optional log file (rotated)")
    parser.add_argument(
        "-r", "--run", dest="run", action="store_true", help="Accepted for qbit_manage parity; runs are always one-shot"
    )
    parser.add_argument(
        "-re", "--recheck", dest="recheck", action="store_true", help="Verify and resume torrents with a local error"
    )
    parser.add_argument("-tu", "--tag-update", dest="tag_update", action="store_true", help="Label torrents by tracker")
    parser.add_argument(
        "-ru", "--rem-unregistered", dest="rem_unregistered", action="store_true", help="Remove unregistered torrents"
    )
    parser.add_argument(
        "-tte", "--tag-tracker-error", dest="tag_tracker_error", action="store_true", help="Label torrents with tracker errors"
    )
    parser.add_argument(
        "-ro", "--rem-orphaned", dest="rem_orphaned", action="store_true", help="Move orphaned files out of the root dir"
    )
    parser.add_argument(
        "-tnhl", "--tag-nohardlinks", dest="tag_nohardlinks", action="store_true", help="Label torrents without hardlinks"
    )
    parser.add_argument("-sl", "--share-limits", dest="share_limits", action="store_true", help="Apply share limits")
    parser.add_argument(
        "-sc", "--skip-cleanup", dest="skip_cleanup", action="store_true", help="Skip emptying the orphaned folder"
    )
    parser.add_argument("-dr", "--dry-run", dest="dry_run", action="store_true", help="Log what would change, change nothing")
    parser.add_argument("-ll", "--log-level", dest="log_level", default="INFO", help="Log level (default: INFO)")
    parser.add_argument("-ls", "--log-size", dest="log_size", type=int, default=10, help="Max log file size in MB")
    parser.add_argument("-lc", "--log-count", dest="log_count", type=int, default=5, help="Number of rotated log files to keep")
    parser.add_argument("-v", "--version", dest="version", action="store_true", help="Display the version and exit")
    return parser


class Context:
    """What a command needs: the client, the validated config, notifications and hooks."""

    def __init__(self, client, config, webhooks, hooks):
        self.client = client
        self.config = config
        self.webhooks = webhooks
        self.hooks = hooks
        self.dry_run = config.commands["dry_run"]


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"transmission_manage {__version__}")
        return 0
    logger = setup_logging(args.log_level, args.log_file, args.log_size, args.log_count)

    try:
        config = Config(args.config_file, vars(args))
    except Failed as err:
        logger.critical("%s", err)
        return 2

    webhooks = Webhooks(config.webhooks)
    dry_run = config.commands["dry_run"]
    start = time.time()
    webhooks.run_start(start, dry_run)
    if dry_run:
        logger.info("Dry run: nothing will be changed")
    stats = {}
    exit_code = 0
    try:
        tx = config.transmission
        client = TransmissionClient(
            host=tx["host"],
            rpc_path=tx["rpc_path"],
            user=tx["user"],
            password=tx["pass"],
            ssl=tx["ssl"],
            protocol=tx["protocol"],
            timeout=tx["timeout"],
            retries=tx["retries"],
        )
        session = client.session(["version"])
        logger.info(
            "Connected to Transmission %s (protocol %s, rpc %s)",
            session.get("version", "?"),
            client.protocol,
            client.rpc_version_semver or "legacy",
        )
        context = Context(client, config, webhooks, Hooks(config.hooks))
        done = set()
        for name in COMMAND_ORDER:
            if not config.commands[name]:
                continue
            command = COMMANDS.get(name)
            if command is None:
                logger.warning("Command '%s' is not ported yet; skipping", name)
                continue
            if command in done:
                continue
            done.add(command)
            stats[name] = command(context)
        # As upstream, the orphaned folder is emptied after the commands (unless skip_cleanup).
        emptied = remove_orphaned.cleanup_orphaned_dir(context)
        if emptied:
            stats["orphaned_emptied"] = emptied
    except (TransmissionError, Failed) as err:
        logger.critical("%s", err)
        webhooks.error(str(err))
        exit_code = 1
    end = time.time()
    webhooks.run_end(start, end, stats, dry_run)
    logger.info("Finished in %.1fs", end - start)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
