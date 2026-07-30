"""Tests for config loading, legacy migration, and the dismissed store."""

import importlib
import json

import pytest


@pytest.fixture
def config_mod(tmp_path, monkeypatch):
    """Reload the config module against a throwaway XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("ACTIVITY_SLACK_TOKEN", raising=False)
    monkeypatch.delenv("ACTIVITY_SLACK_COOKIE", raising=False)
    from activity import config

    return importlib.reload(config)


def write_config(config_mod, payload):
    config_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod.CONFIG_PATH.write_text(json.dumps(payload))


def test_load_without_any_source_exits(config_mod):
    with pytest.raises(SystemExit):
        config_mod.load()


def test_load_reads_both_sections(config_mod):
    write_config(
        config_mod,
        {
            "bell": True,
            "slack": {"token": "xoxc-1", "cookie": "xoxd-1", "watch": ["#eng"]},
            "gmail": {"user": "you@gmail.com", "app_password": "pw", "watch": ["@stripe.com"]},
        },
    )
    cfg = config_mod.load()
    assert cfg.bell is True
    assert cfg.slack.token == "xoxc-1"
    assert cfg.slack.watch == ["#eng"]
    assert cfg.gmail.user == "you@gmail.com"
    assert cfg.gmail.mailbox == "INBOX"


def test_partial_gmail_section_is_ignored(config_mod):
    write_config(
        config_mod,
        {"slack": {"token": "t", "cookie": "c"}, "gmail": {"user": "you@gmail.com"}},
    )
    cfg = config_mod.load()
    assert cfg.gmail is None
    assert cfg.has_source


def test_env_overrides_slack_credentials(config_mod, monkeypatch):
    write_config(config_mod, {"slack": {"token": "stale", "cookie": "stale"}})
    monkeypatch.setenv("ACTIVITY_SLACK_TOKEN", "xoxc-env")
    monkeypatch.setenv("ACTIVITY_SLACK_COOKIE", "xoxd-env")
    cfg = config_mod.load()
    assert (cfg.slack.token, cfg.slack.cookie) == ("xoxc-env", "xoxd-env")


def test_legacy_flat_config_is_migrated(config_mod):
    config_mod.LEGACY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (config_mod.LEGACY_CONFIG_DIR / "config.json").write_text(
        json.dumps({"token": "xoxc-old", "cookie": "xoxd-old", "watch": ["#eng"], "bell": True})
    )
    (config_mod.LEGACY_CONFIG_DIR / "dismissed.json").write_text(json.dumps([]))

    cfg = config_mod.load()
    assert cfg.slack.token == "xoxc-old"
    assert cfg.slack.watch == ["#eng"]
    assert cfg.bell is True
    # The migration is written through, so the legacy file is only read once.
    assert json.loads(config_mod.CONFIG_PATH.read_text())["slack"]["token"] == "xoxc-old"
    assert config_mod.DISMISSED_PATH.exists()


def test_save_section_leaves_other_sections_alone(config_mod):
    write_config(config_mod, {"bell": True, "slack": {"token": "t", "cookie": "c"}})
    config_mod.save_section("gmail", {"user": "you@gmail.com", "app_password": "pw"})
    data = json.loads(config_mod.CONFIG_PATH.read_text())
    assert data["slack"]["token"] == "t"
    assert data["bell"] is True
    assert data["gmail"]["user"] == "you@gmail.com"


def test_dismissed_upgrades_legacy_records(config_mod):
    recent = 4_000_000_000  # far future, so the TTL prune keeps it
    config_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_mod.DISMISSED_PATH.write_text(
        json.dumps(
            [
                f"C123:{recent}",
                {"channel": "C456", "channel_name": "#eng", "ts": str(recent), "is_dm": True},
                {"source": "gmail", "id": "m@x", "ts": recent},
                {"channel": "C789", "ts": "1"},  # older than the TTL, pruned
            ]
        )
    )
    records = config_mod.load_dismissed()
    assert [(r["source"], r["id"]) for r in records] == [
        ("slack", f"C123:{recent}"),
        ("slack", f"C456:{recent}"),
        ("gmail", "m@x"),
    ]
    assert records[1]["group"] == "#eng"
    assert records[1]["is_direct"] is True
