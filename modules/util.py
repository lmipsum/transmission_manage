"""Shared helpers: config loading, tracker messages, log redaction and hardlink detection."""

from __future__ import annotations

import logging
import os
import re

import yaml

logger = logging.getLogger("transmission_manage")


class Failed(Exception):
    """A user-facing error that stops the run (bad config, unusable environment)."""


class TorrentMessages:
    """Tracker messages, matched upper-cased. Ported from qbit_manage's util.TorrentMessages.

    The tracker-specific list for a single site is left out: this project keeps no tracker names.
    """

    UNREGISTERED_MSGS = [
        "UNREGISTERED",
        "TORRENT NOT FOUND",
        "TORRENT IS NOT FOUND",
        "NOT REGISTERED",
        "NOT EXIST",
        "UNKNOWN TORRENT",
        "TRUMP",
        "RETITLED",
        "TRUNCATED",
        "TORRENT IS NOT AUTHORIZED FOR USE ON THIS TRACKER",
        "INFOHASH NOT FOUND.",
        "TORRENT HAS BEEN DELETED.",
        "TRACKER NICHT REGISTRIERT.",
        "TORRENT EXISTIERT NICHT",
        "TORRENT NICHT GEFUNDEN",
        "TORRENT INTROUVABLE",
        "TORRENT DELETED",
        "TORRENT BANNED",
    ]

    # Never treated as unregistered, even when an UNREGISTERED_MSGS entry also matches.
    IGNORE_MSGS = [
        "YOU HAVE REACHED THE CLIENT LIMIT FOR THIS TORRENT",
        "PASSKEY",  # a passkey problem affects every torrent of the tracker, never a single dead one
        "MISSING INFO_HASH",
        "EXPECTED VALUE (LIST, DICT, INT OR STRING) IN BENCODED STRING",
        "COULD NOT PARSE BENCODED DATA",
        "STREAM TRUNCATED",
        "GATEWAY TIMEOUT",
        "ANNOUNCE IS CURRENTLY UNAVAILABLE",
        "TORRENT HAS BEEN POSTPONED",
        "520 (UNKNOWN HTTP ERROR)",
    ]

    # A failing tracker with one of these messages is down, not a sign that the torrent has a problem.
    EXCEPTIONS_MSGS = [
        "DOWN",
        "DOWN.",
        "IT MAY BE DOWN,",
        "UNREACHABLE",
        "(UNREACHABLE)",
        "BAD GATEWAY",
        "TRACKER UNAVAILABLE",
    ]


def list_in_text(text, search_list, match_all=False):
    """Ported from qbit_manage: single words must match a whole word of text, phrases match as substrings."""
    search_set = set(search_list)
    phrases = {item for item in search_set if " " in item}
    words = search_set - phrases
    text_words = text.split(" ")
    if match_all:
        return all(word in text_words for word in words) and all(phrase in text for phrase in phrases)
    return any(word in words for word in text_words) or any(phrase in text for phrase in phrases)


class _EnvLoader(yaml.SafeLoader):
    """SafeLoader with qbit_manage's ``!ENV VAR_NAME`` tag: the value is read from the environment."""


def _env_constructor(loader, node):
    name = loader.construct_scalar(node)
    value = os.getenv(name)
    if value is None:
        logger.warning("Environment variable '%s' is not set; using an empty string", name)
        return ""
    return value


_EnvLoader.add_constructor("!ENV", _env_constructor)


def load_yaml(path):
    """Load a YAML mapping, resolving ``!ENV`` tags. Raises Failed on a missing, empty or invalid file."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=_EnvLoader)  # noqa: S506 - SafeLoader subclass
    except FileNotFoundError as err:
        raise Failed(f"Config file not found: {path}") from err
    except yaml.YAMLError as err:
        raise Failed(f"YAML error in {path}: {err}") from err
    if not isinstance(data, dict) or not data:
        raise Failed(f"Config file is empty or not a mapping: {path}")
    return data


# Passkeys live in announce URLs as a query parameter or as the path segment next to "announce"
# (/announce/<key>, /<key>/announce). Only URLs are touched: bare info hashes in log lines stay readable.
_URL = re.compile(r"(?i)\b((?:https?|udp|wss?)://[^/\s'\"<>]+)([^\s'\"<>]*)")
_URL_QUERY_SECRET = re.compile(r"(?i)\b(passkey|authkey|torrent_pass|apikey|key|pid|uid)=[^&#\s]+")
_URL_AFTER_ANNOUNCE = re.compile(r"(?i)(/announce)/[^/?#]+")
_URL_BEFORE_ANNOUNCE = re.compile(r"(?i)/[^/?#]{16,}(?=/announce\b)")


def _redact_url(match):
    # Scheme, host and port stay as they are; only the path and query can carry a key.
    authority, rest = match.group(1), match.group(2)
    rest = _URL_QUERY_SECRET.sub(lambda m: f"{m.group(1)}=***", rest)
    rest = _URL_AFTER_ANNOUNCE.sub(r"\1/***", rest)
    return authority + _URL_BEFORE_ANNOUNCE.sub("/***", rest)


def redact(text):
    """Mask passkeys and similar secrets in URLs before they reach a log line or a webhook."""
    if not text:
        return text
    return _URL.sub(_redact_url, str(text))


class RedactingFilter(logging.Filter):
    """Logging filter that applies redact() to every record."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def path_replace(path, old_prefix, new_prefix):
    """Swap a leading directory (root_dir -> remote_dir) when the tool sees the data under another path."""
    if not path or not old_prefix or not new_prefix:
        return path
    old = os.path.normpath(old_prefix)
    normalized = os.path.normpath(path)
    if normalized == old or normalized.startswith(old + os.sep):
        return os.path.normpath(os.path.join(new_prefix, os.path.relpath(normalized, old)))
    return path


def get_root_files(*directories):
    """Every regular, non-symlink file below the given directories (missing or empty arguments are skipped)."""
    files = []
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        for current, _dirs, names in os.walk(directory):
            files.extend(os.path.join(current, name) for name in names)
    return files


class CheckHardLinks:
    """Hardlink detection, ported from qbit_manage's util.CheckHardLinks.

    A file counts as linked when it has more links than the ones inside the download root (ignore_root_dir), so a
    link anywhere outside the root is detected without walking any other directory. For a folder, only files
    of at least 10% of the largest file are checked, so a leftover sample or subtitle cannot decide alone.
    Unreadable or empty paths fail closed (reported as linked), since they prove nothing about missing links.
    """

    THRESHOLD = 0.1

    def __init__(self, root_dir, remote_dir=None, orphaned_dir=None):
        scan_root = remote_dir or root_dir
        self.inode_count = {}
        for path in get_root_files(scan_root, orphaned_dir):
            if os.path.islink(path):
                continue
            try:
                stat = os.stat(path)
            except OSError as err:
                logger.warning("Cannot stat %s: %s", path, err)
                continue
            identity = (stat.st_dev, stat.st_ino)
            self.inode_count[identity] = self.inode_count.get(identity, 0) + 1
        logger.debug("Hardlink map: %d unique inode(s) under %s", len(self.inode_count), scan_root)

    def _has_hardlinks(self, stat, ignore_root_dir):
        if ignore_root_dir:
            return stat.st_nlink - self.inode_count.get((stat.st_dev, stat.st_ino), 1) > 0
        return stat.st_nlink > 1

    def nohardlink(self, path, ignore_root_dir=True):
        """True when no qualifying file under path has a hardlink outside the root dir."""
        try:
            if os.path.isfile(path):
                if os.path.islink(path):
                    logger.warning("Symlink at %s, unable to determine hardlinks; skipping", path)
                    return False
                return not self._has_hardlinks(os.stat(path), ignore_root_dir)
            if not os.path.isdir(path):
                logger.warning("Nohardlink: %s does not exist or is not readable; treating it as linked", path)
                return False
            files = []
            for current, _dirs, names in os.walk(path):
                for name in names:
                    full = os.path.join(current, name)
                    if os.path.islink(full):
                        logger.warning("Symlink at %s, unable to determine hardlinks; skipping it", full)
                    elif os.path.isfile(full):
                        files.append((os.stat(full), full))
            if not files:
                logger.warning("Nohardlink: no regular files in %s; treating it as linked", path)
                return False
            files.sort(key=lambda item: item[0].st_size, reverse=True)
            threshold = files[0][0].st_size * self.THRESHOLD
            for stat, _full in files:
                if stat.st_size < threshold:
                    break  # sorted by size: nothing smaller can qualify
                if self._has_hardlinks(stat, ignore_root_dir):
                    return False
            return True
        except OSError as err:
            logger.warning("Nohardlink: cannot check %s (%s); treating it as linked", path, err)
            return False
