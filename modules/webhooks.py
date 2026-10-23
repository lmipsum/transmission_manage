"""Webhook notifications: plain JSON POSTs to user-supplied URLs.

Mirrors qbit_manage's webhooks section (error / run_start / run_end / function) but without the Notifiarr and
Apprise integrations, to keep the dependency set small.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from modules.util import redact

logger = logging.getLogger("transmission_manage")

RETRY_ATTEMPTS = 3
RETRY_DELAY_S = 2


class Webhooks:
    def __init__(self, config, timeout=10, sleep=time.sleep):
        config = config or {}
        self.error_url = config.get("error")
        self.run_start_url = config.get("run_start")
        self.run_end_url = config.get("run_end")
        self.function_urls = config.get("function") or {}
        self.timeout = timeout
        self._sleep = sleep

    def _send(self, url, payload):
        """POST payload as JSON; retry server errors. Never raises: a broken webhook must not stop a run."""
        if not url:
            return False
        body = json.dumps(payload, default=str).encode()
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout):
                    return True
            except urllib.error.HTTPError as err:
                if err.code < 500:
                    logger.warning("Webhook %s rejected the notification: HTTP %s", redact(url), err.code)
                    return False
                reason = f"HTTP {err.code}"
            except (urllib.error.URLError, OSError) as err:
                reason = str(err)
            if attempt < RETRY_ATTEMPTS:
                logger.debug("Webhook %s failed (%s); retry %d/%d", redact(url), reason, attempt, RETRY_ATTEMPTS - 1)
                self._sleep(RETRY_DELAY_S)
        logger.warning("Webhook %s failed after %d attempts (%s)", redact(url), RETRY_ATTEMPTS, reason)
        return False

    def run_start(self, start_time, dry_run):
        return self._send(self.run_start_url, {"function": "run_start", "start_time": start_time, "dry_run": dry_run})

    def run_end(self, start_time, end_time, stats, dry_run):
        payload = {
            "function": "run_end",
            "start_time": start_time,
            "end_time": end_time,
            "run_time": round(end_time - start_time, 1),
            "dry_run": dry_run,
            "stats": stats,
        }
        return self._send(self.run_end_url, payload)

    def error(self, text, function=None, critical=True):
        payload = {"function": "run_error", "title": None, "body": redact(text), "critical": critical, "type": function}
        return self._send(self.error_url, payload)

    def function(self, name, payload):
        """Per-command notification, e.g. the list of torrents a command acted on.

        As upstream, it goes to the first configured function whose name is part of this one, so
        cleanup_share_limits uses the share_limits URL and untag_tracker_error the tag_tracker_error one.
        """
        url = next((url for key, url in self.function_urls.items() if key in name), None)
        return self._send(url, {"function": name, **payload})
