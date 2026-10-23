"""Extension hooks: let another tool take part in decisions without changing this port.

Each hook is an external command that receives a JSON object on stdin. A hook that cannot be run, or that does
not finish within the timeout, counts as a "no" (the removal is skipped, the torrent is not classified).

- pre_remove runs before every torrent removal with the torrent and the planned removal. Exit status 0 lets the
  removal go ahead, anything else skips it. On exit 0 the last line of stdout may be a JSON object with
  "delete_data": false to keep the files and remove only the torrent; it can never turn a keep into a delete.
- pre_remove_batch runs once per rem_unregistered run with every planned removal, before anything is removed.
  Exit status 0 lets the run go ahead, anything else skips all of its removals.
- classify_unregistered runs for a failing torrent whose tracker messages match none of the built-in
  unregistered messages. Exit status 0 marks it unregistered.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess

logger = logging.getLogger("transmission_manage")


class Hooks:
    def __init__(self, config):
        config = config or {}
        self.pre_remove_cmd = config.get("pre_remove")
        self.pre_remove_batch_cmd = config.get("pre_remove_batch")
        self.classify_unregistered_cmd = config.get("classify_unregistered")
        self.timeout = config.get("timeout", 60)

    def _run(self, name, command_line, payload, subject, quiet=False):
        """Run a hook; (exit status was 0, stdout). A hook that could not run or timed out gives (False, "")."""
        command = shlex.split(command_line, posix=os.name != "nt")
        if os.name == "nt":  # non-POSIX splitting keeps the quotes around quoted paths
            command = [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part for part in command]
        try:
            result = subprocess.run(
                command,
                input=json.dumps({"event": name, **payload}),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as err:
            logger.warning("%s hook failed to run (%s); treating it as a no for %s", name, err, subject)
            return False, ""
        if result.returncode != 0:
            detail = (result.stdout or result.stderr or "").strip().splitlines()
            logger.log(
                logging.DEBUG if quiet else logging.INFO,
                "%s hook said no for %s (exit %d)%s",
                name,
                subject,
                result.returncode,
                f": {detail[-1]}" if detail else "",
            )
            return False, result.stdout or ""
        return True, result.stdout or ""

    def pre_remove(self, payload):
        """(allowed, delete_data). Without a pre_remove hook the removal proceeds as planned."""
        delete_data = bool(payload.get("delete_data"))
        if not self.pre_remove_cmd:
            return True, delete_data
        allowed, stdout = self._run("pre_remove", self.pre_remove_cmd, payload, payload.get("name"))
        if not allowed:
            return False, delete_data
        if _last_json_line(stdout).get("delete_data") is False and delete_data:
            logger.info("pre_remove hook keeps the files of %s; removing the torrent only", payload.get("name"))
            delete_data = False
        return True, delete_data

    def pre_remove_batch(self, payload):
        """True if the run's removals may proceed (always, when no pre_remove_batch hook is configured)."""
        if not self.pre_remove_batch_cmd:
            return True
        subject = f"{len(payload.get('torrents') or [])} planned removal(s)"
        return self._run("pre_remove_batch", self.pre_remove_batch_cmd, payload, subject)[0]

    def classify_unregistered(self, payload):
        """True if the hook marks the torrent unregistered (never, when no hook is configured)."""
        if not self.classify_unregistered_cmd:
            return False
        name = payload.get("name")
        return self._run("classify_unregistered", self.classify_unregistered_cmd, payload, name, quiet=True)[0]


def _last_json_line(stdout):
    """The JSON object on the last non-empty stdout line, or {} (other output is the hook's own logging)."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return {}
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
