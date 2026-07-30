"""Gmail notification source over IMAP, authed with an app password.

Gmail's IMAP gateway needs no Cloud project and no OAuth client: with 2FA on
your account you mint an app password at myaccount.google.com and that is the
whole credential. The mailbox is opened read-only and messages are fetched with
``BODY.PEEK``, so nothing here ever marks your mail as read.

``imaplib`` is blocking, so every call runs in a worker thread behind a lock.
"""

import asyncio
import email.utils
import html
import imaplib
import re
import time
from collections.abc import Callable
from email.header import decode_header
from email.header import make_header
from email.message import Message
from email.parser import BytesParser
from urllib.parse import quote

from .base import GMAIL
from .base import Notification

IMAP_HOST = "imap.gmail.com"
DEFAULT_MAILBOX = "INBOX"
# How many recent messages to show on the first poll so the feed isn't empty.
BACKFILL = 25
# An inbox can hold thousands of unread messages; only the newest are worth a
# header fetch, since the feed caps what it renders anyway.
UNREAD_LIMIT = 200
# Fetch the front of the raw message: headers plus enough MIME to reach the
# text part for a body preview. Attachments come later in the message, so 64KB
# covers the text of nearly all mail without ever downloading a photo.
FETCH_BYTES = 64 * 1024
FETCH_SPEC = f"(UID FLAGS BODY.PEEK[]<0.{FETCH_BYTES}>)"
# How much decoded body text to keep next to the subject.
PREVIEW_CHARS = 200

FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"

RE_UID = re.compile(rb"UID (\d+)")
RE_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
RE_HTML_JUNK = re.compile(r"(?is)<(style|script|head)\b.*?</\1>")
RE_HTML_TAG = re.compile(r"(?s)<[^>]+>")


def parse_fetch(response: list) -> list[tuple[int, set[str], bytes]]:
    """Pull (uid, flags, raw message bytes) out of an IMAP FETCH response.

    ``imaplib`` returns a flat list mixing tuples of (prefix, literal) with bare
    closing-paren bytes, so the parts of one message are spread across items.
    """
    out: list[tuple[int, set[str], bytes]] = []
    for item in response:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        prefix, literal = item[0], item[1]
        uid_match = RE_UID.search(prefix)
        if not uid_match:
            continue
        flags_match = RE_FLAGS.search(prefix)
        raw_flags = flags_match.group(1).decode(errors="replace") if flags_match else ""
        out.append((int(uid_match.group(1)), set(raw_flags.split()), literal))
    return out


def decode_field(value: str | None) -> str:
    """Decode an RFC 2047 encoded header (``=?utf-8?B?...?=``) to plain text.

    Long headers arrive folded across lines, so whitespace runs collapse to one space.
    """
    if not value:
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        decoded = value
    return " ".join(decoded.split())


def sender_name(from_header: str | None) -> str:
    """Prefer the display name, falling back to the local part of the address."""
    name, address = email.utils.parseaddr(from_header or "")
    decoded = decode_field(name)
    if decoded:
        return decoded
    return address.split("@")[0] if address else "unknown"


def header_timestamp(date_header: str | None) -> float:
    """Parse a Date header, falling back to now for mail with a broken one."""
    if date_header:
        try:
            return email.utils.parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError):
            pass
    return time.time()


def message_link(message_id: str) -> str:
    """A Gmail web search that resolves to the one message."""
    stripped = message_id.strip().strip("<>")
    return f"https://mail.google.com/mail/u/0/#search/{quote(f'rfc822msgid:{stripped}', safe='')}"


def _part_text(part: Message) -> str:
    """Decode one MIME part to text, tolerating truncation from the partial fetch."""
    payload = part.get_payload(decode=True)
    if payload is None:
        # A base64/qp part cut mid-stream can fail to decode; take the raw text.
        raw = part.get_payload()
        payload = raw.encode() if isinstance(raw, str) else b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def strip_html(text: str) -> str:
    text = RE_HTML_JUNK.sub(" ", text)
    text = RE_HTML_TAG.sub(" ", text)
    return html.unescape(text)


def body_preview(msg: Message, limit: int = PREVIEW_CHARS) -> str:
    """The first line or two of the message body: text/plain if present, else de-tagged HTML."""
    plain = html_body = None
    for part in msg.walk():
        if part.get_content_maintype() != "text" or "attachment" in part.get(
            "Content-Disposition", ""
        ):
            continue
        subtype = part.get_content_subtype()
        if subtype == "plain" and plain is None:
            plain = _part_text(part)
        elif subtype == "html" and html_body is None:
            html_body = _part_text(part)
    text = plain if plain and plain.strip() else strip_html(html_body or "")
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


def is_bulk(headers) -> bool:
    """True for mailing-list and bulk mail, which shouldn't count as addressed to you."""
    return bool(headers.get("List-Id") or headers.get("List-Unsubscribe"))


def recipients(headers) -> list[str]:
    raw = ", ".join(filter(None, [headers.get("To"), headers.get("Cc")]))
    return [addr.lower() for _, addr in email.utils.getaddresses([raw]) if addr]


class GmailSource:
    name = GMAIL

    def __init__(
        self,
        user: str,
        app_password: str,
        mailbox: str = DEFAULT_MAILBOX,
        watch: list[str] | None = None,
    ) -> None:
        self._user = user
        self._password = app_password
        self._mailbox = mailbox
        # Addresses or "@domain" suffixes whose mail is always a favorite.
        self._watch = [w.lower() for w in (watch or [])]
        self._conn: imaplib.IMAP4_SSL | None = None
        self._lock = asyncio.Lock()
        self._seen: dict[int, Notification] = {}
        # Every uid ever fetched. Kept separate from _seen so that trimming old
        # rows out of memory doesn't make the next poll fetch them all over again.
        self._known_uids: set[int] = set()
        self._backfilled = False

    # ---- connection -----------------------------------------------------

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(self._user, self._password)
        return conn

    def _select(self) -> imaplib.IMAP4_SSL:
        """Return a connection with the mailbox selected read-only, reconnecting if needed.

        Gmail drops idle IMAP connections, so a failed SELECT means reconnect
        rather than fail the poll.
        """
        if self._conn is not None:
            try:
                self._conn.select(self._mailbox, readonly=True)
                return self._conn
            except (imaplib.IMAP4.error, OSError):
                self._conn = None
        conn = self._connect()
        conn.select(self._mailbox, readonly=True)
        self._conn = conn
        return conn

    async def start(self, progress: Callable[[str], None]) -> None:
        progress("connecting to Gmail…")
        async with self._lock:
            await asyncio.to_thread(self._select)

    async def close(self) -> None:
        async with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                await asyncio.to_thread(self._logout, conn)

    @staticmethod
    def _logout(conn: imaplib.IMAP4_SSL) -> None:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    # ---- polling --------------------------------------------------------

    async def poll(self) -> list[Notification]:
        async with self._lock:
            fetched, flags_by_uid = await asyncio.to_thread(self._fetch_sync)
        for uid, flags, raw_message in fetched:
            self._ingest(uid, flags, raw_message)
        # Mail read elsewhere should stop showing as unread here.
        for uid, flags in flags_by_uid.items():
            if FLAG_SEEN in flags and uid in self._seen:
                self._seen[uid].unread = False
        self._backfilled = True
        return list(self._seen.values())

    def _fetch_sync(self) -> tuple[list[tuple[int, set[str], bytes]], dict[int, set[str]]]:
        """Blocking half of a poll: headers for new mail, plus flags for known unread mail."""
        conn = self._select()
        wanted = set(self._search(conn, "UNSEEN")[-UNREAD_LIMIT:])
        if not self._backfilled:
            wanted |= set(self._search(conn, "ALL")[-BACKFILL:])

        new_uids = sorted(uid for uid in wanted if uid not in self._known_uids)
        self._known_uids |= set(new_uids)
        fetched: list[tuple[int, set[str], bytes]] = []
        if new_uids:
            _, response = conn.uid("FETCH", ",".join(str(u) for u in new_uids), FETCH_SPEC)
            fetched = parse_fetch(response)

        unread_uids = sorted(uid for uid, n in self._seen.items() if n.unread)
        flags_by_uid: dict[int, set[str]] = {}
        if unread_uids:
            _, response = conn.uid("FETCH", ",".join(str(u) for u in unread_uids), "(UID FLAGS)")
            # A FLAGS-only fetch has no literal, so parse_fetch's tuple shape doesn't apply.
            for line in response:
                prefix = line[0] if isinstance(line, tuple) else line
                uid_match = RE_UID.search(prefix or b"")
                flags_match = RE_FLAGS.search(prefix or b"")
                if uid_match and flags_match:
                    raw = flags_match.group(1).decode(errors="replace")
                    flags_by_uid[int(uid_match.group(1))] = set(raw.split())
        return fetched, flags_by_uid

    @staticmethod
    def _search(conn: imaplib.IMAP4_SSL, criterion: str) -> list[int]:
        _, data = conn.uid("SEARCH", None, criterion)
        if not data or not data[0]:
            return []
        return [int(part) for part in data[0].split()]

    def _ingest(self, uid: int, flags: set[str], raw_message: bytes) -> None:
        msg = BytesParser().parsebytes(raw_message)
        to_me = self._user.lower() in recipients(msg)
        bulk = is_bulk(msg)
        message_id = msg.get("Message-ID") or f"uid-{uid}"
        sender = email.utils.parseaddr(msg.get("From") or "")[1].lower()
        subject = decode_field(msg.get("Subject")) or "(no subject)"
        preview = body_preview(msg)
        self._seen[uid] = Notification(
            source=GMAIL,
            id=message_id.strip().strip("<>"),
            group=self._mailbox.lower(),
            author=sender_name(msg.get("From")),
            text=f"{subject}  ·  {preview}" if preview else subject,
            ts=header_timestamp(msg.get("Date")),
            unread=FLAG_SEEN not in flags,
            is_direct=to_me and not bulk,
            # Mail with you as the only recipient is the closest thing to being named.
            is_mention=to_me and not bulk and len(recipients(msg)) == 1,
            favorite=FLAG_FLAGGED in flags or self._is_watched(sender),
            link=message_link(message_id),
        )

    def _is_watched(self, sender: str) -> bool:
        return any(
            sender.endswith(entry) if entry.startswith("@") else sender == entry
            for entry in self._watch
        )

    def trim(self, keep_ids: set[str]) -> None:
        """Drop cached notifications the app is no longer holding, to bound memory."""
        self._seen = {uid: n for uid, n in self._seen.items() if n.id in keep_ids}
