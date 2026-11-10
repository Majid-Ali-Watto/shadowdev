import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from shadowdev.clients import atlassian
from shadowdev.clients.atlassian import AtlassianOAuth, JiraAccessLost, jira_client
from shadowdev.models.tokens import Tokens
from shadowdev.clients.rest import HTTPError


class FakeUsers:
    def __init__(self, tokens):
        self.tokens = tokens
        self.saved = []

    def jira_tokens(self, account_id):
        return self.tokens

    def save_jira_tokens(self, account_id, tokens):
        self.tokens = tokens
        self.saved.append(tokens)


class AtlassianOAuthTest(unittest.TestCase):
    def setUp(self):
        self.oauth = AtlassianOAuth("cid", "secret", "https://sd.example/oauth/jira/callback")

    def test_authorize_url_asks_for_jira_access_and_a_refresh_token(self):
        url = urlsplit(self.oauth.authorize_url("st4te"))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://auth.atlassian.com/authorize")
        self.assertEqual((q["client_id"], q["state"], q["response_type"], q["audience"], q["prompt"]),
                         ("cid", "st4te", "code", "api.atlassian.com", "consent"))
        self.assertEqual(q["redirect_uri"], "https://sd.example/oauth/jira/callback")
        self.assertEqual(set(q["scope"].split()), {"read:jira-work", "write:jira-work", "read:jira-user", "offline_access"})

    def test_exchange_and_site_lookup(self):
        calls = []

        def fake(method, url, headers, body=None, label=""):
            calls.append((method, url, headers, body))
            if url.endswith("/oauth/token"):
                return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            return [{"id": "other", "url": "https://other.atlassian.net/", "name": "other", "scopes": ["read:jira-work"]},
                    {"id": "cloud-1", "url": "https://acme.atlassian.net", "name": "acme"},
                    {"id": "conf", "url": "https://wiki.atlassian.net", "scopes": ["read:confluence-content.all"]}]

        with mock.patch.object(atlassian, "request_json", fake):
            tokens = self.oauth.exchange("c0de")
            sites = self.oauth.sites("a")
        # Every Jira site the grant reaches (one account can reach several); not other products.
        self.assertEqual([(s.cloud_id, s.url, s.name) for s in sites],
                         [("other", "https://other.atlassian.net", "other"), ("cloud-1", "https://acme.atlassian.net", "acme")])
        self.assertEqual((tokens.access_token, tokens.refresh_token), ("a", "r"))
        self.assertAlmostEqual(tokens.expires_at, time.time() + 3600, delta=5)
        self.assertEqual(calls[0][3], {"client_id": "cid", "client_secret": "secret", "grant_type": "authorization_code",
                                       "code": "c0de", "redirect_uri": "https://sd.example/oauth/jira/callback"})
        self.assertEqual(calls[1][2], {"Authorization": "Bearer a"})


class JiraClientTest(unittest.TestCase):
    def test_uses_the_access_token_until_it_expires_then_refreshes_and_saves_the_rotated_one(self):
        store = FakeUsers(Tokens("fresh", "r1", time.time() + 3600))
        oauth = mock.Mock()
        jira = jira_client(oauth, store, 1, "cloud-1")
        self.assertEqual(jira.api_base, "https://api.atlassian.com/ex/jira/cloud-1")
        with mock.patch("shadowdev.clients.jira.request_json") as request:
            jira.myself()
            self.assertEqual(request.call_args.args[2], {"Authorization": "Bearer fresh"})
            oauth.refresh.assert_not_called()

            store.tokens = Tokens("stale", "r1", time.time() + 30)  # inside the refresh margin
            oauth.refresh.return_value = Tokens("new", "r2", time.time() + 3600)
            jira.myself()
            self.assertEqual(request.call_args.args[2], {"Authorization": "Bearer new"})
        oauth.refresh.assert_called_once_with("r1")
        self.assertEqual(store.saved, [oauth.refresh.return_value])

    def test_concurrent_requests_refresh_once(self):
        # Refresh tokens rotate: a second refresh with the same token would fail.
        store = FakeUsers(Tokens("stale", "r1", 0))
        oauth = mock.Mock()

        def refresh(token):
            time.sleep(0.05)
            return Tokens("new", "r2", time.time() + 3600)

        oauth.refresh.side_effect = refresh
        jira = jira_client(oauth, store, 2, "cloud-1")
        with mock.patch("shadowdev.clients.jira.request_json"):
            threads = [threading.Thread(target=jira.myself) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(oauth.refresh.call_count, 1)

    def test_revoked_grant_asks_to_sign_in_again(self):
        oauth = mock.Mock()
        oauth.refresh.side_effect = HTTPError("invalid_grant", 403)
        jira = jira_client(oauth, FakeUsers(Tokens("stale", "r1", 0)), 3, "cloud-1")
        with self.assertRaisesRegex(JiraAccessLost, "sign in to ShadowDev again"):
            jira.myself()


class RestTest(unittest.TestCase):
    def call(self, method, outcomes):
        """`outcomes`: per attempt, an HTTP status to fail with, an exception, or a JSON body."""
        import io
        import urllib.error
        from shadowdev.clients import rest
        outcomes = list(outcomes)

        def urlopen(request, timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, int):
                raise urllib.error.HTTPError(request.full_url, outcome, "x", {"Retry-After": "1"}, io.BytesIO(b"no"))
            if isinstance(outcome, Exception):
                raise outcome
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = outcome
            return response

        with mock.patch.object(rest.urllib.request, "urlopen", urlopen), mock.patch.object(rest.time, "sleep") as sleep:
            try:
                return rest.request_json(method, "https://x.test/a?b=1", {}), sleep.call_count, outcomes
            except rest.HTTPError as e:
                return e, sleep.call_count, outcomes

    def test_rate_limits_and_failed_reads_are_tried_again_but_writes_are_not_repeated(self):
        self.assertEqual(self.call("POST", [429, b'{"ok": 1}']), ({"ok": 1}, 1, []))  # refused, so safe to repeat
        self.assertEqual(self.call("GET", [502, OSError("reset"), b'[]']), ([], 2, []))
        error, sleeps, left = self.call("POST", [502, b'{}'])  # it may have been carried out
        self.assertEqual((error.status, sleeps, left), (502, 0, [b'{}']))
        error, sleeps, left = self.call("GET", [404, b'{}'])
        self.assertEqual((error.status, sleeps), (404, 0))
        error, sleeps, left = self.call("GET", [TimeoutError("timed out")] * 3)  # unreachable: status 0
        self.assertEqual((error.status, sleeps, left), (0, 2, []))
        self.assertIn("could not be reached", str(error))


if __name__ == "__main__":
    unittest.main()
