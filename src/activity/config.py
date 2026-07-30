"""Credential and preference storage for activity.

Config lives at ``~/.config/activity/config.json`` with one section per source:

    {
      "bell": true,
      "slack": {"token": "xoxc-...", "cookie": "xoxd-...", "watch": ["#eng"]},
      "gmail": {"user": "you@gmail.com", "app_password": "...", "watch": ["@stripe.com"]}
    }

A source with no section is simply not polled. Slack credentials can also come
from the environment. The older flat format written under
``~/.config/slacktivity/`` is read and migrated on first run.
"""

import json
import os
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_DIR = CONFIG_HOME / "activity"
LEGACY_CONFIG_DIR = CONFIG_HOME / "slacktivity"
CONFIG_PATH = CONFIG_DIR / "config.json"
DISMISSED_PATH = CONFIG_DIR / "dismissed.json"
# Drop dismissed-message records older than this so the file doesn't grow forever.
DISMISSED_TTL_SECONDS = 30 * 24 * 3600

SLACK_TOKEN_ENV = "ACTIVITY_SLACK_TOKEN"
SLACK_COOKIE_ENV = "ACTIVITY_SLACK_COOKIE"

NO_SOURCES_MESSAGE = (
    "No sources configured.\nRun `activity auth` to set up Slack, Gmail, or both."
)


@dataclass
class SlackConfig:
    token: str
    cookie: str
    # Channel names (with or without leading '#'/'@') or IDs to treat as favorites.
    watch: list[str] = field(default_factory=list)


@dataclass
class GmailConfig:
    user: str
    app_password: str
    mailbox: str = "INBOX"
    # Sender addresses, or "@domain" suffixes, whose mail is always a favorite.
    watch: list[str] = field(default_factory=list)


@dataclass
class Config:
    slack: SlackConfig | None = None
    gmail: GmailConfig | None = None
    # Ring the terminal bell when a new unread notification arrives.
    bell: bool = False

    @property
    def has_source(self) -> bool:
        return bool(self.slack or self.gmail)


def _read_raw() -> dict:
    """Read the config file, migrating the legacy slacktivity layout if that's all there is."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    legacy_path = LEGACY_CONFIG_DIR / "config.json"
    if not legacy_path.exists():
        return {}
    legacy = json.loads(legacy_path.read_text())
    migrated = {
        "bell": legacy.get("bell", False),
        "slack": {
            "token": legacy.get("token", ""),
            "cookie": legacy.get("cookie", ""),
            "watch": legacy.get("watch", []),
        },
    }
    _write_raw(migrated)
    legacy_dismissed = LEGACY_CONFIG_DIR / "dismissed.json"
    if legacy_dismissed.exists() and not DISMISSED_PATH.exists():
        DISMISSED_PATH.write_text(legacy_dismissed.read_text())
    return migrated


def _write_raw(payload: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH


def load() -> Config:
    """Build the config from the config file, with env overrides for Slack."""
    data = _read_raw()

    slack_data = data.get("slack", {})
    token = os.environ.get(SLACK_TOKEN_ENV) or slack_data.get("token")
    cookie = os.environ.get(SLACK_COOKIE_ENV) or slack_data.get("cookie")
    slack = None
    if token and cookie:
        slack = SlackConfig(token=token, cookie=cookie, watch=slack_data.get("watch", []))

    gmail_data = data.get("gmail", {})
    gmail = None
    if gmail_data.get("user") and gmail_data.get("app_password"):
        gmail = GmailConfig(
            user=gmail_data["user"],
            app_password=gmail_data["app_password"],
            mailbox=gmail_data.get("mailbox", "INBOX"),
            watch=gmail_data.get("watch", []),
        )

    config = Config(slack=slack, gmail=gmail, bell=data.get("bell", False))
    if not config.has_source:
        raise SystemExit(NO_SOURCES_MESSAGE)
    return config


def save(config: Config) -> Path:
    """Persist the whole config with owner-only permissions."""
    payload: dict = {"bell": config.bell}
    if config.slack:
        payload["slack"] = {
            "token": config.slack.token,
            "cookie": config.slack.cookie,
            "watch": config.slack.watch,
        }
    if config.gmail:
        payload["gmail"] = {
            "user": config.gmail.user,
            "app_password": config.gmail.app_password,
            "mailbox": config.gmail.mailbox,
            "watch": config.gmail.watch,
        }
    return _write_raw(payload)


def save_section(name: str, section: dict) -> Path:
    """Write one source's section, leaving the rest of the config untouched."""
    data = _read_raw()
    data[name] = section
    return _write_raw(data)


def load_dismissed() -> list[dict]:
    """Load dismissed-notification records, pruning those older than the TTL.

    Records hold enough to redisplay and restore a notification. Two older
    formats are upgraded on read: a bare ``"<channel>:<ts>"`` string, and the
    Slack-only dict keyed by ``channel``/``ts``.
    """
    if not DISMISSED_PATH.exists():
        return []
    try:
        raw = json.loads(DISMISSED_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    cutoff = time.time() - DISMISSED_TTL_SECONDS
    records: list[dict] = []
    for item in raw:
        record = _upgrade_dismissed(item)
        if not record:
            continue
        try:
            if float(record["ts"]) >= cutoff:
                records.append(record)
        except (KeyError, TypeError, ValueError):
            continue
    return records


def _upgrade_dismissed(item: object) -> dict | None:
    if isinstance(item, str):
        channel, _, ts = item.rpartition(":")
        return {"source": "slack", "id": f"{channel}:{ts}", "ts": ts}
    if not isinstance(item, dict):
        return None
    if "source" in item and "id" in item:
        return item
    channel = item.get("channel")
    ts = item.get("ts")
    if not channel or not ts:
        return None
    return {
        "source": "slack",
        "id": f"{channel}:{ts}",
        "group": item.get("channel_name") or channel,
        "author": item.get("author", ""),
        "text": item.get("text", ""),
        "ts": ts,
        "is_direct": item.get("is_dm", False),
        "is_mention": item.get("is_mention", False),
    }


def save_dismissed(records: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DISMISSED_PATH.write_text(json.dumps(records))
