"""Credential and watch-list storage for slacktivity.

Credentials are your own Slack browser-session token (``xoxc-...``) and the
``d`` cookie (``xoxd-...``). They are read from the environment first, then
from ``~/.config/slacktivity/config.json``.
"""

import json
import os
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "slacktivity"
CONFIG_PATH = CONFIG_DIR / "config.json"
DISMISSED_PATH = CONFIG_DIR / "dismissed.json"
# Drop dismissed-message records older than this so the file doesn't grow forever.
DISMISSED_TTL_SECONDS = 30 * 24 * 3600


@dataclass
class Config:
    token: str
    cookie: str
    # Channel names (with or without leading '#'/'@') or IDs to surface under the "Watched" filter.
    watch: list[str] = field(default_factory=list)
    # Ring the terminal bell when a new unread message arrives.
    bell: bool = False


def load() -> Config:
    """Load credentials from env vars, falling back to the config file."""
    data: dict = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text())

    token = os.environ.get("SLACKTIVITY_TOKEN") or data.get("token")
    cookie = os.environ.get("SLACKTIVITY_COOKIE") or data.get("cookie")
    watch = data.get("watch", [])
    bell = data.get("bell", False)

    if not token or not cookie:
        raise SystemExit(
            "No Slack credentials found.\n"
            "Run `slacktivity auth` to set them up, or set "
            "SLACKTIVITY_TOKEN and SLACKTIVITY_COOKIE."
        )
    return Config(token=token, cookie=cookie, watch=watch, bell=bell)


def save(token: str, cookie: str, watch: list[str] | None = None, bell: bool = False) -> Path:
    """Persist credentials to the config file with owner-only permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"token": token, "cookie": cookie, "watch": watch or [], "bell": bell}
    CONFIG_PATH.write_text(json.dumps(payload, indent=2))
    CONFIG_PATH.chmod(0o600)
    return CONFIG_PATH


def load_dismissed() -> list[dict]:
    """Load dismissed-message records, pruning those older than the TTL.

    Records are dicts holding enough to redisplay and restore a message. The
    legacy format was a bare ``"<channel>:<ts>"`` string; those are upgraded to
    stub records so old dismissals still appear (without a saved preview).
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
        if isinstance(item, str):
            channel, _, ts = item.rpartition(":")
            record = {"channel": channel, "ts": ts}
        elif isinstance(item, dict):
            record = item
        else:
            continue
        ts = record.get("ts")
        try:
            if ts and float(ts) >= cutoff:
                records.append(record)
        except (TypeError, ValueError):
            continue
    return records


def save_dismissed(records: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DISMISSED_PATH.write_text(json.dumps(records))
