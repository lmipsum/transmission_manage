import logging
import os
import textwrap

import pytest

from modules.config import Config
from modules.util import Failed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(tmp_path, text):
    path = tmp_path / "config.yml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_sample_config_is_valid(monkeypatch):
    monkeypatch.setenv("TRANSMISSION_USER", "u")
    monkeypatch.setenv("TRANSMISSION_PASS", "p")
    config = Config(os.path.join(ROOT, "config", "config.yml.sample"))
    assert config.commands["dry_run"] is True
    assert config.transmission["user"] == "u"
    assert config.tracker["tracker-two-backup.example.net"] == ["TrackerTwo", "private"]
    assert config.hooks == {"pre_remove": None, "timeout": 60}


def test_defaults_when_sections_are_missing(tmp_path):
    config = Config(write(tmp_path, "transmission:\n  host: 'box:9091'\n"))
    assert config.transmission["host"] == "box:9091"
    assert config.settings["rem_unregistered_grace_minutes"] == 10
    assert config.settings["rem_unregistered_ignore_list"] == []
    assert not any(config.commands.values())


def test_env_tag_missing_variable_becomes_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("TM_TEST_UNSET", raising=False)
    config = Config(write(tmp_path, "transmission:\n  pass: !ENV TM_TEST_UNSET\n"))
    assert config.transmission["pass"] == ""


def test_unused_keys_and_sections_are_ignored_with_a_warning(tmp_path, caplog):
    logging.getLogger("transmission_manage").propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="transmission_manage"):
            config = Config(
                write(tmp_path, "settings:\n  typo_key: 5\nqbt:\n  host: x\nbogus: {}\ncommands:\n  cat_update: true\n")
            )
    finally:
        logging.getLogger("transmission_manage").propagate = False
    text = caplog.text
    for expected in ("settings.typo_key", "'qbt' is ignored", "'bogus", "commands.cat_update"):
        assert expected in text
    assert "typo_key" not in config.settings


def test_wrong_type_stops_the_run(tmp_path):
    with pytest.raises(Failed, match="must not be a boolean"):
        Config(write(tmp_path, "settings:\n  rem_unregistered_max_torrents: true\n"))
    with pytest.raises(Failed, match="hooks.timeout"):
        Config(write(tmp_path, "hooks:\n  timeout: soon\n"))


def test_config_commands_override_the_command_line(tmp_path):
    path = write(tmp_path, "commands:\n  dry_run: false\n  rem_unregistered: true\n")
    config = Config(path, {"dry_run": True, "tag_update": True})
    assert config.commands["dry_run"] is False
    assert config.commands["rem_unregistered"] is True
    assert config.commands["tag_update"] is True  # not set in the file, so the CLI value stays


def test_cli_commands_apply_without_a_commands_section(tmp_path):
    config = Config(write(tmp_path, "transmission: {}\n"), {"dry_run": True, "config_file": "x"})
    assert config.commands["dry_run"] is True


def test_command_values_must_be_boolean(tmp_path):
    with pytest.raises(Failed, match="commands.dry_run"):
        Config(write(tmp_path, "commands:\n  dry_run: yes please\n"))


def test_tracker_needs_a_tag(tmp_path):
    with pytest.raises(Failed, match="needs a 'tag'"):
        Config(write(tmp_path, "tracker:\n  example.org: {}\n"))


def test_webhooks_ignore_unported_integrations_and_reject_bad_urls(tmp_path):
    config = Config(write(tmp_path, "webhooks:\n  error: notifiarr\n"))
    assert config.webhooks["error"] is None
    with pytest.raises(Failed, match="http\\(s\\) URL"):
        Config(write(tmp_path, "webhooks:\n  function:\n    rem_unregistered: ftp://x\n"))


def test_nohardlinks_category_dir_option_is_ignored(tmp_path):
    config = Config(write(tmp_path, "nohardlinks:\n  label-a:\n    ignore_category_dir: true\n    ignore_root_dir: false\n"))
    assert config.nohardlinks == {"label-a": {"ignore_root_dir": False}}


def test_missing_file_and_empty_file(tmp_path):
    with pytest.raises(Failed, match="not found"):
        Config(str(tmp_path / "missing.yml"))
    with pytest.raises(Failed, match="empty"):
        Config(write(tmp_path, ""))
