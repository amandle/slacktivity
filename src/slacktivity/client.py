"""Thin async wrapper over Slack's web API using a browser-session token.

Auth is the ``xoxc-...`` token sent as a form field plus the ``d`` cookie. This
is the same credential pair the Slack web app uses, so it needs no app install
and no workspace-admin approval. It is tied to your browser session and will
break when that session ends.
"""

import httpx

BASE_URL = "https://slack.com/api/"
# Slack rejects requests from session tokens without a browser-like UA.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class SlackError(Exception):
    """Raised when Slack returns ``{"ok": false}``."""


class SlackSession:
    def __init__(self, token: str, cookie: str) -> None:
        self._token = token
        cookie_header = cookie if cookie.startswith("d=") else f"d={cookie}"
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Cookie": cookie_header},
            timeout=30.0,
        )
        self._user_names: dict[str, str] = {}

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
            except SlackError:
                self._user_names[user_id] = user_id
        return self._user_names[user_id]

    async def close(self) -> None:
        await self._http.aclose()
