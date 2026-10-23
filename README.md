# transmission_manage

A port of [qbit_manage](https://github.com/StuffAnthings/qbit_manage) to the
[Transmission](https://transmissionbt.com/) BitTorrent client.

qbit_manage automates the chores of running a qBittorrent box on private
trackers. Nothing comparable exists for Transmission, so this project ports its
modules, keeping qbit_manage's behaviour, module names and configuration keys
wherever Transmission allows it. Where Transmission works differently (no
categories, a different tracker status model), the port says so below.

> **Status:** early work in progress, no release yet. The modules below are
> ported one by one, tracked in the issues.

## Usage

Requires Python 3.10+ and network access to Transmission's RPC port.

```bash
git clone https://github.com/lmipsum/transmission_manage.git
cd transmission_manage
python -m pip install .
cp config/config.yml.sample config/config.yml   # then edit it
python transmission_manage.py --config-file config/config.yml
```

Runs are one-shot: schedule them with cron or a systemd timer. The sample config
starts in `dry_run` mode; review the log of a dry run before switching it off.
Command-line flags mirror qbit_manage (`--rem-unregistered`, `--tag-nohardlinks`,
`--dry-run`, ...); a `commands:` section in the config file overrides them.
Commands that are not ported yet are skipped with a warning, and configuration
keys the port does not use are ignored with a warning.

Exit codes: `0` success, `1` Transmission unreachable or an RPC error, `2` invalid
configuration.

## Scope

| qbit_manage module | Status | Transmission notes |
|---|---|---|
| Client (replaces `qbittorrent.py`) | done | JSON-RPC 2.0 for Transmission 4.1+, detected from the CSRF handshake; legacy protocol for older daemons. Torrents are addressed by info hash, and calls that change torrents refuse an empty selection (an omitted `ids` acts on every torrent). |
| Config, `dry_run`, webhooks | done | qbit_manage key names. Webhooks are plain URLs; Notifiarr and Apprise are not ported. |
| `remove_unregistered` + `tag_tracker_error` | done | Tracker state is read from `tracker_stats`: a successful announce on any tracker means working (qBittorrent's WORKING); not yet announced, queued or announcing means inconclusive (NOT_CONTACTED / UPDATING); backup trackers are ignored. |
| `tag_nohardlinks` | done | The `nohardlinks` section is keyed by Transmission label, since Transmission has no categories; `ignore_category_dir` is therefore not available. |
| `remove_orphaned` | planned | |
| `recheck` | planned | |
| Tracker tagging | planned | Transmission has free-form `labels` instead of qBittorrent tags. |
| `share_limits` | later | |
| Categories and other qBittorrent-only features | out of scope | No Transmission equivalent. |

## Hooks

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

## Credits and license

Derived from [qbit_manage](https://github.com/StuffAnthings/qbit_manage),
Copyright (c) 2021 StuffAnThings, released under the MIT License. See
[NOTICE](NOTICE). This project is not affiliated with qbit_manage or
Transmission.

Released under the [MIT License](LICENSE).
