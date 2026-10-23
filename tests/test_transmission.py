import math

import pytest

from modules import transmission as tr
from modules.transmission import TransmissionClient
from modules.transmission import TransmissionError


def client_for(server, **kwargs):
    return TransmissionClient(host=server.host, retries=0, timeout=5, **kwargs)


def test_detects_jsonrpc_from_the_409_version_header(jsonrpc_server):
    client = client_for(jsonrpc_server)
    session = client.session(["version"])
    assert client.protocol == "jsonrpc"
    assert client.rpc_version_semver == "6.0.0"
    assert session["version"].startswith("4.1")
    last = jsonrpc_server.requests[-1]["body"]
    assert last["jsonrpc"] == "2.0" and last["method"] == "session_get"


def test_falls_back_to_legacy_without_a_version_header(legacy_server):
    client = client_for(legacy_server)
    torrents = client.torrents(["name", "upload_ratio", "tracker_stats"])
    assert client.protocol == "legacy"
    last = legacy_server.requests[-1]["body"]
    assert last["method"] == "torrent-get"
    assert last["arguments"]["fields"] == ["hashString", "name", "uploadRatio", "trackerStats"]
    # Responses come back with snake_case keys whichever protocol was used.
    assert torrents[0]["hash_string"] == "a" * 40
    assert torrents[0]["tracker_stats"][0]["last_announce_result"] == "Success"


def test_both_protocols_return_the_same_shape(jsonrpc_server, legacy_server):
    fields = ["name", "status", "tracker_stats"]
    assert client_for(jsonrpc_server).torrents(fields) == client_for(legacy_server).torrents(fields)


def test_torrents_by_hash_and_empty_hash_list(jsonrpc_server):
    client = client_for(jsonrpc_server)
    assert [t["hash_string"] for t in client.torrents(["name"], hashes=["b" * 40])] == ["b" * 40]
    sent = len(jsonrpc_server.requests)
    assert client.torrents(["name"], hashes=[]) == []
    assert len(jsonrpc_server.requests) == sent  # no request for an empty selection


def test_partial_torrent_list_is_refused(jsonrpc_server):
    jsonrpc_server.torrents.append({"id": 3, "name": "no hash"})
    with pytest.raises(TransmissionError, match="partial"):
        client_for(jsonrpc_server).torrents(["name"])


@pytest.mark.parametrize("call", ["remove", "set_labels", "start", "stop", "verify", "reannounce"])
def test_mutating_calls_refuse_an_empty_selection(jsonrpc_server, call):
    client = client_for(jsonrpc_server)
    args = ([], ["x"]) if call == "set_labels" else ([],)
    with pytest.raises(ValueError, match="every torrent"):
        getattr(client, call)(*args)
    assert jsonrpc_server.requests == []


def test_remove_uses_each_protocols_naming(jsonrpc_server, legacy_server):
    client_for(jsonrpc_server).remove(["a" * 40], delete_data=True)
    assert jsonrpc_server.requests[-1]["body"]["params"] == {"ids": ["a" * 40], "delete_local_data": True}
    client_for(legacy_server).remove(["a" * 40], delete_data=True)
    body = legacy_server.requests[-1]["body"]
    assert body["method"] == "torrent-remove"
    assert body["arguments"] == {"ids": ["a" * 40], "delete-local-data": True}


def test_set_labels_legacy_naming(legacy_server):
    client_for(legacy_server).set_labels(["a" * 40], ["noHL"])
    assert legacy_server.requests[-1]["body"]["arguments"] == {"ids": ["a" * 40], "labels": ["noHL"]}


def test_limits_and_session_use_each_protocols_naming(jsonrpc_server, legacy_server):
    fields = {"seed_ratio_mode": 1, "seed_ratio_limit": 2.0, "upload_limited": True}
    client_for(jsonrpc_server).set_torrent(["a" * 40], fields)
    assert jsonrpc_server.requests[-1]["body"]["params"] == {**fields, "ids": ["a" * 40]}
    legacy = client_for(legacy_server)
    legacy.set_torrent(["a" * 40], fields)
    assert legacy_server.requests[-1]["body"]["arguments"] == {
        "seedRatioMode": 1,
        "seedRatioLimit": 2.0,
        "uploadLimited": True,
        "ids": ["a" * 40],
    }
    # Legacy session arguments are kebab-case, except seedRatioLimit and seedRatioLimited.
    legacy.set_session({"seed_ratio_limited": False, "idle_seeding_limit_enabled": False})
    assert legacy_server.requests[-1]["body"]["arguments"] == {"seedRatioLimited": False, "idle-seeding-limit-enabled": False}
    legacy.session(["seed_ratio_limit", "idle_seeding_limit_enabled", "units"])
    assert legacy_server.requests[-1]["body"]["arguments"]["fields"] == ["seedRatioLimit", "idle-seeding-limit-enabled", "units"]


def test_jsonrpc_error_surfaces_the_daemon_message(jsonrpc_server):
    jsonrpc_server.jsonrpc_error = {"code": 7, "message": "backend error", "data": {"errorString": "disk full"}}
    with pytest.raises(TransmissionError, match="backend error: disk full") as err:
        client_for(jsonrpc_server).session()
    assert err.value.code == 7


def test_authentication_failure(jsonrpc_server):
    jsonrpc_server.fail_with = 401
    with pytest.raises(TransmissionError, match="authentication") as err:
        client_for(jsonrpc_server).session()
    assert err.value.code == 401


def test_expired_session_id_is_renewed_even_without_retries(jsonrpc_server):
    client = client_for(jsonrpc_server)
    jsonrpc_server.expire_session_after = 1
    client.session()  # detection + first call; the daemon rotates its id afterwards
    client.session()  # gets a 409 with the new id and must retry transparently
    assert jsonrpc_server.session_id == "sid-2"


def test_unreachable_daemon():
    client = TransmissionClient(host="127.0.0.1:9", retries=0, timeout=1)
    with pytest.raises(TransmissionError, match="cannot reach"):
        client.session()


def test_explicit_protocol_skips_detection(legacy_server):
    client = client_for(legacy_server, protocol="legacy")
    client.session()
    assert all("jsonrpc" not in r["body"] for r in legacy_server.requests)


def test_ratio_and_count_sentinels():
    assert tr.ratio({"upload_ratio": -1}) is None
    assert tr.ratio({"upload_ratio": -2}) == math.inf
    assert tr.ratio({"upload_ratio": 1.25}) == 1.25
    assert tr.ratio({}) is None
    assert tr.known_count(-1) is None
    assert tr.known_count(0) == 0


@pytest.mark.parametrize(
    ("snake", "camel"),
    [
        ("hash_string", "hashString"),
        ("tracker_stats", "trackerStats"),
        ("last_announce_succeeded", "lastAnnounceSucceeded"),
        ("is_backup", "isBackup"),
        ("upload_ratio", "uploadRatio"),
        ("id", "id"),
    ],
)
def test_name_translation_round_trip(snake, camel):
    assert tr._snake_to_camel(snake) == camel
    assert tr._to_snake(camel) == snake
