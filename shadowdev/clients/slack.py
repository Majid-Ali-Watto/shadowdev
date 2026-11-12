import re
from urllib.parse import urlencode

from .rest import HTTPError, request_json

API = "https://slack.com/api"
# What the developer's user token needs: post and react as them, and read the review channel (public or private).
SCOPES = ("chat:write", "reactions:write", "channels:history", "groups:history")


def escape(text: str) -> str:
    """Text from outside (an issue's summary) as plain text in a Slack message."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url: str, text: str = "") -> str:
    """A clickable link in a Slack message, showing `text` (or the URL itself). A URL that would
    break Slack's link syntax is shown as plain text instead."""
    if not url.startswith("https://") or any(c in url for c in "<>| \n"):
        return escape(text or url)
    return f"<{url}|{escape(text)}>" if text else f"<{url}>"


def issue_link(site_url: str, key: str) -> str:
    """An issue key that opens the issue on its Jira site."""
    return link(f"{site_url.rstrip('/')}/browse/{key}", key)


def link_issues(text: str, site_url: str, keys) -> str:
    """Slack text (already escaped) with each of these issue keys made a link to the issue. Only
    whole keys: MAR-1 is not linked inside MAR-12."""
    for key in sorted(set(keys), key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z0-9_|/-]){re.escape(key)}(?![0-9>])", lambda _, k=key: issue_link(site_url, k), text)
    return text


class Slack:
    """Slack Web API, acting as one developer with their user token (xoxp-…)."""

    def __init__(self, token: str):
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}

    def _call(self, method: str, body: dict | None = None, fine: tuple[str, ...] = (), **query) -> dict:
        """Slack answers 200 also for errors, with ok: false; those are raised as HTTPError 400,
        except the errors in `fine`, which mean what was asked is already so."""
        url = f"{API}/{method}" + (f"?{urlencode(query)}" if query else "")
        data = request_json("POST" if body is not None else "GET", url, self._headers, body, "Slack") or {}
        if not data.get("ok") and data.get("error") not in fine:
            raise HTTPError(f"Slack {method}: {data.get('error') or 'failed'}", 400)
        return data

    def whoami(self) -> dict:
        """The token's user: user_id, user (name), team."""
        return self._call("auth.test")

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        """Post a message (in a thread: a reply); returns its timestamp, which identifies it."""
        body = {"channel": channel, "text": text, "unfurl_links": False, **({"thread_ts": thread_ts} if thread_ts else {})}
        return self._call("chat.postMessage", body)["ts"]

    def react(self, channel: str, ts: str, emoji: str, on: bool = True) -> None:
        """Add (or, `on` False, remove) the emoji reaction, by name ("eyes"), on the message `ts`."""
        if on:
            self._call("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji}, fine=("already_reacted",))
        else:
            self._call("reactions.remove", {"channel": channel, "timestamp": ts, "name": emoji}, fine=("no_reaction",))

    def history(self, channel: str, limit: int) -> list[dict]:
        """The channel's last `limit` messages (not the replies in threads), oldest first. A message
        that starts a thread carries `latest_reply`, the timestamp of its newest reply."""
        messages = self._call("conversations.history", channel=channel, limit=limit).get("messages") or []
        return sorted(messages, key=lambda m: float(m.get("ts") or 0))

    def message(self, channel: str, thread_ts: str, ts: str) -> dict | None:
        """One message, with its `reactions` [{name, users}]: `thread_ts` is its thread, or the
        message itself when it is not a reply."""
        messages = self._call("conversations.replies", channel=channel, ts=thread_ts, oldest=ts, latest=ts,
                              inclusive="true", limit=1).get("messages") or []
        return next((m for m in messages if m.get("ts") == ts), None)

    def replies(self, channel: str, thread_ts: str, after: str) -> list[dict]:
        """The replies in a thread after the timestamp `after`, oldest first."""
        messages = self._call("conversations.replies", channel=channel, ts=thread_ts, oldest=after, limit=200).get("messages") or []
        return [m for m in messages if m.get("ts") != thread_ts and float(m.get("ts") or 0) > float(after)]
