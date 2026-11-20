"""The developer's page: accounts, timing and limits, pausing, and deleting an account."""
import dataclasses
import hashlib
import hmac
import html
import http.client
import json
import threading
import time
import unittest
from unittest import mock

from shadowdev.app import build_app
from shadowdev.clients import mail
from shadowdev.clients.github import expiry as github_expiry
from shadowdev.config import Config
from shadowdev.models.limits import Limits
from shadowdev.models.settings import Settings
from shadowdev.models.user import GitHubAccount
from shadowdev.repositories.users import ACTIVITY_PAGE
from shadowdev.server import Server, make_handler
from shadowdev.services.run_queue import RunQueue
from shadowdev.services.webhook import matching_issue
from tests.helpers import HOME, OFFICE, add_developer, add_project, join
from tests.server_base import ME, ME_HOME, Env, WebTest, payload


class LimitsTest(Env):
    def test_a_developer_can_have_at_most_ten_runs_queued(self):
        me = self.developer()
        pool = RunQueue(self.cfg, self.users, self.projects, self.runs, mock.Mock())  # not started: all stay queued
        for n in range(1, 11):
            self.assertIsNone(pool.submit(self.market, [f"MAR-{n}"], me))
        self.assertEqual(pool.submit(self.market, ["MAR-11"], me), "you already have 10 runs of marketplace queued or running")
        self.assertIsNone(pool.submit(self.market, ["MAR-11"], self.developer("acc-sara")))  # others are not affected
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME)
        self.assertIsNone(pool.submit(side, ["MAR-1"], me))  # nor are their other projects

    def test_webhooks_are_signed_fresh_and_accepted_once_also_across_restarts(self):
        self.developer()
        queued = []
        pool = mock.Mock(submit=lambda project, keys, uid, **kw: queued.append((project.name, keys[0])))

        def serve():
            server = Server(("127.0.0.1", 0), make_handler(build_app(self.cfg, mock.Mock(), pool)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server.server_address[1]

        def send(port, body: dict, path="/webhook", secret="hook-secret"):
            raw = json.dumps(body).encode()
            sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", path, raw, {"X-Hub-Signature": sig, "Content-Type": "application/json"})
            resp = conn.getresponse()
            result = resp.status, json.loads(resp.read())["status"]
            conn.close()
            return result

        port = serve()
        now = int(time.time() * 1000)
        event = {**payload(), "timestamp": now}
        self.assertEqual(send(port, {**payload()}), (400, "stale or missing timestamp"))
        self.assertEqual(send(port, {**payload(), "timestamp": now - 2 * 3600 * 1000}), (400, "stale or missing timestamp"))
        self.assertEqual(send(port, event), (202, "queued"))
        self.assertEqual(send(port, event), (200, "duplicate event"))  # a replay of the same signed event
        self.assertEqual(send(serve(), event), (200, "duplicate event"))  # also after a restart

        # Per project: its own URL and secret; the shared secret, or another project's, doesn't work there.
        secret = self.projects.webhook_secret(self.market.id)
        hook = f"/webhook?project={self.market.id}"
        event2 = {**payload(key="MAR-13", site=None), "timestamp": now}
        self.assertEqual(send(port, event2, hook), (401, "bad signature"))
        self.assertEqual(send(port, event2, "/webhook?project=999", secret), (401, "bad signature"))
        self.assertEqual(send(port, event2, hook, secret), (202, "queued"))
        self.assertEqual(queued, [("marketplace", "MAR-12"), ("marketplace", "MAR-13")])

    def test_without_a_shared_secret_only_project_webhooks_work(self):
        cfg = Config(**{**self.cfg.__dict__, "webhook_secret": None})
        status, _ = build_app(cfg, mock.Mock(), mock.Mock()).webhooks.handle(b"{}", "sha256=x")
        self.assertEqual(status, 401)

    def test_config(self):
        env = {"JIRA_SITES": "https://office.atlassian.net, https://home.atlassian.net/", "ATLASSIAN_CLIENT_ID": "a",
               "ATLASSIAN_CLIENT_SECRET": "b", "SHADOWDEV_SECRET_KEY": "d"}
        cfg = Config.from_env(env)
        self.assertEqual((cfg.jira_sites, cfg.webhook_secret), ((OFFICE.url, HOME.url), None))
        self.assertTrue(cfg.allows_site(HOME.url + "/"))
        self.assertFalse(cfg.allows_site("https://other.atlassian.net"))
        legacy = {**env, "JIRA_SITES": "", "JIRA_BASE_URL": "https://acme.atlassian.net"}
        self.assertEqual(Config.from_env(legacy).jira_sites, ("https://acme.atlassian.net",))
        self.assertTrue(Config.from_env({**env, "JIRA_SITES": "*"}).allows_site("https://anything.atlassian.net"))
        with self.assertRaisesRegex(SystemExit, "must look like"):
            Config.from_env({**env, "JIRA_SITES": "http://insecure.example"})
        with self.assertRaisesRegex(SystemExit, "must be https"):
            Config.from_env({**env, "PUBLIC_URL": "http://shadowdev.internal:8080"})
        self.assertEqual(Config.from_env({**env, "PUBLIC_URL": "http://localhost:8080"}).public_url, "http://localhost:8080")
        self.assertEqual(Config.from_env({**env, "PUBLIC_URL": "https://shadowdev.example.com"}).public_url, "https://shadowdev.example.com")
        self.assertIsNone(cfg.smtp)  # email is optional
        smtp = Config.from_env({**env, "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "dev@example.com",
                                "SMTP_USERNAME": "dev", "SMTP_PASSWORD": "pw"}).smtp
        self.assertEqual((smtp.host, smtp.port, smtp.sender, smtp.username), ("smtp.example.com", 587, "dev@example.com", "dev"))
        self.assertNotIn("pw", repr(smtp))
        with self.assertRaisesRegex(SystemExit, "SMTP_FROM"):
            Config.from_env({**env, "SMTP_HOST": "smtp.example.com"})
        with self.assertRaisesRegex(SystemExit, "SMTP_PASSWORD"):
            Config.from_env({**env, "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "a@b", "SMTP_USERNAME": "dev"})
        with self.assertRaisesRegex(SystemExit, "SMTP_PORT"):
            Config.from_env({**env, "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "a@b", "SMTP_PORT": "x"})

    def test_email_is_encrypted_except_to_a_relay_on_this_machine(self):
        for host, port, tls, ssl_ in (("smtp.example.com", 587, True, False), ("localhost", 25, False, False),
                                      ("smtp.example.com", 465, False, True)):
            with self.subTest(host=host, port=port), mock.patch.object(mail.smtplib, "SMTP") as plain, \
                    mock.patch.object(mail.smtplib, "SMTP_SSL") as encrypted:
                mail.send_mail(mail.Smtp(host, port, "dev@example.com", "dev", "pw"), "me@example.com",
                               "MAR-1:\r\nBcc: x@evil.example", "Hello")
                server = (encrypted if ssl_ else plain).return_value  # smtplib's `with` gives the server itself
                (plain if ssl_ else encrypted).assert_not_called()
                self.assertEqual(server.starttls.called, tls)
                server.login.assert_called_once_with("dev", "pw")
                sent = server.send_message.call_args.args[0]
                self.assertEqual((sent["To"], sent["Subject"], sent["Bcc"]), ("me@example.com", "MAR-1: Bcc: x@evil.example", None))
                self.assertFalse(sent.is_multipart())  # text only, without HTML

    def test_an_email_carries_its_html_beside_the_text_and_escapes_what_it_shows(self):
        from shadowdev.views import emails
        html = emails.decision({"MAR-1": ['<img src=x onerror="alert(1)"> which total?']}, OFFICE.url)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt; which total?", html)
        self.assertNotIn("<img", html)
        with mock.patch.object(mail.smtplib, "SMTP") as plain:
            mail.send_mail(mail.Smtp("localhost", 25, "dev@example.com"), "me@example.com", "MAR-1", "Text", html)
        sent = plain.return_value.send_message.call_args.args[0]
        self.assertEqual([p.get_content_type() for p in sent.iter_parts()], ["text/plain", "text/html"])
        self.assertIn("which total?", sent.get_body(("html",)).get_content())


if __name__ == "__main__":
    unittest.main()


class GitHubExpiryTest(unittest.TestCase):
    def test_reads_githubs_expiration_header(self):
        self.assertEqual(github_expiry("2030-01-02 03:04:05 UTC"), 1893553445.0)
        self.assertEqual(github_expiry("2030-01-01 20:04:05 -0700"), 1893553445.0)
        self.assertEqual((github_expiry(None), github_expiry("soon")), (0, 0))

    def test_warns_a_week_ahead(self):
        account = lambda expires: GitHubAccount(1, "office", "majid", "t", "M", "m@x", expires)
        now = 1_000_000.0
        self.assertEqual(account(0).expiry_warning(now), "")  # never expires
        self.assertEqual(account(now + 30 * 86400).expiry_warning(now), "")
        self.assertEqual(account(now + 86400 + 5).expiry_warning(now), "token expires in 1 day")
        self.assertEqual(account(now + 3600).expiry_warning(now), "token expires today")
        self.assertEqual(account(now - 1).expiry_warning(now), "token expired")
        self.assertNotIn("'t'", repr(account(0)))  # the token is never in its repr


class SettingsWebTest(WebTest):
    """The developer's page: accounts, timing and limits, pausing, and deleting an account."""

    def test_delete_account_stops_runs_and_removes_everything(self):
        self.sign_in()
        uid = self.me().id
        self.request("POST", "/delete-account", {"csrf": self.csrf()})
        self.pool.stop_runs.assert_called_once_with(uid)
        self.assertIsNone(self.users.get(uid))
        self.assertIn("Sign in with Jira", self.request("GET", "/")[2])

    def test_timing_and_limits_are_the_developers_own(self):
        self.sign_in()
        csrf = self.csrf()
        page = self.request("GET", "/")[2]
        self.assertIn("Timing and limits", page)
        self.assertIn('name="max_parallel_runs" value="5"', page)  # the defaults until changed
        self.assertIn('name="github_poll_minutes" value="1"', page)
        self.assertNotIn("sec<", page)  # every time is in minutes
        form = {"csrf": csrf, "jira_polling": "on", "jira_poll_minutes": "5", "github_poll_minutes": "0.5", "slack_poll_minutes": "1.5",
                "review_quiet_minutes": "1", "claude_timeout_minutes": "45", "command_timeout_minutes": "20",
                "max_parallel_runs": "2", "max_fix_attempts": "4"}
        self.assertEqual(self.request("POST", "/limits", form)[1], "/?ok=limits")
        self.assertEqual(self.me().limits, Limits(
            jira_polling=True, jira_poll_minutes=5, github_poll_minutes=0.5, slack_poll_minutes=1.5, review_quiet_minutes=1,
            claude_timeout_minutes=45, command_timeout_minutes=20, max_parallel_runs=2, max_fix_attempts=4))
        status, _, body = self.request("POST", "/limits", {**form, "max_parallel_runs": "50"})
        self.assertEqual(status, 400)
        self.assertIn("Runs at the same time must be a whole number from 1 to 20.", html.unescape(body))
        self.assertEqual(self.me().limits.max_parallel_runs, 2)  # unchanged
        self.request("POST", "/limits", {**form, "csrf": "forged", "max_parallel_runs": "1"})  # needs the session's CSRF token
        self.assertEqual(self.me().limits.max_parallel_runs, 2)

    def test_pause_and_resume(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        add_developer(self.users, ME, user_id=uid)  # office GitHub and Claude accounts
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        join(self.users, uid, market, ME)
        self.assertIn("Pause automation", self.request("GET", "/")[2])

        self.assertEqual(self.request("POST", "/pause", {"csrf": csrf})[1], "/?ok=paused")
        page = self.request("GET", "/")[2]
        self.assertIn("Paused: nothing runs for you", page)
        # In a word in the sidebar, on every page, leading to where to resume.
        self.assertIn('<a class="side-status warn" href="/?tab=settings" title="Paused: nothing runs for you', page)
        self.assertIn('<a class="side-status warn" href="/?tab=settings"', self.request("GET", "/project")[2])
        self.assertIn("Resume automation", page)
        self.assertTrue(self.me().paused)
        self.assertIn(market.id, self.me().subscriptions)  # settings kept
        self.assertIsNone(matching_issue(payload(), self.projects, self.users))  # webhooks ignored
        self.assertIn("automation is paused", self.run_form(csrf, {"issue_keys": "MAR-1"})[2])
        self.assertEqual(self.queued, [])

        self.assertEqual(self.request("POST", "/resume", {"csrf": csrf})[1], "/?ok=resumed")
        self.assertFalse(self.me().paused)
        self.assertIsNotNone(matching_issue(payload(), self.projects, self.users))
        # With "Start by itself" off in the project, new assignments wait for Run issues now.
        sub = self.me().subscriptions[market.id]
        self.users.subscriptions.subscribe(self.me().id, dataclasses.replace(sub, settings=Settings(auto_start=False)))
        self.assertIsNone(matching_issue(payload(), self.projects, self.users))

    def test_lost_jira_access_and_expiring_github_tokens_are_pointed_out(self):
        self.sign_in()
        me = self.me()
        self.users.mark_jira_lost(me.jira_accounts[0].id)
        self.github.user_and_expiry.side_effect = lambda: (self.github.user(), time.time() + 3 * 86400 + 60)
        self.request("POST", "/github", {"csrf": self.csrf(), "github_token": "ghp_x"})
        page = self.request("GET", "/")[2]
        self.assertIn("Jira access of Majid was lost (expired or revoked): connect that account again under Accounts.", page)
        self.assertIn("The GitHub token expires in 3 days (majid): save a new token under Accounts.", page)
        self.assertIn('<span class="tag warn">access lost: connect it again</span>', page)
        self.oauth_round_trip(self.connect())  # connecting it again mends it
        self.assertFalse(self.me().jira_accounts[0].lost)

    def test_what_is_done_to_the_account_is_listed_without_secrets(self):
        self.sign_in()
        csrf = self.csrf()
        self.request("POST", "/github", {"csrf": csrf, "github_token": "ghp_secret", "github_label": "office"})
        self.request("POST", "/pause", {"csrf": csrf})
        activity = [what for _, what, _ in self.users.activity(self.me().id)[0]]
        self.assertEqual(activity, ["Paused automation", "Saved a GitHub token", "Signed in"])
        page = self.request("GET", "/?tab=activity")[2]
        self.assertIn('data-tab="activity" aria-selected="true"', page)
        self.assertIn("Saved a GitHub token <span class=muted>majid</span>", page)
        self.assertNotIn("ghp_secret", page)
        self.assertNotIn('aria-label="Pages of activity"', page)  # one page: no pager

    def test_activity_is_shown_a_page_at_a_time(self):
        self.sign_in()  # "Signed in" is the oldest entry
        me = self.me().id
        for n in range(ACTIVITY_PAGE):
            self.users.record(me, f"Entry {n}")
        rows, page, pages = self.users.activity(me)
        self.assertEqual((len(rows), page, pages), (ACTIVITY_PAGE, 1, 2))
        first = self.request("GET", "/?tab=activity")[2]
        self.assertIn(f"Entry {ACTIVITY_PAGE - 1}", first)
        self.assertNotIn("<li><span>Signed in</span>", first)
        self.assertIn('href="/?tab=activity&amp;activity_page=2" rel="next"', first)
        second = self.request("GET", "/?tab=activity&activity_page=2")[2]
        self.assertIn("<li><span>Signed in</span>", second)
        self.assertNotIn("Entry 1<", second)
        self.assertIn("Page 2 of 2", second)
        # A page past the last, or not a number, gives the last or the first.
        self.assertIn("Page 2 of 2", self.request("GET", "/?tab=activity&activity_page=99")[2])
        self.assertIn("Page 1 of 2", self.request("GET", "/?tab=activity&activity_page=x")[2])
