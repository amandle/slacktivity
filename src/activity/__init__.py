"""activity — a live terminal feed of notifications from Slack, Gmail, and friends."""

import sys

from . import config as config_mod

USAGE = """\
usage: activity [auth [slack|gmail]]

  activity              run the feed
  activity auth         set up every source in turn
  activity auth slack   set up Slack only
  activity auth gmail   set up Gmail only
"""

SLACK_INSTRUCTIONS = """\
--- Slack ---
activity authenticates as your existing Slack browser session.
No app install and no workspace-admin approval are needed.

1. Open Slack in your browser and log in (https://app.slack.com).
2. Open DevTools -> Console and paste this to print your token:

   JSON.parse(localStorage.localConfig_v2).teams[
     Object.keys(JSON.parse(localStorage.localConfig_v2).teams)[0]
   ].token

   It starts with "xoxc-".

3. DevTools -> Application -> Cookies -> https://app.slack.com.
   Copy the value of the cookie named "d" (starts with "xoxd-").

Leave either blank to skip Slack.
"""

GMAIL_INSTRUCTIONS = """\
--- Gmail ---
activity reads Gmail over IMAP with an app password, so there's no Cloud
project and no OAuth client to create. Your account needs 2-step verification.

1. Enable IMAP: Gmail -> Settings -> Forwarding and POP/IMAP -> Enable IMAP.
2. Create an app password at https://myaccount.google.com/apppasswords
   (pick "Mail"). It's 16 letters; spaces in it don't matter.

The mailbox is opened read-only, so activity never marks your mail as read.
Leave either blank to skip Gmail.
"""


def _auth_slack() -> bool:
    print(SLACK_INSTRUCTIONS)
    token = input("xoxc token: ").strip()
    cookie = input("d cookie (xoxd-...): ").strip()
    if not token or not cookie:
        print("Skipping Slack.\n")
        return False
    config_mod.save_section("slack", {"token": token, "cookie": cookie, "watch": []})
    print("Slack saved.\n")
    return True


def _auth_gmail() -> bool:
    print(GMAIL_INSTRUCTIONS)
    user = input("gmail address: ").strip()
    # Google displays app passwords in groups separated by non-breaking spaces,
    # so a paste can carry \xa0 as well as regular spaces.
    password = "".join(input("app password: ").split())
    if not user or not password:
        print("Skipping Gmail.\n")
        return False
    config_mod.save_section(
        "gmail", {"user": user, "app_password": password, "mailbox": "INBOX", "watch": []}
    )
    print("Gmail saved.\n")
    return True


AUTH_STEPS = {"slack": _auth_slack, "gmail": _auth_gmail}


def _auth(which: str | None) -> None:
    if which and which not in AUTH_STEPS:
        raise SystemExit(USAGE)
    steps = [AUTH_STEPS[which]] if which else list(AUTH_STEPS.values())
    saved = [step() for step in steps]
    if any(saved):
        print(f"Saved to {config_mod.CONFIG_PATH}. Run `activity` to start.")
    else:
        print("Nothing saved.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "auth":
        _auth(args[1] if len(args) > 1 else None)
        return
    if args:
        raise SystemExit(USAGE)
    cfg = config_mod.load()
    # Imported here so `activity auth` doesn't pay Textual's import cost.
    from .app import ActivityApp

    ActivityApp(cfg).run()
