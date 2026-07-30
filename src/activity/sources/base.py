"""The generic notification model every source produces, and the source contract.

A source owns everything specific to its service: auth, polling, name lookups,
what counts as unread, and how to build a deep link. The app only ever sees
:class:`Notification` values.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from typing import runtime_checkable

# Source ids, used as the first half of a notification key and shown in the feed.
SLACK = "slack"
GMAIL = "gmail"


@dataclass
class Notification:
    source: str
    # Unique within the source. Slack uses "<channel>:<ts>", Gmail the RFC822 message id.
    id: str
    # The bucket this arrived in: a Slack channel/DM name, or a Gmail mailbox.
    group: str
    author: str
    text: str
    ts: float
    unread: bool
    # Addressed to you specifically: a DM, or mail sent to you rather than a list.
    is_direct: bool
    # Names you explicitly: an @-mention, or mail where you're the sole recipient.
    is_mention: bool
    # Starred/favorited on the service side, or matched by the config watch list.
    favorite: bool
    # URL or app scheme that opens this notification in its native client.
    link: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.id)


@runtime_checkable
class Source(Protocol):
    """A pollable notification service.

    ``start`` runs once and may be slow (auth, channel lists, backfill); it
    reports human-readable progress through the callback it's given. ``poll`` is
    called on the feed's interval and returns whatever it currently knows about,
    both new and previously seen — the app dedupes by
    :attr:`Notification.key`, so a source may re-emit a notification to update
    its unread state.
    """

    name: str

    async def start(self, progress: Callable[[str], None]) -> None: ...

    async def poll(self) -> list[Notification]: ...

    def trim(self, keep_ids: set[str]) -> None:
        """Forget cached notifications whose ids the app no longer holds."""
        ...

    async def close(self) -> None: ...


def record_from_notification(n: Notification) -> dict:
    """Flatten a notification for the dismissed-message store."""
    return {
        "source": n.source,
        "id": n.id,
        "group": n.group,
        "author": n.author,
        "text": n.text,
        "ts": n.ts,
        "is_direct": n.is_direct,
        "is_mention": n.is_mention,
        "link": n.link,
    }


def notification_from_record(record: dict) -> Notification:
    """Rebuild a notification from the dismissed-message store.

    Stored records are display-only: they're already read, never favorited, and
    a record written by an older version may have no saved preview.
    """
    return Notification(
        source=record.get("source", SLACK),
        id=record["id"],
        group=record.get("group") or "",
        author=record.get("author", ""),
        text=record.get("text") or "(no saved preview)",
        ts=float(record["ts"]),
        unread=False,
        is_direct=bool(record.get("is_direct")),
        is_mention=bool(record.get("is_mention")),
        favorite=False,
        link=record.get("link", ""),
    )
