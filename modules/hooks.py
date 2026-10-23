"""Extension hooks: let another tool take part in decisions without changing this port.

pre_remove runs before every torrent removal. It receives a JSON object on stdin describing the torrent and the
planned removal; exit status 0 lets the removal go ahead, anything else skips it. A hook that cannot be run, or
that does not finish within the timeout, also skips the removal.
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
        self.timeout = config.get("timeout", 60)

    def pre_remove(self, payload):
        """True if the removal may proceed (always, when no pre_remove hook is configured)."""
        if not self.pre_remove_cmd:
            return True
        command = shlex.split(self.pre_remove_cmd, posix=os.name != "nt")
        if os.name == "nt":  # non-POSIX splitting keeps the quotes around quoted paths
            command = [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part for part in command]
        try:
            result = subprocess.run(
                command,
                input=json.dumps({"event": "pre_remove", **payload}),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as err:
            logger.warning("pre_remove hook failed to run (%s); skipping the removal of %s", err, payload.get("name"))
            return False
        if result.returncode != 0:
            detail = (result.stdout or result.stderr or "").strip().splitlines()
            logger.info(
                "pre_remove hook declined the removal of %s (exit %d)%s",
                payload.get("name"),
                result.returncode,
                f": {detail[-1]}" if detail else "",
            )
            return False
        return True
