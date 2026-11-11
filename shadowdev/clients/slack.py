from urllib.parse import urlencode

from .rest import HTTPError, request_json

API = "https://slack.com/api"
# What the developer's user token needs: post as them, and read the review channel (public or private).
SCOPES = ("chat:write", "channels:history", "groups:history")


def escape(text: str) -> str:
    """Text from outside (an issue's summary) as plain text in a Slack message."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Slack:
    """Slack Web API, acting as one developer with their user token (xoxp-…)."""

    def __init__(self, token: str):
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    def _call(self, method: str, body: dict | None = None, **query) -> dict:
        """Slack answers 200 also for errors, with ok: false; those are raised as HTTPError 400."""
        url = f"{API}/{method}" + (f"?{urlencode(query)}" if query else "")
        data = request_json("POST" if body is not None else "GET", url, self._headers, body, "Slack") or {}
        if not data.get("ok"):
            raise HTTPError(f"Slack {method}: {data.get('error') or 'failed'}", 400)
        return data

    def whoami(self) -> dict:
        """The token's user: user_id, user (name), team."""
        return self._call("auth.test")

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        """Post a message (in a thread: a reply); returns its timestamp, which identifies it."""
        body = {"channel": channel, "text": text, "unfurl_links": False, **({"thread_ts": thread_ts} if thread_ts else {})}
        return self._call("chat.postMessage", body)["ts"]

    def history(self, channel: str, limit: int) -> list[dict]:
        """The channel's last `limit` messages (not the replies in threads), oldest first. A message
        that starts a thread carries `latest_reply`, the timestamp of its newest reply."""
        messages = self._call("conversations.history", channel=channel, limit=limit).get("messages") or []
        return sorted(messages, key=lambda m: float(m.get("ts") or 0))

    def replies(self, channel: str, thread_ts: str, after: str) -> list[dict]:
        """The replies in a thread after the timestamp `after`, oldest first."""
        messages = self._call("conversations.replies", channel=channel, ts=thread_ts, oldest=after, limit=200).get("messages") or []
        return [m for m in messages if m.get("ts") != thread_ts and float(m.get("ts") or 0) > float(after)]
