"""slacktivity — a live terminal feed of recent Slack messages."""

import sys

from . import config as config_mod

AUTH_INSTRUCTIONS = """\
slacktivity authenticates as your existing Slack browser session.
No app install and no workspace-admin approval are needed.

1. Open Slack in your browser and log in (https://app.slack.com).
2. Open DevTools -> Console and paste this to print your token:

   JSON.parse(localStorage.localConfig_v2).teams[
     Object.keys(JSON.parse(localStorage.localConfig_v2).teams)[0]
   ].token

   It starts with "xoxc-".

3. DevTools -> Application -> Cookies -> https://app.slack.com.
   Copy the value of the cookie named "d" (starts with "xoxd-").

Paste both below. They are stored in ~/.config/slacktivity/config.json (mode 600).
"""


def _auth() -> None:
    print(AUTH_INSTRUCTIONS)
    token = input("xoxc token: ").strip()
    cookie = input("d cookie (xoxd-...): ").strip()
    if not token or not cookie:
        raise SystemExit("Both token and cookie are required.")
    path = config_mod.save(token, cookie)
    print(f"\nSaved to {path}. Run `slacktivity` to start.")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "auth":
        _auth()
        return
    cfg = config_mod.load()
    from .app import SlacktivityApp

    SlacktivityApp(cfg).run()
