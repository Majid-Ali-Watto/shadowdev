"""Signing in with Jira, connecting accounts, and sessions."""
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

from shadowdev.models.user import Site
from tests.helpers import OFFICE, add_developer
from tests.server_base import ME, WebTest


class SignInWebTest(WebTest):
    """Signing in with Jira, connecting accounts, and sessions."""

    def test_sign_in_with_jira_oauth(self):
        page = self.request("GET", "/")[2]
        self.assertIn(f'href="{self.cfg.public_url}/login/jira"', page)

        # The button sends the browser to Atlassian with a one-time state, also kept in a cookie.
        status, location, _ = self.request("GET", "/login/jira")
        self.assertTrue(location.startswith("https://auth.atlassian.com/authorize?state="))
        state = parse_qs(urlsplit(location).query)["state"][0]
        self.assertEqual(self.cookies["shadowdev_oauth_state"], f"signin.{state}")

        # A callback with someone else's state, or a refusal, signs nobody in.
        self.assertIn("expired or was started elsewhere",
                      self.request("GET", "/oauth/jira/callback?code=c0de&state=forged")[2])
        self.assertIn("not granted", self.request("GET", "/oauth/jira/callback?error=access_denied&state=x")[2])
        self.oauth.exchange.assert_not_called()

        status, location, _ = self.sign_in()
        self.assertEqual((status, location), (303, self.cfg.public_url + "/"))
        self.oauth.exchange.assert_called_once_with("c0de")
        self.assertTrue(any("shadowdev_session=" in c and "HttpOnly; SameSite=Lax" in c for c in self.set_cookies))
        self.assertNotIn("shadowdev_oauth_state", self.cookies)  # one-time
        user = self.me()
        self.assertEqual((user.display_name, user.email, list(user.sites)), ("Majid", "majid@example.com", [OFFICE.cloud_id]))
        self.assertEqual(self.users.jira_tokens(user.jira_accounts[0].id).refresh_token, "refresh-1")
        self.assertIn("Signed in as Majid (majid@example.com)", self.request("GET", "/")[2])

    def test_a_new_sign_in_ends_the_browsers_previous_session(self):
        self.sign_in()
        old = self.cookies["shadowdev_session"]
        self.sign_in()  # the same browser signs in again
        self.assertNotEqual(self.cookies["shadowdev_session"], old)
        self.cookies["shadowdev_session"] = old  # what the old cookie would still send
        self.assertEqual(self.request("GET", "/runs")[0], 401)

    def test_sign_in_needs_a_site_this_server_allows(self):
        self.oauth.sites.return_value = [Site("x", "https://someone-else.atlassian.net", "x")]
        self.assertIn("None of the Jira sites you granted is enabled", self.sign_in()[2])
        self.assertIsNone(self.users.owner_of(ME))

    def test_connecting_another_developers_jira_account_is_refused(self):
        add_developer(self.users, "acc-sara")
        self.sign_in()
        location = self.connect()
        self.me_jira = {"accountId": "acc-sara", "displayName": "Sara", "accountType": "atlassian"}
        self.assertIn("already connected to another ShadowDev account", self.oauth_round_trip(location)[2])
        self.assertEqual(len(self.me().jira_accounts), 1)
        # Connecting needs a signed-in browser: a "connect" state without a session signs nobody in.
        self.cookies.pop("shadowdev_session")
        self.cookies["shadowdev_oauth_state"] = "connect.abc"
        self.assertIn("expired or was started elsewhere", self.request("GET", "/oauth/jira/callback?code=c&state=abc")[2])

    def test_using_shadowdev_renews_the_session_but_the_pages_own_refresh_does_not(self):
        self.sign_in()
        token = self.cookies["shadowdev_session"]

        def age(days_left):
            with sqlite3.connect(self.cfg.data_dir / "shadowdev.db") as db:
                db.execute("UPDATE sessions SET expires = ?", (time.time() + days_left * 86400,))

        self.request("GET", "/")
        self.assertEqual(self.set_cookies, [])  # signed in just now: nothing to renew
        age(2)
        self.assertEqual(self.request("GET", "/runs")[0], 200)
        self.assertEqual(self.set_cookies, [])  # an open tab alone does not keep the session alive
        self.request("GET", "/")
        self.assertIn(f"shadowdev_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800", self.set_cookies[0])
        self.request("GET", "/")
        self.assertEqual(self.set_cookies, [])  # renewed: a full week again
        age(2)
        self.request("POST", "/logout", {"csrf": self.csrf()})  # (csrf() renewed it) signing out still wins
        self.assertNotIn("shadowdev_session", self.cookies)
        age(-1)
        self.cookies["shadowdev_session"] = token
        self.assertEqual(self.request("GET", "/runs")[0], 401)
