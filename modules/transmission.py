"""Transmission RPC client.

Speaks the JSON-RPC 2.0 protocol of Transmission 4.1+ (``rpc_version_semver`` 6.0.0) and falls back to the
legacy protocol for older daemons. Callers always use the snake_case method and field names of the current
spec; for a legacy daemon the client translates names on the way out and back, so every response is returned
with snake_case keys whichever protocol is in use.

Torrents are always addressed by info hash: integer torrent ids are not stable across daemon restarts.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger("transmission_manage")

# libtransmission/transmission.h (4.1.0): tr_torrent_activity
STATUS_STOPPED = 0
STATUS_CHECK_WAIT = 1
STATUS_CHECK = 2
STATUS_DOWNLOAD_WAIT = 3
STATUS_DOWNLOAD = 4
STATUS_SEED_WAIT = 5
STATUS_SEED = 6

# tr_stat_errtype
STAT_OK = 0
STAT_TRACKER_WARNING = 1
STAT_TRACKER_ERROR = 2
STAT_LOCAL_ERROR = 3

# tr_tracker_state (tracker_stats[].announce_state / scrape_state)
TRACKER_INACTIVE = 0
TRACKER_WAITING = 1
TRACKER_QUEUED = 2
TRACKER_ACTIVE = 3  # announcing or scraping right now

# tr_stat.ratio sentinels
RATIO_NA = -1
RATIO_INF = -2

# tr_ratiolimit (seed_ratio_mode) and tr_idlelimit (seed_idle_mode)
LIMIT_GLOBAL = 0
LIMIT_SINGLE = 1
LIMIT_UNLIMITED = 2

JSONRPC_MIN_SEMVER = (6, 0, 0)
SESSION_ID_HEADER = "X-Transmission-Session-Id"
RPC_VERSION_HEADER = "X-Transmission-Rpc-Version"

# Mutating methods act on ALL torrents when "ids" is omitted (spec 3.1), so they must never be sent without it.
_MUTATING_METHODS = {
    "torrent_remove",
    "torrent_set",
    "torrent_start",
    "torrent_start_now",
    "torrent_stop",
    "torrent_verify",
    "torrent_reannounce",
}


class TransmissionError(Exception):
    """An RPC call failed: transport error, authentication, or an error reported by the daemon."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _semver(text):
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _snake_to_camel(name):
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _snake_to_kebab(name):
    return name.replace("_", "-")


def _to_snake(name):
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).replace("-", "_").lower()


def _snake_keys(value):
    if isinstance(value, dict):
        return {_to_snake(key): _snake_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snake_keys(item) for item in value]
    return value


# The legacy session arguments are kebab-case except for these camelCase ones.
_LEGACY_CAMEL_SESSION = {"seed_ratio_limit", "seed_ratio_limited"}


def _legacy_session_name(name):
    return _snake_to_camel(name) if name in _LEGACY_CAMEL_SESSION else _snake_to_kebab(name)


def _legacy_params(method, params):
    """Translate snake_case params to the legacy protocol's naming for this method."""
    params = dict(params)
    if method == "torrent_get" and "fields" in params:
        params["fields"] = [_snake_to_camel(field) for field in params["fields"]]
        return params
    if method == "session_get" and "fields" in params:
        params["fields"] = [_legacy_session_name(field) for field in params["fields"]]
        return params
    if method == "session_set":
        return {_legacy_session_name(key): item for key, item in params.items()}
    if method == "torrent_set":
        return {key if key == "ids" else _snake_to_camel(key): item for key, item in params.items()}
    if method in ("torrent_remove", "free_space"):
        return {key if key == "ids" else _snake_to_kebab(key): item for key, item in params.items()}
    return params


def ratio(torrent):
    """upload_ratio with the sentinels resolved: None when not applicable, math.inf when infinite."""
    value = torrent.get("upload_ratio")
    if value is None or value == RATIO_NA:
        return None
    if value == RATIO_INF:
        return math.inf
    return float(value)


def known_count(value):
    """A tracker peer count, or None when the tracker has not reported one (-1). Never sum raw counts."""
    if value is None or value < 0:
        return None
    return value


class TransmissionClient:
    """Minimal synchronous client for the Transmission RPC API."""

    def __init__(
        self,
        host="localhost:9091",
        rpc_path="/transmission/rpc",
        user=None,
        password=None,
        ssl=False,
        protocol="auto",
        timeout=30,
        retries=2,
    ):
        if protocol not in ("auto", "jsonrpc", "legacy"):
            raise ValueError(f"protocol must be auto, jsonrpc or legacy, not {protocol!r}")
        scheme = "https" if ssl else "http"
        self.url = f"{scheme}://{host}{rpc_path}"
        self.timeout = timeout
        self.retries = retries
        self.protocol = None if protocol == "auto" else protocol
        self.rpc_version_semver = None
        self._session_id = None
        self._auth = None
        if user or password:
            token = base64.b64encode(f"{user or ''}:{password or ''}".encode()).decode()
            self._auth = f"Basic {token}"
        self._ids = itertools.count(1)

    # --- transport -----------------------------------------------------------------------------------

    def _post(self, payload):
        """POST a JSON payload, handling the CSRF session-id handshake. Returns the decoded response body."""
        body = json.dumps(payload).encode()
        attempt = 0
        handshakes = 0
        while True:
            headers = {"Content-Type": "application/json"}
            if self._session_id:
                headers[SESSION_ID_HEADER] = self._session_id
            if self._auth:
                headers["Authorization"] = self._auth
            request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as err:
                # The session id can expire at any time; a fresh one always deserves a retry, independent of
                # the retries for connection errors. Two in a row means the daemon keeps rejecting it.
                if err.code == 409 and handshakes < 2:
                    handshakes += 1
                    self._note_handshake(err.headers)
                    continue
                if err.code == 401:
                    raise TransmissionError("authentication failed (check user/pass)", code=401) from err
                raise TransmissionError(f"HTTP {err.code} from {self.url}: {err.reason}", code=err.code) from err
            except (urllib.error.URLError, OSError) as err:
                if attempt < self.retries:
                    time.sleep(min(2**attempt, 5))
                    attempt += 1
                    continue
                raise TransmissionError(f"cannot reach {self.url}: {err}") from err
            except json.JSONDecodeError as err:
                raise TransmissionError(f"invalid JSON from {self.url}: {err}") from err

    def _note_handshake(self, headers):
        session_id = headers.get(SESSION_ID_HEADER)
        if session_id:
            self._session_id = session_id
        version = headers.get(RPC_VERSION_HEADER)
        if version:
            self.rpc_version_semver = version
        if self.protocol is None:
            # Since rpc_version_semver 6.0.0 the 409 carries the version, so the scheme is known without an
            # extra request; an older daemon sends no version header and only speaks the legacy protocol.
            parsed = _semver(version)
            self.protocol = "jsonrpc" if parsed and parsed >= JSONRPC_MIN_SEMVER else "legacy"

    def _detect_protocol(self):
        # The first request has no session id, so the daemon answers 409 and _note_handshake decides.
        # session_get is valid in both schemes; only its naming differs, which does not matter here.
        payload = {"jsonrpc": "2.0", "method": "session_get", "params": {"fields": ["version"]}, "id": next(self._ids)}
        response = self._post(payload)
        if self.protocol is None:
            # No 409 at all (CSRF protection disabled): tell the schemes apart by the reply's shape.
            self.protocol = "jsonrpc" if "jsonrpc" in response else "legacy"
        logger.debug("Transmission RPC protocol: %s (rpc_version_semver=%s)", self.protocol, self.rpc_version_semver)

    # --- calls ----------------------------------------------------------------------------------------

    def call(self, method, params=None):
        """Call an RPC method using snake_case names. Returns the result object with snake_case keys."""
        params = dict(params or {})
        if method in _MUTATING_METHODS and not params.get("ids"):
            raise ValueError(f"{method} needs a non-empty ids list (an omitted ids acts on every torrent)")
        if self.protocol is None:
            self._detect_protocol()
        if self.protocol == "jsonrpc":
            payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": next(self._ids)}
            response = self._post(payload)
            if "error" in response:
                error = response["error"] or {}
                detail = (error.get("data") or {}).get("errorString")
                message = error.get("message", "unknown error") + (f": {detail}" if detail else "")
                raise TransmissionError(f"{method} failed: {message}", code=error.get("code"))
            return _snake_keys(response.get("result") or {})
        payload = {"method": method.replace("_", "-"), "arguments": _legacy_params(method, params), "tag": next(self._ids)}
        response = self._post(payload)
        if response.get("result") != "success":
            raise TransmissionError(f"{method} failed: {response.get('result', 'no result')}")
        return _snake_keys(response.get("arguments") or {})

    def session(self, fields=None):
        params = {"fields": list(fields)} if fields else {}
        return self.call("session_get", params)

    def torrents(self, fields, hashes=None):
        """torrent_get for the given fields (hash_string is always included). Raises if the list looks broken."""
        wanted = list(dict.fromkeys(["hash_string", *fields]))
        params = {"fields": wanted}
        if hashes is not None:
            if not hashes:
                return []
            params["ids"] = list(hashes)
        result = self.call("torrent_get", params)
        if "torrents" not in result:
            raise TransmissionError("torrent_get returned no torrent list")
        torrents = result["torrents"]
        if any(not isinstance(torrent, dict) or not torrent.get("hash_string") for torrent in torrents):
            # A partial list must never drive a cleanup decision.
            raise TransmissionError("torrent_get returned torrents without a hash; refusing a partial list")
        return torrents

    def set_labels(self, hashes, labels):
        self.call("torrent_set", {"ids": list(hashes), "labels": list(labels)})

    def set_torrent(self, hashes, fields):
        """torrent_set with snake_case field names, e.g. seed_ratio_mode or upload_limit."""
        self.call("torrent_set", {**fields, "ids": list(hashes)})

    def set_session(self, fields):
        self.call("session_set", dict(fields))

    def remove(self, hashes, delete_data=False):
        self.call("torrent_remove", {"ids": list(hashes), "delete_local_data": bool(delete_data)})

    def start(self, hashes):
        self.call("torrent_start", {"ids": list(hashes)})

    def stop(self, hashes):
        self.call("torrent_stop", {"ids": list(hashes)})

    def verify(self, hashes):
        self.call("torrent_verify", {"ids": list(hashes)})

    def reannounce(self, hashes):
        self.call("torrent_reannounce", {"ids": list(hashes)})

    def free_space(self, path):
        return self.call("free_space", {"path": path})
