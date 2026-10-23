# transmission_manage

[![Tests](https://img.shields.io/github/actions/workflow/status/lmipsum/transmission_manage/tests.yml?branch=main&label=tests&style=plastic)](https://github.com/lmipsum/transmission_manage/actions/workflows/tests.yml)
[![Secrets scan](https://img.shields.io/github/actions/workflow/status/lmipsum/transmission_manage/secrets-scan.yml?branch=main&label=secrets%20scan&style=plastic)](https://github.com/lmipsum/transmission_manage/actions/workflows/secrets-scan.yml)
[![License: MIT](https://img.shields.io/github/license/lmipsum/transmission_manage?style=plastic)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A port of [qbit_manage](https://github.com/StuffAnthings/qbit_manage) to the
[Transmission](https://transmissionbt.com/) BitTorrent client.

qbit_manage automates the chores of running a qBittorrent box on private
trackers. Nothing comparable exists for Transmission, so this project ports its
modules, keeping qbit_manage's behaviour, module names and configuration keys
wherever Transmission allows it. Where Transmission works differently (no
categories, a different tracker status model), the port says so below.

> **Status:** early work in progress, no release yet. The modules below are
> ported one by one, tracked in the issues.

## Supported Transmission Versions

| Transmission | RPC | Notes |
|---|---|---|
| 4.1+ | JSON-RPC 2.0 (`rpc_version_semver` 6.x) | Detected automatically from the CSRF handshake. |
| 3.00 – 4.0.x | legacy protocol | Labels, which several modules rely on, exist since 3.00 (`rpc-version` 16). |

## Getting Started

Requires Python 3.10+ and network access to Transmission's RPC port.

```bash
git clone https://github.com/lmipsum/transmission_manage.git
cd transmission_manage
python -m pip install .
cp config/config.yml.sample config/config.yml   # then edit it
```

The sample config starts in `dry_run` mode; review the log of a dry run before
switching it off.

## Usage

```bash
python transmission_manage.py --config-file config/config.yml
```

Runs are one-shot: schedule them with cron or a systemd timer. Command-line
flags mirror qbit_manage (`--rem-unregistered`, `--tag-nohardlinks`,
`--rem-orphaned`, `--dry-run`, ...); a `commands:` section in the config file
overrides them. Commands that are not ported yet are skipped with a warning, and
configuration keys the port does not use are ignored with a warning.

Exit codes: `0` success, `1` Transmission unreachable or an RPC error, `2` invalid
configuration.

### Ported modules

| qbit_manage module | Status | Transmission notes |
|---|---|---|
| Client (replaces `qbittorrent.py`) | done | Torrents are addressed by info hash, and calls that change torrents refuse an empty selection (an omitted `ids` acts on every torrent). |
| Config, `dry_run`, webhooks | done | qbit_manage key names. Webhooks are plain URLs; Notifiarr and Apprise are not ported. |
| `remove_unregistered` + `tag_tracker_error` | done | Tracker state is read from `tracker_stats`: a successful announce on any tracker means working (qBittorrent's WORKING); not yet announced, queued or announcing means inconclusive (NOT_CONTACTED / UPDATING); backup trackers are ignored. |
| `tag_nohardlinks` | done | The `nohardlinks` section is keyed by Transmission label, since Transmission has no categories; `ignore_category_dir` is therefore not available. |
| `remove_orphaned` | done | Torrent file paths are `download_dir` + `files`; for torrents that are not complete, the session's `incomplete_dir` and the `.part` suffix stand in for qBittorrent's download path. Download directories of torrents are kept when empty folders are removed, in place of category save paths. |
| `recheck` | done | A per-torrent ratio limit exists only with `seed_ratio_mode` 1; global and unlimited modes count as no limit, as upstream treats qBittorrent's -2 and -1. Transmission has no seeding-time limit (its idle limit measures inactivity), so that part of upstream's check always reads as no limit. |
| `tag_update` | done | Tags are Transmission `labels`, added to the existing ones. qBittorrent's `stalledDL` is a downloading torrent with `is_stalled`; privacy comes from `is_private`. Tracker keywords match the announce URLs case-sensitively, as upstream; `other` and the host fallback work as upstream. |
| `share_limits` | later | |
| Categories and other qBittorrent-only features | out of scope | No Transmission equivalent. |

### Hooks

The port adds one extension point and nothing else: a `pre_remove` command run
before every torrent removal, so another tool can apply its own rules without
changing the port.

```yaml
hooks:
  pre_remove: /path/to/check-removal
  timeout: 60
```

The command receives a JSON object on stdin (`command`, `hash`, `name`,
`tracker`, `status`, `delete_data`, `dry_run`). Exit status `0` lets the
removal proceed; any other status, a failure to run, or a timeout skips it.

## Development

```bash
python -m pip install -e ".[dev]"
pip install pre-commit && pre-commit install   # ruff, yamllint, tracker-secret scan
python -m pytest
```

A commit or push that contains anything shaped like a tracker passkey is
refused, both by the pre-commit hook and in CI.

## Support

Bug reports and questions: [GitHub issues](https://github.com/lmipsum/transmission_manage/issues).

## Credits and license

Derived from [qbit_manage](https://github.com/StuffAnthings/qbit_manage),
Copyright (c) 2021 StuffAnThings, released under the MIT License. See
[NOTICE](NOTICE). This project is not affiliated with qbit_manage or
Transmission.

Released under the [MIT License](LICENSE).
