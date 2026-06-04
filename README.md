# slacktivity

A live terminal feed of recent Slack messages, with mark-as-read. Authenticates
as your existing Slack browser session — no app install, no workspace-admin
approval.

## Install

```bash
cd ~/code/tools/slacktivity
uv sync
```

## Auth

```bash
uv run slacktivity auth
```

This walks you through grabbing two credentials from your logged-in Slack web session:

- **`xoxc-` token** — DevTools → Console:
  ```js
  JSON.parse(localStorage.localConfig_v2).teams[
    Object.keys(JSON.parse(localStorage.localConfig_v2).teams)[0]
  ].token
  ```
- **`d` cookie (`xoxd-`)** — DevTools → Application → Cookies → `https://app.slack.com`, the cookie named `d`.

Stored in `~/.config/slacktivity/config.json` (mode 600). You can also set
`SLACKTIVITY_TOKEN` / `SLACKTIVITY_COOKIE` in the environment instead.

> Note: this is a session token, so it expires when you log out of the browser
> session, and using it is a gray area under Slack's ToS. Fine for personal use;
> don't share it.

## Run

```bash
uv run slacktivity
```

### Filters (toggle, union)

| Key | Filter | Shows |
|-----|--------|-------|
| `1` | Unread (all) | every unread message across all channels/DMs |
| `2` | Favorites | messages in your Slack-starred channels (plus any `watch` entries in `config.json`) |
| `3` | DMs + mentions | direct messages and messages that @-mention you |

A message is shown if it matches **any** active filter. `1` starts on.

Favorites are read from Slack's `client.userBoot` payload (your starred channels)
at startup. The status bar shows how many were found — if it says `0 favorites`
but you have starred channels, the `userBoot` field name may differ on your
workspace; file it and the lookup can be pointed at the right field.

### Other keys

| Key | Action |
|-----|--------|
| `r` | mark the selected message read (dismiss it from the feed) |
| `a` | mark every visible message read — or, in the read view, mark them all unread |
| `d` | toggle the **read view** (archive of messages you've marked read) |
| `u` | in the read view, mark the selected message unread (restore it to the feed) |
| `z` | undo the last mark-read / mark-unread action |
| `g` | poll now |
| `b` | toggle the terminal bell on new messages (persists to `config.json`) |
| `ctrl+p` | open the command palette (refresh, settings, filters, views) |
| `q` | quit |
| ↑/↓ | move selection |

The command palette (`ctrl+p`) lists refresh and the toggleable settings — the
bell, each feed filter, and the read-archive view — so you can search for them
instead of remembering the key.

Marking read is a local triage layer — it dismisses individual messages from the
feed so that anything still showing needs your attention. It does **not** touch
Slack's own unread badges. Dismissed messages (with a saved preview) persist to
`~/.config/slacktivity/dismissed.json` and are pruned after 30 days; `d` opens
the archive and `u` restores any of them.

## Watched channels

Edit `~/.config/slacktivity/config.json`:

```json
{
  "token": "xoxc-...",
  "cookie": "xoxd-...",
  "watch": ["#eng-oncall", "#general", "@jane"],
  "bell": true
}
```

Entries match channel names (with or without `#`/`@`) or raw channel IDs, and
are merged into the **Favorites** filter alongside your Slack-starred channels.

Set `"bell": true` (or press `b` in the app) to ring the terminal bell when a
new unread message arrives. Your own messages and the startup backfill don't ring.

## How it works

- Polls `client.counts` every 5s for unread state and latest-message timestamps.
- Fetches deltas with `conversations.history` (`oldest=` cursor per channel).
- Resolves user/channel mentions and caches names.
- All calls go to `https://slack.com/api/` with the `xoxc` token as a form field
  and the `d` cookie as a header — the same path the web app uses.
