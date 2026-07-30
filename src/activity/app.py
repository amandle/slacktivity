"""Textual TUI: a live, filterable feed of notifications from every configured source."""

import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from rich.markup import escape
from textual.app import App
from textual.app import ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit
from textual.command import Hit
from textual.command import Hits
from textual.command import Provider
from textual.containers import Horizontal
from textual.widgets import Footer
from textual.widgets import Header
from textual.widgets import Label
from textual.widgets import ListItem
from textual.widgets import ListView
from textual.widgets import Static

from .config import Config
from .config import load_dismissed
from .config import save
from .config import save_dismissed
from .sources.base import Notification
from .sources.base import Source
from .sources.base import notification_from_record
from .sources.base import record_from_notification
from .sources.gmail import GmailSource
from .sources.slack import SlackSource

POLL_SECONDS = 5
# Cap on rows held in memory / rendered.
MAX_ROWS = 500

# Fixed column widths (cells); the text column takes the rest and wraps within it.
COL_TIME_W = 14
COL_SRC_W = 7
COL_GROUP_W = 18
COL_AUTHOR_W = 14

# Cap body length in the feed; longer text is truncated with an ellipsis.
MAX_BODY_CHARS = 300

# filter key -> (internal id, label)
FILTERS: dict[str, tuple[str, str]] = {
    "1": ("all_unread", "Unread (all)"),
    "2": ("favorites", "Favorites"),
    "3": ("direct", "Direct + mentions"),
}

# Each filter cycles off -> on -> ghost -> off. Ghost shows matching notifications
# greyed out and never rings the bell; on shows them normally and can ring.
FILTER_OFF = "off"
FILTER_ON = "on"
FILTER_GHOST = "ghost"
FILTER_CYCLE = {FILTER_OFF: FILTER_ON, FILTER_ON: FILTER_GHOST, FILTER_GHOST: FILTER_OFF}


def build_sources(config: Config) -> list[Source]:
    """Instantiate a source per configured section, in display order."""
    sources: list[Source] = []
    if config.slack:
        sources.append(
            SlackSource(config.slack.token, config.slack.cookie, config.slack.watch)
        )
    if config.gmail:
        sources.append(
            GmailSource(
                config.gmail.user,
                config.gmail.app_password,
                config.gmail.mailbox,
                config.gmail.watch,
            )
        )
    return sources


@dataclass
class UndoEntry:
    """One reversible triage action, recorded as the changes it made to `dismissed`."""

    label: str
    dismissed: list[tuple[str, str]]  # keys this action added to dismissed
    restored: dict[tuple[str, str], Notification]  # keys this action removed from dismissed


UNDO_LIMIT = 50


class SettingsCommands(Provider):
    """Surface activity's settings and views in the command palette (ctrl+p)."""

    def _commands(self) -> list[tuple[str, str, Callable[[], object]]]:
        app: "ActivityApp" = self.app
        bell_label = "Bell: turn off" if app.bell_enabled else "Bell: turn on"
        view_label = "View: back to feed" if app.show_dismissed else "View: read archive"
        commands: list[tuple[str, str, Callable[[], object]]] = [
            ("Refresh", "Poll every source for new notifications now", app.action_poll_now),
            (bell_label, "Ring the terminal bell on new notifications", app.action_toggle_bell),
            (view_label, "Toggle the read-archive view", app.action_toggle_dismissed),
        ]
        for key, (fid, label) in FILTERS.items():
            state = app.filter_state[fid]
            commands.append(
                (
                    f"Filter: {label} ({state})",
                    "Cycle this feed filter: off -> on -> ghost",
                    partial(app.action_toggle, key),
                )
            )
        return commands

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class ActivityApp(App):
    TITLE = "activity"
    COMMANDS = App.COMMANDS | {SettingsCommands}

    CSS = """
    #status { dock: top; height: 1; padding: 0 1; background: $panel; color: $text; }
    #feed { height: 1fr; }
    #feed > ListItem { padding: 0 1; height: auto; }
    #feed .row { height: auto; align-vertical: top; }
    #feed .c-time { width: 14; }
    #feed .c-src { width: 7; }
    #feed .c-group { width: 18; }
    #feed .c-author { width: 14; }
    #feed .c-text { width: 1fr; }
    #feed .ghost { text-style: dim; }
    """

    BINDINGS = [
        # Filters (1/2/3) live in the top status bar, so they're hidden from the footer.
        Binding("1", "toggle('1')", "Unread", show=False),
        Binding("2", "toggle('2')", "Favs", show=False),
        Binding("3", "toggle('3')", "Direct/@", show=False),
        Binding("e", "mark_read", "Mark read", show=True),
        Binding("E", "mark_all", "Mark all read", show=True),
        Binding("d", "toggle_dismissed", "Read view", show=True),
        Binding("u", "unmark", "Mark unread", show=True),
        Binding("z", "undo", "Undo", show=True),
        # Vim-style navigation, mirroring the arrow keys; hidden from the footer.
        Binding("j", "feed_cursor(1)", show=False),
        Binding("k", "feed_cursor(-1)", show=False),
        # Refresh and bell are hidden from the footer; reach them via the command palette (ctrl+p).
        Binding("g", "poll_now", "Refresh", show=False),
        Binding("b", "toggle_bell", "Bell", show=False),
        Binding("o", "open_message", "Open", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.sources = build_sources(config)
        self.notifications: dict[tuple[str, str], Notification] = {}
        self.dismissed: dict[tuple[str, str], Notification] = {}
        for record in load_dismissed():
            n = notification_from_record(record)
            self.dismissed[n.key] = n
        self._undo_stack: list[UndoEntry] = []
        # Startup default: what's aimed at you (favorites, direct, mentions) shows
        # normally; the rest of the unread firehose is ghosted for context.
        self.filter_state: dict[str, str] = {
            "all_unread": FILTER_GHOST,
            "favorites": FILTER_ON,
            "direct": FILTER_ON,
        }
        self.bell_enabled = config.bell
        # Counts new "on"-filter arrivals during the current poll, to ring once per poll.
        self._new_unread = 0
        self.show_dismissed = False
        self.visible_order: list[tuple[str, str]] = []
        self._render_sig: tuple | None = None
        self._first_render = True
        self._bootstrapped = False
        self.status_extra = "starting…"
        # Per-source trailing status, e.g. a poll error, keyed by source name.
        self._source_status: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        yield ListView(id="feed")
        yield Footer()

    async def on_mount(self) -> None:
        self.update_status()
        # Run setup in a worker so on_mount returns and the UI paints immediately.
        # Awaiting it here would leave the screen blank until setup finished.
        self.run_worker(self._bootstrap(), exclusive=True)

    # ---- setup ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        """Set the trailing status text and repaint, so progress shows between awaits."""
        self.status_extra = text
        self.update_status()

    async def _bootstrap(self) -> None:
        started: list[Source] = []
        for source in self.sources:
            try:
                await source.start(self._set_status)
                started.append(source)
            except Exception as exc:  # a broken source shouldn't sink the others
                self._source_status[source.name] = f"{source.name} setup failed: {exc}"
        self.sources = started
        if not self.sources:
            self._set_status(self._status_summary() or "no sources available")
            return
        self._set_status("fetching notifications…")
        await self.poll()
        self._bootstrapped = True  # from here on, new arrivals may ring the bell
        self._set_status(self._status_summary())
        self.set_interval(POLL_SECONDS, self.poll)

    def _status_summary(self) -> str:
        """Source-level notes (errors first) for the right-hand end of the status bar."""
        if self._source_status:
            return "   ".join(self._source_status.values())
        return "   ".join(f"{s.name} ok" for s in self.sources)

    # ---- polling -------------------------------------------------------

    async def poll(self) -> None:
        self._new_unread = 0
        for source in self.sources:
            try:
                incoming = await source.poll()
            # Sources fail in service-specific ways (HTTP, IMAP, socket); whatever
            # goes wrong in one, the others still have to render.
            except Exception as exc:
                self._source_status[source.name] = f"{source.name} poll error: {exc}"
                continue
            self._source_status.pop(source.name, None)
            self._merge(incoming)
        self._trim()
        if self._bootstrapped:
            self._set_status(self._status_summary())
        self.refresh_table()
        if self.bell_enabled and self._new_unread:
            self._double_bell()

    def _merge(self, incoming: list[Notification]) -> None:
        for n in incoming:
            if n.key in self.dismissed:
                continue
            known = self.notifications.get(n.key)
            if known is None:
                self.notifications[n.key] = n
                # Ring only for arrivals an "on" filter would show. Ghost stays silent.
                if self._bootstrapped and n.unread and self._visibility(n) == FILTER_ON:
                    self._new_unread += 1
                continue
            # A re-emitted notification carries fresh unread/favorite state.
            known.unread = n.unread
            known.favorite = n.favorite

    def _double_bell(self) -> None:
        """Two quick beeps so a new notification is distinguishable from a single error bell."""
        self.bell()
        # A short gap keeps terminals from collapsing the pair into one beep.
        self.set_timer(0.15, self.bell)

    def _trim(self) -> None:
        if len(self.notifications) > MAX_ROWS:
            keep = sorted(self.notifications.values(), key=lambda n: n.ts)[-MAX_ROWS:]
            self.notifications = {n.key: n for n in keep}
        # Let each source forget what we're no longer holding.
        for source in self.sources:
            held = {
                key[1]
                for key in list(self.notifications) + list(self.dismissed)
                if key[0] == source.name
            }
            source.trim(held)

    # ---- rendering -----------------------------------------------------

    def _matches_filter(self, fid: str, n: Notification) -> bool:
        if fid == "all_unread":
            return n.unread
        if fid == "favorites":
            return n.favorite
        if fid == "direct":
            return n.is_direct or n.is_mention
        return False

    def _visibility(self, n: Notification) -> str | None:
        """Return FILTER_ON, FILTER_GHOST, or None (hidden) under the current filter states.

        A notification shown by any "on" filter is FILTER_ON; if only "ghost" filters
        match it, it's FILTER_GHOST (greyed, silent). Matching no enabled filter hides it.
        """
        if n.key in self.dismissed:
            return None
        matched_ghost = False
        for fid, state in self.filter_state.items():
            if state == FILTER_OFF or not self._matches_filter(fid, n):
                continue
            if state == FILTER_ON:
                return FILTER_ON
            matched_ghost = True
        return FILTER_GHOST if matched_ghost else None

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        text = text or ""
        return text if len(text) <= width else text[: width - 1] + "…"

    def _build_item(self, n: Notification, marker: str, ghosted: bool = False) -> ListItem:
        """A row with fixed time/source/group/author columns and a wrapping text column.

        Ghosted rows (shown only by a "ghost"-state filter) render dimmed throughout.
        """
        clock = time.strftime("%m/%d %H:%M", time.localtime(n.ts))
        time_col = self._truncate(f"{marker} {clock}", COL_TIME_W - 1)
        src_col = self._truncate(n.source, COL_SRC_W - 1)
        group_col = self._truncate(n.group, COL_GROUP_W - 1)
        author_col = self._truncate(n.author, COL_AUTHOR_W - 1)
        group_markup = "dim" if ghosted else "b"
        author_markup = "dim" if ghosted else "cyan"
        # Render user text with markup disabled: Textual's parser opens a tag on
        # any '[', but the escape regex only covers lowercase-led tags, so content
        # like "[ENG-123=>]" slips through escaping and crashes the parser. The
        # ghost dim comes from CSS (.ghost) instead, which doesn't need markup.
        text_classes = "c-text ghost" if ghosted else "c-text"
        if n.text:
            body = Label(self._truncate(n.text, MAX_BODY_CHARS), classes=text_classes, markup=False)
        else:
            body = Label("[dim](no text)[/dim]", classes=text_classes)
        row = Horizontal(
            Label(f"[dim]{escape(time_col)}[/dim]", classes="c-time"),
            Label(f"[dim]{escape(src_col)}[/dim]", classes="c-src"),
            Label(f"[{group_markup}]{escape(group_col)}[/{group_markup}]", classes="c-group"),
            Label(f"[{author_markup}]{escape(author_col)}[/{author_markup}]", classes="c-author"),
            body,
            classes="row",
        )
        return ListItem(row)

    def refresh_table(self) -> None:
        # Each row is (notification, marker, ghosted).
        if self.show_dismissed:
            ordered = sorted(self.dismissed.values(), key=lambda n: n.ts)[-MAX_ROWS:]
            rows = [(n, "✓", False) for n in ordered]
        else:
            visible = [
                (n, state) for n in self.notifications.values() if (state := self._visibility(n))
            ]
            visible.sort(key=lambda pair: pair[0].ts)
            rows = [
                (n, "●" if n.unread else " ", state == FILTER_GHOST)
                for n, state in visible[-MAX_ROWS:]
            ]

        # Skip the DOM rebuild when nothing on screen would change — this stops the
        # feed from flickering on the steady-state polls that fetch nothing new.
        signature = (
            self.show_dismissed,
            tuple(sorted(self.filter_state.items())),
            tuple((n.key, mk, ghosted, n.text) for n, mk, ghosted in rows),
        )
        if signature == self._render_sig:
            return
        self._render_sig = signature

        feed = self.query_one("#feed", ListView)
        prev_index = feed.index
        feed.clear()
        self.visible_order = []
        items: list[ListItem] = []
        for n, marker, ghosted in rows:
            items.append(self._build_item(n, marker, ghosted))
            self.visible_order.append(n.key)
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
                f"u: mark unread   z: undo   d: back to feed   {escape(self.status_extra)}"
            )
            return
        chips = []
        for key, (fid, label) in FILTERS.items():
            state = self.filter_state[fid]
            if state == FILTER_ON:
                chip = f"[reverse] {escape(f'{key} {label}')} [/reverse]"
            elif state == FILTER_GHOST:
                chip = f"[dim italic] {escape(f'{key} {label} (ghost)')} [/dim italic]"
            else:
                chip = f"[dim]{escape(f'{key} {label}')}[/dim]"
            chips.append(chip)
        filters = "  ".join(chips)
        bell = "🔔" if self.bell_enabled else "🔕"
        status.update(
            f"[b]Filters[/b]  {filters}    "
            f"shown: {len(self.visible_order)}   dismissed: {len(self.dismissed)}   "
            f"{bell}   {escape(self.status_extra)}"
        )

    # ---- actions -------------------------------------------------------

    def action_toggle(self, key: str) -> None:
        """Cycle a filter through off -> on -> ghost -> off."""
        fid = FILTERS[key][0]
        self.filter_state[fid] = FILTER_CYCLE[self.filter_state[fid]]
        self.update_status()  # immediate feedback even if visible rows don't change
        self.refresh_table()

    def action_feed_cursor(self, delta: int) -> None:
        """Move the feed selection (j/k), mirroring the arrow keys."""
        feed = self.query_one("#feed", ListView)
        if delta > 0:
            feed.action_cursor_down()
        else:
            feed.action_cursor_up()

    def _selected_key(self) -> tuple[str, str] | None:
        feed = self.query_one("#feed", ListView)
        index = feed.index
        if index is None or not (0 <= index < len(self.visible_order)):
            return None
        return self.visible_order[index]

    def _save_dismissed(self) -> None:
        save_dismissed([record_from_notification(n) for n in self.dismissed.values()])

    def action_mark_read(self) -> None:
        """Dismiss the selected notification from the feed (local triage only)."""
        if self.show_dismissed:
            self.notify("Already in the read view — press u to mark unread.", severity="warning")
            return
        key = self._selected_key()
        n = self.notifications.get(key) if key else None
        if not n:
            self.notify("No row selected.", severity="warning")
            return
        self.dismissed[key] = n
        self._push_undo(UndoEntry(f"read {n.group}", dismissed=[key], restored={}))
        self._save_dismissed()
        self.notify(f"Marked {n.group} notification read.")
        self.refresh_table()

    def action_mark_all(self) -> None:
        """Dismiss everything visible, or restore it all when in the read view."""
        if not self.visible_order:
            self.notify("Nothing here.", severity="warning")
            return
        count = len(self.visible_order)
        if self.show_dismissed:
            restored = {
                key: self.dismissed[key] for key in self.visible_order if key in self.dismissed
            }
            for key in list(self.visible_order):
                self._restore(key)
            self._push_undo(UndoEntry(f"unread {count}", dismissed=[], restored=restored))
            self._save_dismissed()
            self.notify(f"Marked {count} notifications unread.")
        else:
            added: list[tuple[str, str]] = []
            for key in self.visible_order:
                n = self.notifications.get(key)
                if n:
                    self.dismissed[key] = n
                    added.append(key)
            self._push_undo(UndoEntry(f"read {count}", dismissed=added, restored={}))
            self._save_dismissed()
            self.notify(f"Marked {count} notifications read.")
        self.refresh_table()

    def action_toggle_dismissed(self) -> None:
        """Switch between the live feed and the archive of read (dismissed) notifications."""
        self.show_dismissed = not self.show_dismissed
        self._first_render = True  # land on the newest row of whichever view we entered
        self.refresh_table()

    def action_unmark(self) -> None:
        """Restore the selected notification from the read view back into the live feed."""
        if not self.show_dismissed:
            self.notify("Open the read view (d) to mark notifications unread.", severity="warning")
            return
        key = self._selected_key()
        if not key or key not in self.dismissed:
            self.notify("No row selected.", severity="warning")
            return
        n = self.dismissed[key]
        self._restore(key)
        self._push_undo(UndoEntry(f"unread {n.group}", dismissed=[], restored={key: n}))
        self._save_dismissed()
        self.notify("Marked unread.")
        self.refresh_table()

    def _restore(self, key: tuple[str, str]) -> None:
        """Remove a notification from the dismissed set and put it back in the live feed."""
        n = self.dismissed.pop(key, None)
        if n and key not in self.notifications:
            self.notifications[key] = n

    def _push_undo(self, entry: UndoEntry) -> None:
        self._undo_stack.append(entry)
        del self._undo_stack[:-UNDO_LIMIT]

    def action_undo(self) -> None:
        """Reverse the most recent triage action (mark read / mark unread)."""
        if not self._undo_stack:
            self.notify("Nothing to undo.", severity="warning")
            return
        entry = self._undo_stack.pop()
        for key in entry.dismissed:
            self._restore(key)
        for key, n in entry.restored.items():
            self.dismissed[key] = n
        self._save_dismissed()
        self.notify(f"Undid: {entry.label}.")
        self.refresh_table()

    async def action_poll_now(self) -> None:
        await self.poll()

    def action_toggle_bell(self) -> None:
        """Toggle the terminal bell on new notifications, persisting the choice to config."""
        self.bell_enabled = not self.bell_enabled
        self.config.bell = self.bell_enabled
        save(self.config)
        self.notify(f"Bell {'on' if self.bell_enabled else 'off'}.")
        self.update_status()

    def action_open_message(self) -> None:
        """Open the selected notification in its native client."""
        key = self._selected_key()
        source = self.dismissed if self.show_dismissed else self.notifications
        n = source.get(key) if key else None
        if not n:
            self.notify("No row selected.", severity="warning")
            return
        if not n.link:
            self.notify("No link saved for this row.", severity="warning")
            return
        webbrowser.open(n.link)
        self.notify(f"Opening in {n.source}…")

    async def action_quit(self) -> None:
        for source in self.sources:
            await source.close()
        self.exit()
