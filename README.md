# activity

A live terminal feed of your notifications — Slack messages and Gmail mail in
one list — with local mark-as-read triage. Each service is a *source*; a source
you haven't configured is simply not polled.

| Source | Auth | Reads |
|--------|------|-------|
| Slack | your browser session token (`xoxc-` + `d` cookie) | unread messages, plus recent history in favorited channels |
| Gmail | an app password over IMAP | unread mail, plus the 25 most recent messages on startup |

## Install

```bash
cd ~/code/tools/slacktivity
uv sync
```

## Auth

```bash
uv run activity auth          # walk both sources
uv run activity auth slack    # or just one
uv run activity auth gmail
```

Everything is stored in `~/.config/activity/config.json` (mode 600). Leaving a
prompt blank skips that source.

### Slack

Two credentials from your logged-in Slack web session. No app install, no
workspace-admin approval.

- **`xoxc-` token** — DevTools → Console:
  ```js
  JSON.parse(localStorage.localConfig_v2).teams[
    Object.keys(JSON.parse(localStorage.localConfig_v2).teams)[0]
  ].token
  ```
- **`d` cookie (`xoxd-`)** — DevTools → Application → Cookies → `https://app.slack.com`, the cookie named `d`.

You can set `ACTIVITY_SLACK_TOKEN` / `ACTIVITY_SLACK_COOKIE` in the environment instead.

> Note: this is a session token, so it expires when you log out of the browser
> session, and using it is a gray area under Slack's ToS. Fine for personal use;
> don't share it.

### Gmail

Gmail's IMAP gateway needs no Cloud project and no OAuth client. Your account
needs 2-step verification on.

1. Enable IMAP: Gmail → Settings → Forwarding and POP/IMAP → **Enable IMAP**.
2. Create an app password at <https://myaccount.google.com/apppasswords> (pick
   "Mail"). It's 16 letters; spaces don't matter.

The mailbox is opened read-only and messages are fetched with `BODY.PEEK`, so
activity never marks your mail as read. A row's text is the subject line
followed by the first couple of lines of the body.

## Run

```bash
uv run activity
```

Each row is `time · source · group · author · text`, where *group* is the Slack
channel/DM or the Gmail mailbox.

### Filters (cycle, union)

| Key | Filter | Shows |
|-----|--------|-------|
| `1` | Unread (all) | everything unread, across every source |
| `2` | Favorites | Slack-starred channels and starred mail, plus anything in a `watch` list |
| `3` | Direct + mentions | Slack DMs and @-mentions; mail addressed to you that isn't from a list |

Each key cycles its filter through **off → on → ghost → off**:

- **on** — matching rows show normally and can ring the bell.
- **ghost** — matching rows show greyed out and never ring the bell.
- **off** — matching rows are hidden.

A row is shown if **any** filter (on or ghost) matches it, and it's greyed only
when no "on" filter matches. On startup `2` and `3` are on and `1` is ghost, so
favorites and direct/mention rows show normally with the rest of the unread
firehose greyed out behind them.

Slack favorites come from the `client.userBoot` payload (your starred channels)
at startup. Gmail favorites are starred mail (IMAP `\Flagged`).

### Other keys

| Key | Action |
|-----|--------|
| `e` | mark the selected row read (dismiss it from the feed) |
| `E` | mark every visible row read — or, in the read view, mark them all unread |
| `d` | toggle the **read view** (archive of rows you've marked read) |
| `u` | in the read view, mark the selected row unread (restore it to the feed) |
| `z` | undo the last mark-read / mark-unread action |
| `o` | open the selected row in its native client (Slack desktop, or Gmail in the browser) |
| `g` | poll now |
| `b` | toggle the terminal bell on new notifications (persists to config) |
| `ctrl+p` | open the command palette (refresh, settings, filters, views) |
| `q` | quit |
| ↑/↓, `j`/`k` | move selection |

Marking read is a local triage layer — it dismisses individual rows so that
anything still showing needs your attention. It does **not** touch Slack's
unread badges or Gmail's. Dismissed rows (with a saved preview) persist to
`~/.config/activity/dismissed.json` and are pruned after 30 days.

## Config

`~/.config/activity/config.json`:

```json
{
  "bell": true,
  "slack": {
    "token": "xoxc-...",
    "cookie": "xoxd-...",
    "watch": ["#eng-oncall", "#general", "@jane"]
  },
  "gmail": {
    "user": "you@gmail.com",
    "app_password": "...",
    "mailbox": "INBOX",
    "watch": ["boss@work.com", "@stripe.com"]
  }
}
```

`watch` feeds the **Favorites** filter. Slack entries match channel names (with
or without `#`/`@`) or raw channel IDs. Gmail entries match a sender address
exactly, or any sender in a domain when written as `@domain`.

Set `"bell": true` (or press `b`) to ring the terminal bell on new unread
notifications. Your own Slack messages and the startup backfill don't ring.

A config written by the older `slacktivity` version is migrated automatically on
first run, dismissed history included.

## How it works

Sources implement one protocol (`start`, `poll`, `trim`, `close`) and return
generic `Notification` records, so the TUI knows nothing about either service.

- **Slack** — polls `client.counts` every 5s for unread state, fetches deltas
  with `conversations.history` (`oldest=` cursor per channel), and resolves
  user/channel mentions with a name cache. All calls go to
  `https://slack.com/api/` with the `xoxc` token as a form field and the `d`
  cookie as a header, the same path the web app uses.
- **Gmail** — holds one IMAP connection open (reconnecting when Gmail drops it),
  runs `UID SEARCH UNSEEN` each poll, and `BODY.PEEK`-fetches the first 64KB
  (headers plus the text part, never the attachments) for uids
  it hasn't seen. `imaplib` is blocking, so every call runs in a worker thread.
