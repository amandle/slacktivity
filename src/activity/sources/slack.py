"""Slack notification source, authed with a browser-session token.

Auth is the ``xoxc-...`` token sent as a form field plus the ``d`` cookie. This
is the same credential pair the Slack web app uses, so it needs no app install
and no workspace-admin approval. It is tied to your browser session and will
break when that session ends.
"""

import html
import json
import re
from collections.abc import Callable

import httpx

from .base import SLACK
from .base import Notification

BASE_URL = "https://slack.com/api/"
# Slack rejects requests from session tokens without a browser-like UA.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Per-channel history page size, and how much recent history to pull the first
# time a favorited channel is seen with nothing unread.
HISTORY_LIMIT = 50
FAVORITE_BACKFILL = 15

# Slack message markup -> readable text.
RE_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
RE_CHANNEL = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]+))?>")
RE_BROADCAST = re.compile(r"<!(\w+)(?:\|([^>]+))?>")
RE_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")


class SlackError(Exception):
    """Raised when Slack returns ``{"ok": false}``."""


def _scrape_block_text(node: object, out: list[str]) -> None:
    """Recursively collect text from Block Kit blocks (sections, headers, rich_text, etc.)."""
    if isinstance(node, dict):
        if node.get("type") == "link" and node.get("url"):
            out.append(node.get("text") or node["url"])
            return
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                out.append(value)
            else:
                _scrape_block_text(value, out)
    elif isinstance(node, list):
        for item in node:
            _scrape_block_text(item, out)


def assemble_body(raw: dict) -> str:
    """Build display text from a message's text, attachments, then Block Kit blocks.

    App messages (e.g. the GitHub PR bot) often leave top-level ``text`` empty and
    carry their content in ``attachments`` or ``blocks`` instead.
    """
    parts: list[str] = []
    if raw.get("text"):
        parts.append(raw["text"])
    for att in raw.get("attachments", []):
        present = [att[k] for k in ("pretext", "title", "text") if att.get(k)]
        parts.extend(present)
        if not present and att.get("fallback"):
            parts.append(att["fallback"])
    if not parts and raw.get("blocks"):
        _scrape_block_text(raw["blocks"], parts)

    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return "  ·  ".join(deduped)


def normalize_starred(value: object) -> set[str]:
    """Slack returns starred channels as a list of IDs, list of dicts, or a JSON string."""
    if not value:
        return set()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return set()
    ids: set[str] = set()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict):
                cid = item.get("id") or item.get("channel")
                if cid:
                    ids.add(cid)
    return ids


class SlackSource:
    name = SLACK

    def __init__(self, token: str, cookie: str, watch: list[str] | None = None) -> None:
        cookie_header = cookie if cookie.startswith("d=") else f"d={cookie}"
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Cookie": cookie_header},
            timeout=30.0,
        )
        self._watch = watch or []
        self._user_names: dict[str, str] = {}
        self.own_id = ""
        self.team_id = ""
        # channel id -> {"name": str|None, "is_dm": bool, "user": str|None}
        self.channels: dict[str, dict] = {}
        self.favorite_ids: set[str] = set()
        self.last_read: dict[str, float] = {}
        self.newest_ts: dict[str, str] = {}
        self._seen: dict[str, Notification] = {}

    # ---- api ------------------------------------------------------------

    async def call(self, method: str, **params: object) -> dict:
        params["token"] = self._token
        resp = await self._http.post(method, data=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error', 'unknown error')}")
        return data

    async def user_name(self, user_id: str) -> str:
        """Resolve a user ID to a display name, cached for the session."""
        if user_id not in self._user_names:
            try:
                user = (await self.call("users.info", user=user_id))["user"]
                profile = user.get("profile", {})
                self._user_names[user_id] = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user.get("name")
                    or user_id
                )
            except (SlackError, httpx.HTTPError):
                self._user_names[user_id] = user_id
        return self._user_names[user_id]

    async def close(self) -> None:
        await self._http.aclose()

    # ---- setup ----------------------------------------------------------

    async def start(self, progress: Callable[[str], None]) -> None:
        progress("connecting to Slack…")
        auth = await self.call("auth.test")
        self.own_id = auth["user_id"]
        self.team_id = auth.get("team_id", "")
        progress("loading Slack channels…")
        await self._load_channels()
        progress("loading Slack favorites…")
        await self._load_favorites()

    async def _load_channels(self) -> None:
        cursor = ""
        while True:
            data = await self.call(
                "conversations.list",
                types="public_channel,private_channel,mpim,im",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
            for c in data.get("channels", []):
                cid = c["id"]
                if c.get("is_im"):
                    # DM name is resolved lazily so we don't fan out users.info at startup.
                    self.channels[cid] = {"name": None, "is_dm": True, "user": c.get("user")}
                elif c.get("is_mpim"):
                    name = c.get("name", "group-dm").replace("mpdm-", "")
                    self.channels[cid] = {"name": name, "is_dm": True, "user": None}
                else:
                    self.channels[cid] = {
                        "name": f"#{c.get('name', cid)}",
                        "is_dm": False,
                        "user": None,
                    }
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break

    async def _load_favorites(self) -> None:
        """Populate favorites from Slack-starred channels plus the config watch list."""
        self.favorite_ids = self._config_watch_ids()
        try:
            boot = await self.call("client.userBoot")
        except (SlackError, httpx.HTTPError):
            return
        starred = boot.get("starred")
        if starred is None:
            starred = boot.get("prefs", {}).get("starred")
        self.favorite_ids |= normalize_starred(starred)

    def _config_watch_ids(self) -> set[str]:
        """Resolve config `watch` entries (names or IDs) to channel IDs."""
        wanted = {w.lstrip("#@").lower() for w in self._watch}
        ids: set[str] = set()
        for cid, meta in self.channels.items():
            if cid in self._watch:
                ids.add(cid)
                continue
            name = (meta.get("name") or "").lstrip("#@").lower()
            if name and name in wanted:
                ids.add(cid)
        return ids

    # ---- polling --------------------------------------------------------

    async def poll(self) -> list[Notification]:
        counts = await self.call("client.counts")
        entries = counts.get("channels", []) + counts.get("mpims", []) + counts.get("ims", [])
        for entry in entries:
            await self._sync_channel(entry)
        # Unread state is per-channel and moves when you read in Slack itself, so
        # refresh it across everything still cached rather than only new arrivals.
        for nid, n in self._seen.items():
            channel = nid.split(":", 1)[0]
            n.unread = n.unread and n.ts > self.last_read.get(channel, 0.0)
        return list(self._seen.values())

    async def _sync_channel(self, entry: dict) -> None:
        cid = entry["id"]
        has_unread = bool(entry.get("has_unreads"))
        favorited = cid in self.favorite_ids
        last_read = float(entry.get("last_read", "0") or 0)
        self.last_read[cid] = max(self.last_read.get(cid, 0.0), last_read)
        if not (has_unread or favorited):
            return

        first_fetch = cid not in self.newest_ts
        params: dict[str, object] = {"channel": cid, "limit": HISTORY_LIMIT}
        if not first_fetch:
            params["oldest"] = self.newest_ts[cid]
        elif has_unread:
            params["oldest"] = entry.get("last_read", "0")
        else:  # favorited, first sight, no unreads: pull a little recent history
            params["limit"] = FAVORITE_BACKFILL

        try:
            history = await self.call("conversations.history", **params)
        except (SlackError, httpx.HTTPError):
            return

        raw_messages = [m for m in history.get("messages", []) if m.get("ts")]
        for raw in raw_messages:
            await self._ingest(cid, raw)

        if raw_messages:
            newest = max(m["ts"] for m in raw_messages)
            if float(newest) > float(self.newest_ts.get(cid, "0")):
                self.newest_ts[cid] = newest
            # Backfilled favorite history wasn't truly unread — treat it as read.
            if first_fetch and not has_unread:
                self.last_read[cid] = max(self.last_read.get(cid, 0.0), float(newest))

    async def _ingest(self, cid: str, raw: dict) -> None:
        nid = f"{cid}:{raw['ts']}"
        if nid in self._seen:
            return
        meta = self.channels.get(cid, {"is_dm": False})
        body = assemble_body(raw)
        is_mention = f"<@{self.own_id}>" in body or any(
            tag in body for tag in ("<!here", "<!channel", "<!everyone")
        )
        ts = float(raw["ts"])
        # Your own messages are never unread, however Slack's counters read.
        is_own = raw.get("user") == self.own_id
        self._seen[nid] = Notification(
            source=SLACK,
            id=nid,
            group=await self.channel_name(cid),
            author=await self._author_name(raw),
            text=await self.render_text(body),
            ts=ts,
            unread=not is_own and ts > self.last_read.get(cid, 0.0),
            is_direct=bool(meta.get("is_dm")),
            is_mention=is_mention,
            favorite=cid in self.favorite_ids,
            # slack://channel?team=...&id=...&message=<ts> jumps the desktop app to the message.
            link=f"slack://channel?team={self.team_id}&id={cid}&message={raw['ts']}",
        )

    async def channel_name(self, cid: str) -> str:
        meta = self.channels.get(cid)
        if not meta:
            return cid
        if meta.get("name"):
            return meta["name"]
        if meta.get("user"):
            name = f"@{await self.user_name(meta['user'])}"
            meta["name"] = name
            return name
        return cid

    async def _author_name(self, raw: dict) -> str:
        if raw.get("user"):
            return await self.user_name(raw["user"])
        return raw.get("username") or raw.get("bot_id") or "system"

    async def render_text(self, raw: str) -> str:
        text = raw
        for uid in set(RE_USER.findall(text)):
            name = await self.user_name(uid)
            text = re.sub(rf"<@{uid}(?:\|[^>]+)?>", f"@{name}", text)
        text = RE_CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
        text = RE_BROADCAST.sub(lambda m: "@" + (m.group(2) or m.group(1)), text)
        text = RE_LINK.sub(lambda m: m.group(2) or m.group(1), text)
        text = html.unescape(text).replace("\n", " ⏎ ")
        return text.strip()

    def trim(self, keep_ids: set[str]) -> None:
        """Drop cached notifications the app is no longer holding, to bound memory."""
        self._seen = {nid: n for nid, n in self._seen.items() if nid in keep_ids}
