"""Textual TUI: a live, filterable feed of recent Slack messages."""

import html
import json
import re
import time
import webbrowser
from dataclasses import dataclass

import httpx
from rich.markup import escape
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer
from textual.widgets import Header
from textual.widgets import Label
from textual.widgets import ListItem
from textual.widgets import ListView
from textual.widgets import Static

from .client import SlackError
from .client import SlackSession
from .config import Config
from .config import load_dismissed
from .config import save_dismissed

POLL_SECONDS = 5
# Per-channel history page size, and a cap on rows held in memory / rendered.
HISTORY_LIMIT = 50
WATCHED_BACKFILL = 15
MAX_ROWS = 500

# Fixed column widths (cells); the text column takes the rest and wraps within it.
COL_TIME_W = 14
COL_CHAN_W = 18
COL_USER_W = 14

# Cap message body length in the feed; longer text is truncated with an ellipsis.
MAX_BODY_CHARS = 300

# filter key -> (internal id, label)
FILTERS: dict[str, tuple[str, str]] = {
    "1": ("all_unread", "Unread (all)"),
    "2": ("favorites", "Favorites"),
    "3": ("dm_mentions", "DMs + mentions"),
}

# Slack message markup -> readable text.
RE_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
RE_CHANNEL = re.compile(r"<#(C[A-Z0-9]+)(?:\|([^>]+))?>")
RE_BROADCAST = re.compile(r"<!(\w+)(?:\|([^>]+))?>")
RE_LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")


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


def _assemble_body(raw: dict) -> str:
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


@dataclass
class Message:
    channel: str
    channel_name: str
    is_dm: bool
    ts: str
    author: str
    text: str
    is_mention: bool

    @property
    def ts_float(self) -> float:
        return float(self.ts)


class SlacktivityApp(App):
    CSS = """
    #status { dock: top; height: 1; padding: 0 1; background: $panel; color: $text; }
    #feed { height: 1fr; }
    #feed > ListItem { padding: 0 1; height: auto; }
    #feed .row { height: auto; align-vertical: top; }
    #feed .c-time { width: 14; }
    #feed .c-chan { width: 18; }
    #feed .c-user { width: 14; }
    #feed .c-text { width: 1fr; }
    """

    BINDINGS = [
        Binding("1", "toggle('1')", "Unread", show=True),
        Binding("2", "toggle('2')", "Favs", show=True),
        Binding("3", "toggle('3')", "DM/@", show=True),
        Binding("r", "mark_read", "Mark read", show=True),
        Binding("a", "mark_all", "Mark all read", show=True),
        Binding("d", "toggle_dismissed", "Read view", show=True),
        Binding("u", "unmark", "Mark unread", show=True),
        Binding("g", "poll_now", "Refresh", show=True),
        Binding("o", "open_message", "Open in Slack", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    @staticmethod
    def _message_from_record(record: dict) -> "Message":
        return Message(
            channel=record["channel"],
            channel_name=record.get("channel_name") or record["channel"],
            is_dm=bool(record.get("is_dm")),
            ts=record["ts"],
            author=record.get("author", ""),
            text=record.get("text") or "(no saved preview)",
            is_mention=bool(record.get("is_mention")),
        )

    @staticmethod
    def _record_from_message(m: "Message") -> dict:
        return {
            "channel": m.channel,
            "channel_name": m.channel_name,
            "is_dm": m.is_dm,
            "ts": m.ts,
            "author": m.author,
            "text": m.text,
            "is_mention": m.is_mention,
        }

    def _save_dismissed(self) -> None:
        save_dismissed([self._record_from_message(m) for m in self.dismissed.values()])

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.session = SlackSession(config.token, config.cookie)
        self.own_id: str = ""
        self.team_id: str = ""
        # channel id -> {"name": str|None, "is_dm": bool, "user": str|None}
        self.channels: dict[str, dict] = {}
        self.favorite_ids: set[str] = set()
        self.last_read: dict[str, float] = {}
        self.newest_ts: dict[str, str] = {}
        self.messages: dict[tuple[str, str], Message] = {}
        self.dismissed: dict[tuple[str, str], Message] = {}
        for record in load_dismissed():
            m = self._message_from_record(record)
            self.dismissed[(m.channel, m.ts)] = m
        self.active: set[str] = {"all_unread"}
        self.show_dismissed = False
        self.visible_order: list[tuple[str, str]] = []
        self._render_sig: tuple | None = None
        self._first_render = True
        self.status_extra = "starting…"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        yield ListView(id="feed")
        yield Footer()

    async def on_mount(self) -> None:
        self.update_status()
        await self._bootstrap()
        self.set_interval(POLL_SECONDS, self.poll)

    # ---- setup ---------------------------------------------------------

    async def _bootstrap(self) -> None:
        try:
            auth = await self.session.call("auth.test")
            self.own_id = auth["user_id"]
            self.team_id = auth.get("team_id", "")
            await self._load_channels()
        except (SlackError, httpx.HTTPError) as exc:
            self.status_extra = f"auth/setup failed: {exc}"
            self.update_status()
            return
        await self._load_favorites()
        await self.poll()

    async def _load_channels(self) -> None:
        cursor = ""
        while True:
            data = await self.session.call(
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
                    self.channels[cid] = {"name": f"#{c.get('name', cid)}", "is_dm": False, "user": None}
            cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break

    async def _load_favorites(self) -> None:
        """Populate the Favorites set from Slack-starred channels plus the config watch list."""
        self.favorite_ids = self._config_watch_ids()
        try:
            boot = await self.session.call("client.userBoot")
        except (SlackError, httpx.HTTPError):
            self.status_extra = f"{len(self.favorite_ids)} favorites (stars unavailable)"
            return
        starred = boot.get("starred")
        if starred is None:
            starred = boot.get("prefs", {}).get("starred")
        self.favorite_ids |= self._normalize_starred(starred)
        self.status_extra = f"{len(self.favorite_ids)} favorites"

    def _config_watch_ids(self) -> set[str]:
        """Resolve config `watch` entries (names or IDs) to channel IDs."""
        wanted = {w.lstrip("#@").lower() for w in self.config.watch}
        ids: set[str] = set()
        for cid, meta in self.channels.items():
            if cid in self.config.watch:
                ids.add(cid)
                continue
            name = (meta.get("name") or "").lstrip("#@").lower()
            if name and name in wanted:
                ids.add(cid)
        return ids

    @staticmethod
    def _normalize_starred(value: object) -> set[str]:
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

    # ---- polling -------------------------------------------------------

    async def poll(self) -> None:
        try:
            counts = await self.session.call("client.counts")
        except (SlackError, httpx.HTTPError) as exc:
            self.status_extra = f"poll error: {exc}"
            self.update_status()
            return

        entries = (
            counts.get("channels", []) + counts.get("mpims", []) + counts.get("ims", [])
        )
        for entry in entries:
            await self._sync_channel(entry)

        self._trim()
        self.refresh_table()

    async def _sync_channel(self, entry: dict) -> None:
        cid = entry["id"]
        has_unread = bool(entry.get("has_unreads"))
        favorited = cid in self.favorite_ids
        if cid not in self.last_read:
            self.last_read[cid] = float(entry.get("last_read", "0") or 0)
        if not (has_unread or favorited):
            return

        first_fetch = cid not in self.newest_ts
        params: dict[str, object] = {"channel": cid, "limit": HISTORY_LIMIT}
        if cid in self.newest_ts:
            params["oldest"] = self.newest_ts[cid]
        elif has_unread:
            params["oldest"] = entry.get("last_read", "0")
        else:  # favorited, first sight, no unreads: pull a little recent history
            params["limit"] = WATCHED_BACKFILL

        try:
            history = await self.session.call("conversations.history", **params)
        except (SlackError, httpx.HTTPError):
            return

        raw_messages = [m for m in history.get("messages", []) if m.get("ts")]
        for raw in raw_messages:
            await self._ingest(cid, raw)

        if raw_messages:
            newest = max(m["ts"] for m in raw_messages)
            if float(newest) > float(self.newest_ts.get(cid, "0")):
                self.newest_ts[cid] = newest
            # Backfilled watched history wasn't truly unread — treat it as read.
            if first_fetch and not has_unread:
                self.last_read[cid] = max(self.last_read.get(cid, 0.0), float(newest))

    async def _ingest(self, cid: str, raw: dict) -> None:
        key = (cid, raw["ts"])
        if key in self.messages:
            return
        meta = self.channels.get(cid, {"is_dm": False})
        body = _assemble_body(raw)
        is_mention = (
            f"<@{self.own_id}>" in body
            or any(tag in body for tag in ("<!here", "<!channel", "<!everyone"))
        )
        self.messages[key] = Message(
            channel=cid,
            channel_name=await self._channel_name(cid),
            is_dm=bool(meta.get("is_dm")),
            ts=raw["ts"],
            author=await self._author_name(raw),
            text=await self._render_text(body),
            is_mention=is_mention,
        )

    async def _channel_name(self, cid: str) -> str:
        meta = self.channels.get(cid)
        if not meta:
            return cid
        if meta.get("name"):
            return meta["name"]
        if meta.get("user"):
            name = f"@{await self.session.user_name(meta['user'])}"
            meta["name"] = name
            return name
        return cid

    async def _author_name(self, raw: dict) -> str:
        if raw.get("user"):
            return await self.session.user_name(raw["user"])
        return raw.get("username") or raw.get("bot_id") or "system"

    async def _render_text(self, raw: str) -> str:
        text = raw
        for uid in set(RE_USER.findall(text)):
            name = await self.session.user_name(uid)
            text = re.sub(rf"<@{uid}(?:\|[^>]+)?>", f"@{name}", text)
        text = RE_CHANNEL.sub(lambda m: "#" + (m.group(2) or m.group(1)), text)
        text = RE_BROADCAST.sub(lambda m: "@" + (m.group(2) or m.group(1)), text)
        text = RE_LINK.sub(lambda m: m.group(2) or m.group(1), text)
        text = html.unescape(text).replace("\n", " ⏎ ")
        return text.strip()

    def _trim(self) -> None:
        if len(self.messages) <= MAX_ROWS:
            return
        keep = sorted(self.messages.values(), key=lambda m: m.ts_float)[-MAX_ROWS:]
        self.messages = {(m.channel, m.ts): m for m in keep}

    # ---- rendering -----------------------------------------------------

    def _is_unread(self, m: Message) -> bool:
        return m.ts_float > self.last_read.get(m.channel, 0.0)

    def _is_visible(self, m: Message) -> bool:
        if (m.channel, m.ts) in self.dismissed:
            return False
        if "all_unread" in self.active and self._is_unread(m):
            return True
        if "favorites" in self.active and m.channel in self.favorite_ids:
            return True
        if "dm_mentions" in self.active and (m.is_dm or m.is_mention):
            return True
        return False

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        text = text or ""
        return text if len(text) <= width else text[: width - 1] + "…"

    def _build_item(self, m: Message, marker: str) -> ListItem:
        """A row with fixed time/channel/user columns and a wrapping text column."""
        clock = time.strftime("%m/%d %H:%M", time.localtime(m.ts_float))
        time_col = self._truncate(f"{marker} {clock}", COL_TIME_W - 1)
        chan_col = self._truncate(m.channel_name, COL_CHAN_W - 1)
        user_col = self._truncate(m.author, COL_USER_W - 1)
        # Render user text with markup disabled: Textual's parser opens a tag on
        # any '[', but the escape regex only covers lowercase-led tags, so content
        # like "[ENG-123=>]" slips through escaping and crashes the parser.
        if m.text:
            body = Label(self._truncate(m.text, MAX_BODY_CHARS), classes="c-text", markup=False)
        else:
            body = Label("[dim](no text)[/dim]", classes="c-text")
        row = Horizontal(
            Label(f"[dim]{escape(time_col)}[/dim]", classes="c-time"),
            Label(f"[b]{escape(chan_col)}[/b]", classes="c-chan"),
            Label(f"[cyan]{escape(user_col)}[/cyan]", classes="c-user"),
            body,
            classes="row",
        )
        return ListItem(row)

    def refresh_table(self) -> None:
        if self.show_dismissed:
            rows = sorted(self.dismissed.values(), key=lambda m: m.ts_float)[-MAX_ROWS:]
        else:
            rows = sorted(
                (m for m in self.messages.values() if self._is_visible(m)),
                key=lambda m: m.ts_float,
            )[-MAX_ROWS:]
        markers = ["✓" if self.show_dismissed else ("●" if self._is_unread(m) else " ") for m in rows]

        # Skip the DOM rebuild when nothing on screen would change — this stops the
        # feed from flickering on the steady-state polls that fetch no new messages.
        signature = (
            self.show_dismissed,
            frozenset(self.active),
            tuple((m.channel, m.ts, mk, m.text) for m, mk in zip(rows, markers)),
        )
        if signature == self._render_sig:
            return
        self._render_sig = signature

        feed = self.query_one("#feed", ListView)
        prev_index = feed.index
        feed.clear()
        self.visible_order = []
        items: list[ListItem] = []
        for m, marker in zip(rows, markers):
            items.append(self._build_item(m, marker))
            self.visible_order.append((m.channel, m.ts))
        feed.extend(items)
        if self.visible_order:
            if self._first_render:
                target = len(self.visible_order) - 1  # land on newest on first paint
            else:
                # Keep the highlight where it was (clamped), so dismissing advances to the next row.
                target = min(max(prev_index or 0, 0), len(self.visible_order) - 1)
            # Items mount asynchronously; set the highlight once they exist.
            self.call_after_refresh(self._set_feed_index, target)
        self._first_render = False
        self.update_status()

    def _set_feed_index(self, index: int) -> None:
        feed = self.query_one("#feed", ListView)
        if 0 <= index < len(feed):
            feed.index = index

    def update_status(self) -> None:
        try:
            status = self.query_one("#status", Static)
        except Exception:
            return
        if self.show_dismissed:
            status.update(
                f"[b]READ view[/b]   shown: {len(self.visible_order)}   "
                f"u: mark unread   d: back to feed   {self.status_extra}"
            )
            return
        labels = [label for _, (fid, label) in FILTERS.items() if fid in self.active]
        active = ", ".join(labels) if labels else "none"
        status.update(
            f"filters: [b]{active}[/b]   "
            f"shown: {len(self.visible_order)}   dismissed: {len(self.dismissed)}   {self.status_extra}"
        )

    # ---- actions -------------------------------------------------------

    def action_toggle(self, key: str) -> None:
        fid = FILTERS[key][0]
        if fid in self.active:
            self.active.discard(fid)
        else:
            self.active.add(fid)
        self.update_status()  # immediate feedback even if visible rows don't change
        self.refresh_table()

    def _selected_key(self) -> tuple[str, str] | None:
        feed = self.query_one("#feed", ListView)
        index = feed.index
        if index is None or not (0 <= index < len(self.visible_order)):
            return None
        return self.visible_order[index]

    def action_mark_read(self) -> None:
        """Dismiss the selected message from the feed (local triage, not Slack's read state)."""
        if self.show_dismissed:
            self.notify("Already in the read view — press u to mark unread.", severity="warning")
            return
        key = self._selected_key()
        m = self.messages.get(key) if key else None
        if not m:
            self.notify("No row selected.", severity="warning")
            return
        self.dismissed[key] = m
        self._save_dismissed()
        self.notify(f"Marked {m.channel_name} message read.")
        self.refresh_table()

    def action_mark_all(self) -> None:
        """Dismiss every visible message, or restore them all when in the read view."""
        if not self.visible_order:
            self.notify("Nothing here.", severity="warning")
            return
        count = len(self.visible_order)
        if self.show_dismissed:
            for key in list(self.visible_order):
                self._restore(key)
            self._save_dismissed()
            self.notify(f"Marked {count} messages unread.")
        else:
            for key in self.visible_order:
                m = self.messages.get(key)
                if m:
                    self.dismissed[key] = m
            self._save_dismissed()
            self.notify(f"Marked {count} messages read.")
        self.refresh_table()

    def action_toggle_dismissed(self) -> None:
        """Switch between the live feed and the archive of read (dismissed) messages."""
        self.show_dismissed = not self.show_dismissed
        self._first_render = True  # land on the newest row of whichever view we entered
        self.refresh_table()

    def action_unmark(self) -> None:
        """Restore the selected message from the read view back into the live feed."""
        if not self.show_dismissed:
            self.notify("Open the read view (d) to mark messages unread.", severity="warning")
            return
        key = self._selected_key()
        if not key or key not in self.dismissed:
            self.notify("No row selected.", severity="warning")
            return
        self._restore(key)
        self._save_dismissed()
        self.notify("Marked unread.")
        self.refresh_table()

    def _restore(self, key: tuple[str, str]) -> None:
        """Remove a message from the dismissed set and put it back in the live feed."""
        m = self.dismissed.pop(key, None)
        if m and key not in self.messages:
            self.messages[key] = m

    async def action_poll_now(self) -> None:
        await self.poll()

    def action_open_message(self) -> None:
        """Open the selected message in the Slack client via a deep link."""
        key = self._selected_key()
        if not key:
            self.notify("No row selected.", severity="warning")
            return
        channel, ts = key
        # slack://channel?team=...&id=...&message=<ts> jumps the desktop app to the message.
        link = f"slack://channel?team={self.team_id}&id={channel}&message={ts}"
        webbrowser.open(link)
        self.notify("Opening in Slack…")

    async def action_quit(self) -> None:
        await self.session.close()
        self.exit()
