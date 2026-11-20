"""The pages themselves: themes, origins, bad requests, rate limits, health, Features and Overview."""
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev import server as server_module
from shadowdev.app import build_app
from shadowdev.automation import limits
from shadowdev.automation.agent import claude_config_dir
from shadowdev.models.publication import Change, Publication
from shadowdev.models.settings import Settings
from shadowdev.routes import LIMITS, ROUTES, ratelimit
from shadowdev.routes import settings as settings_routes
from shadowdev.routes.ratelimit import RateLimiter
from shadowdev.routes.request import Request
from shadowdev.views.components import alert, esc, remove_form
from shadowdev.views.overview import overview_panel
from tests.helpers import add_developer, add_project, join, make_cfg
from tests.server_base import WebTest


class RateLimiterTest(unittest.TestCase):
    def test_counts_per_client_route_and_minute(self):
        limiter = RateLimiter({"/x": 2})
        self.assertEqual([limiter.retry_after("a", "/x", 120.0) for _ in range(3)], [0, 0, 60])
        self.assertEqual(limiter.retry_after("b", "/x", 130.0), 0)  # another client
        self.assertEqual(limiter.retry_after("a", "/y", 130.0), 0)  # a route without a limit
        self.assertEqual(limiter.retry_after("a", "/x", 175.0), 5)  # still that minute
        self.assertEqual(limiter.retry_after("a", "/x", 180.0), 0)  # the next one

    def test_a_full_minute_turns_new_clients_away_without_rebuilding_the_counts(self):
        limiter = RateLimiter({"/x": 5})
        with mock.patch.object(ratelimit, "MAX_CLIENTS", 2):
            self.assertEqual([limiter.retry_after(c, "/x", 120.0) for c in ("a", "b")], [0, 0])
            counts = limiter._counts
            self.assertEqual(limiter.retry_after("c", "/x", 130.0), 60)  # no room for another client this minute
            self.assertIs(limiter._counts, counts)  # and nothing was copied to find that out
            self.assertEqual(limiter.retry_after("a", "/x", 131.0), 0)  # the clients counted are still counted
            self.assertEqual(limiter.retry_after("c", "/x", 180.0), 0)  # the next minute has room again


class RequestTest(unittest.TestCase):
    """What a request is answered with: a page's security headers, and whose forms it accepts."""

    def setUp(self):
        self.app = mock.Mock(cfg=mock.Mock(public_url="http://localhost:8080"))

    def request(self, headers: dict) -> Request:
        return Request(mock.Mock(path="/", headers=headers), self.app)

    def test_a_page_without_scripts_allows_none_and_an_empty_nonce_is_refused(self):
        req = self.request({})
        req.html("<p>x</p>")
        csp = [c.args[1] for c in req.handler.send_header.call_args_list if c.args[0] == "Content-Security-Policy"]
        self.assertIn("script-src 'none'", csp[0])
        with self.assertRaises(ValueError):
            self.request({}).html("<p>x</p>", nonce="")

    def test_a_form_must_come_from_the_public_url_or_this_machine_by_either_name(self):
        for origin, allowed in ((None, True), ("http://localhost:8080", True), ("http://127.0.0.1:8080", True),
                                ("http://localhost:9999", False), ("https://evil.example", False), ("null", False)):
            self.assertEqual(self.request({"Origin": origin} if origin else {}).same_origin(), allowed, origin)


class RouteTableTest(unittest.TestCase):
    """The route table says who may call each route, and how often: the rate limits come from it."""

    def test_the_routes_anyone_can_call_without_signing_in_are_known_and_limited(self):
        anonymous = {key for key, route in ROUTES.items() if route.anonymous}
        self.assertEqual(anonymous, {("GET", "/"), ("GET", "/login/jira"), ("GET", "/oauth/jira/callback"),
                                     ("POST", "/webhook"), ("GET", "/health")})
        for key in anonymous:
            self.assertIsNotNone(ROUTES[key].limit, f"{key} is open to anyone and has no limit")

    def test_the_rate_limits_are_the_route_tables(self):
        self.assertEqual(LIMITS, {path: route.limit for (_, path), route in ROUTES.items() if route.limit is not None})
        self.assertEqual(LIMITS["/project/test-message"], 6)


class ComponentsTest(unittest.TestCase):
    """Escaping: a value is escaped once, however many helpers it passes through."""

    def test_remove_forms_escape_their_action_and_field(self):
        form = remove_form('/x"><script>', 'f"', 3, "", "<b>", "m")
        self.assertNotIn("<script>", form)
        self.assertIn('action="/x&quot;&gt;&lt;script&gt;"', form)
        self.assertIn('name="f&quot;"', form)

    def test_helper_output_is_not_escaped_twice(self):
        inner = alert("a < b")
        self.assertIn("a &lt; b", inner)
        self.assertEqual(esc(inner), inner)
        self.assertEqual(esc("<i>"), "&lt;i&gt;")


class OverviewTest(unittest.TestCase):
    """What the Overview gathers beyond runs: PRs in review, what waits for working hours, Claude usage."""

    def setUp(self):
        limits.USAGE.clear()
        self.addCleanup(limits.USAGE.clear)
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), mock.Mock())
        self.uid = add_developer(self.app.users, "acc-majid")
        self.market = add_project(self.app.projects, self.uid, "marketplace", ["MAR"])
        join(self.app.users, self.uid, self.market, "acc-majid",
             settings=Settings(work_from="09:00", work_until="17:00", work_days=("sat",), work_timezone="UTC"))

    def panel(self) -> str:
        user = self.app.users.get(self.uid)
        return overview_panel(user, self.app.overview.of(user))

    def test_prs_held_work_and_claude_usage(self):
        self.assertIn("No open PRs of yours are watched", self.panel())
        self.assertIn("Nothing is waiting", self.panel())
        self.assertIn("Not known yet", self.panel())
        reviews = self.app.reviews
        reviews.track_pr(self.uid, self.market.id, "fe", "acme/fe", 7, "MAR-1", None)
        reviews.track_pr(self.uid, self.market.id, "fe", "acme/fe", 8, "MAR-2", None)
        reviews.set_pr_review(reviews.prs_of(self.uid)[0]["id"], "approved:2")
        held = self.app.overview.held
        held.hold_publication(self.uid, self.market.id, None, None,
                              Publication(("MAR-3",), "MAR-3", "MAR-3: x", (Change("fe", "main", "new", "body"),)))
        held.hold_call(self.uid, self.market.id, "jira", "comment", ["MAR-3", "secret <text>"])
        held.hold_slack(self.uid, self.market.id, "C1", "reply", "1.2")
        user = self.app.users.get(self.uid)
        key = str(claude_config_dir(self.app.cfg, user.claude_accounts[0]))
        now = time.time()
        limits.USAGE.record(key, {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.4, "resetsAt": now + 3600},
            "seven_day": {"utilization": 0.95, "resetsAt": now + 86400}}})
        panel = self.panel()
        self.assertIn('href="https://github.com/acme/fe/pull/8" target="_blank" rel="noopener">acme/fe#8</a><small>MAR-2</small>', panel)
        self.assertIn("approved by 2", panel)
        self.assertIn("not checked yet", panel)  # its review was not read yet
        self.assertIn('<div class="k">PRs in review</div><div class="v">2</div>', panel)
        self.assertIn("marketplace: your hours begin", panel)
        self.assertIn("Pull requests of MAR-3", panel)
        self.assertIn("1 comment on Jira and GitHub", panel)
        self.assertIn("1 Slack reply to reviewers", panel)
        self.assertNotIn("secret", panel)  # what a held comment says is not shown
        self.assertIn('aria-valuenow="40" aria-label="5-hour limit"><i class="" style="width:40%">', panel)
        self.assertIn('aria-valuenow="95" aria-label="Weekly limit"><i class="warn" style="width:95%">', panel)
        self.assertIn('<span class="tag warn">at its limit</span>', panel)  # 95% of the week, which resets in a day


class PagesWebTest(WebTest):
    """The pages themselves: themes, origins, bad requests, rate limits, health, Features and Overview."""

    def test_health_and_the_login_page_are_rate_limited(self):
        for _ in range(60):
            self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/health")[0], 429)
        for _ in range(120):
            self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 429)

    def test_a_request_must_arrive_within_its_deadline(self):
        # A client that never ends its headers is hung up on at the deadline, not after 20 seconds per read.
        with mock.patch.object(server_module, "REQUEST_DEADLINE_SECONDS", 1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            self.addCleanup(sock.close)
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
            started = time.monotonic()
            self.assertEqual(sock.recv(1024), b"")
            self.assertLess(time.monotonic() - started, 5)

    def test_a_body_cut_short_is_not_answered(self):
        # The client promised 100 bytes, sent 7 and closed its sending side: no 500 goes to it.
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(b'POST /webhook?project=1 HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{"a":1}')
        sock.shutdown(socket.SHUT_WR)
        self.assertEqual(sock.recv(1024), b"")

    def test_ids_are_ascii_digits_so_a_superscript_two_is_refused_not_a_crash(self):
        # "²" passes str.isdigit(), and int() then refuses it: each place must answer, never with a 500.
        self.sign_in()
        self.pool.live.return_value = []
        two = "²"
        q = "%C2%B2"  # the same character in a URL: a request line is ASCII
        self.assertEqual(self.request("GET", f"/runs/log?run={q}")[0], 404)
        self.assertEqual(self.request("GET", f"/project/statuses?id={q}")[0], 404)
        self.assertEqual(self.request("GET", f"/project?id={q}")[0], 303)
        self.assertEqual(self.request("GET", f"/logs/live?seen=1:{q}")[0], 200)
        self.assertEqual(self.request("GET", f"/?answer={q}")[0], 200)
        self.assertEqual(self.request("GET", f"/runs?page={q}")[0], 200)
        self.assertEqual(self.request("POST", "/github/remove", {"csrf": self.csrf(), "github_account_id": two})[0], 400)
        self.assertEqual(self.request("POST", "/stop", {"csrf": self.csrf(), "run_id": two})[0], 400)
        self.assertEqual(self.request("POST", "/project/join", {"csrf": self.csrf(), "project_id": two})[0], 400)
        self.assertEqual(self.request("POST", "/github", headers={"Content-Length": two})[0], 413)
        self.assertEqual(self.request("POST", f"/webhook?project={q}", {"a": "1"})[0], 401)

    def test_fragments_fetched_into_a_page_allow_no_script(self):
        self.sign_in()
        for path in ("/runs", "/overview"):
            self.assertEqual(self.request("GET", path)[0], 200)
            self.assertIn("script-src 'none'", self.headers["Content-Security-Policy"])

    def test_pages_render_in_the_chosen_theme(self):
        self.assertIn('<html lang="en">', self.request("GET", "/")[2])  # no choice: the system's
        self.cookies["shadowdev_theme"] = "dark"
        self.assertIn('<html lang="en" data-theme="dark">', self.request("GET", "/")[2])
        self.sign_in()
        page = self.request("GET", "/")[2]
        self.assertIn('data-theme="dark"', page)
        self.assertIn("data-theme-toggle", page)
        self.cookies["shadowdev_theme"] = '"><script>'  # only light or dark is ever used
        self.assertIn('<html lang="en">', self.request("GET", "/")[2])

    def test_pages_let_browsers_send_their_origin(self):
        # With Referrer-Policy: no-referrer, browsers send "Origin: null" on form posts, which the
        # Origin check rejects: no form would work.
        self.sign_in()
        csrf = self.csrf()
        self.assertEqual(self.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"}, {"Origin": "null"})[0], 403)
        # On this machine the server answers by either loopback name at its public port (make dev).
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": "http://127.0.0.1:8080"})[0], 303)
        # Host is not trusted: a page of another site sending a matching Host is refused.
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": f"http://127.0.0.1:{self.port}", "Host": f"127.0.0.1:{self.port}"})[0], 403)
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": "http://evil.example", "Host": "evil.example"})[0], 403)
        # Behind a proxy that rewrites Host, the public URL is the origin.
        self.assertEqual(self.request("POST", "/github", {"csrf": csrf, "github_token": "gh"},
                                      {"Origin": self.cfg.public_url, "Host": "127.0.0.1:9"})[0], 303)

    def test_bad_requests_are_answered_not_crashed(self):
        self.sign_in()
        self.assertEqual(self.request("POST", "/github", headers={"Content-Length": "x"})[0], 413)
        with mock.patch.object(self.app.accounts, "set_paused", side_effect=RuntimeError("bug")), \
                self.assertLogs("shadowdev", "ERROR"):
            self.assertEqual(self.request("POST", "/pause", {"csrf": self.csrf()})[0], 500)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_health_says_only_ok(self):
        self.assertEqual(self.request("GET", "/health"), (200, None, '{"status": "ok"}'))

    def test_sign_in_and_webhook_are_rate_limited_per_client(self):
        for _ in range(30):
            self.assertEqual(self.request("GET", "/login/jira")[0], 303)
        status, _, body = self.request("GET", "/login/jira")
        self.assertEqual((status, body), (429, '{"status": "too many requests"}'))
        self.assertTrue(1 <= int(self.headers["Retry-After"]) <= 60)
        # Behind a proxy on this machine, each client is counted by the address the proxy gives.
        self.assertEqual(self.request("GET", "/login/jira", headers={"X-Forwarded-For": "203.0.113.9"})[0], 303)
        self.assertEqual(self.request("GET", "/health")[0], 200)  # other routes are not limited

    def test_features_tab_explains_shadowdev(self):
        self.sign_in()
        page = self.request("GET", "/?tab=features")[2]
        self.assertIn('data-tab="features" aria-selected="true"', page)
        self.assertIn('<ol class="flow" aria-label="A run, step by step">', page)
        self.assertIn("<b>Sandboxed.</b>", page)
        self.assertIn('id="panel-features" role="tabpanel" aria-labelledby="tab-features" hidden',
                      self.request("GET", "/?tab=activity")[2])  # in every page, shown only when chosen

    def test_overview_shows_what_needs_the_developer_and_what_runs(self):
        self.assertEqual(self.request("GET", "/overview")[0], 401)
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        asked = self.runs.add("MAR-1", uid, market.id, "marketplace")
        self.runs.update(asked, "question", "needs your decision")
        self.runs.set_questions(asked, {"MAR-1": ["Keep the old <API>?"]})
        self.runs.update(self.runs.add("MAR-2", uid, market.id, "marketplace"), "failed", "tests <b>failed</b>")
        self.runs.update(self.runs.add("MAR-3", uid, market.id, "marketplace"), "failed", "flaky")
        self.runs.update(self.runs.add("MAR-3", uid, market.id, "marketplace"), "done", "PR opened")  # ran again since
        running = self.runs.add("MAR-4", uid, market.id, "marketplace")
        self.runs.update(running, "running")
        self.runs.set_step(running, "Claude analysis")
        self.runs.add("MAR-5", uid, market.id, "marketplace")  # queued
        page = self.request("GET", "/?tab=overview")[2]
        self.assertIn('data-tab="overview" aria-selected="true"', page)
        self.assertIn('<span class="count" data-needs-count aria-label="2 need you">2</span>', page)
        self.assertIn("MAR-1 asks you 1 question<small>Keep the old &lt;API&gt;?", page)
        self.assertIn(f'href="/?tab=runs&amp;answer={asked}#run-form">Answer</a>', page)
        self.assertIn("MAR-2 failed<small>tests &lt;b&gt;failed&lt;/b&gt;", page)
        self.assertNotIn("MAR-3 failed", page)  # run again since: it no longer needs the developer
        self.assertIn('aria-valuenow="3" aria-label="Analysing: phase 3 of 6"', page)
        self.assertIn('<div class="v">1</div><div class="s">1 queued', page)  # running now
        self.assertIn('<div class="k">Done, last 7 days</div><div class="v">1</div>', page)
        # What the page loads to keep itself up to date: the same panel.
        panel = self.request("GET", "/overview")[2]
        self.assertTrue(panel.startswith('<div id="overview" data-needs="2">'), panel[:80])
        self.assertIn("MAR-2 failed", panel)

    def test_signed_in_pages_are_parts_of_the_app(self):
        login = self.request("GET", "/")[2]
        self.assertNotIn("<main data-app>", login)  # outside the app: forms there leave for Atlassian
        self.assertIn("ShadowDev.mount(", login)  # its theme switcher
        self.sign_in()
        page = self.request("GET", "/")[2]
        self.assertIn("<main data-app>", page)
        for name in ("app.js", "theme.js", "tabs.js", "runs.js", "overview.js", "logs.js", "project_editor.js", "statuses.js"):
            self.assertIn(Path(settings_routes.__file__).parents[1].joinpath("views/static", name).read_text(), page)
        self.assertEqual(page.count("<script nonce="), 8)
        # Forms that leave the app load in full.
        for action in ("/logout", "/jira/connect", "/delete-account"):
            self.assertRegex(page, rf'<form method="post" action="{action}" data-reload')
        editor = self.request("GET", "/project")[2]
        self.assertIn("<main data-app>", editor)
        # The project editor is part of Projects: the same sidebar, there with links back to the sections.
        self.assertIn('<a href="/?tab=projects" aria-current="page">', editor)
        self.assertIn('<a href="/?tab=overview">', editor)
        self.assertNotIn('id="tab-projects"', editor)  # links, not tabs of this page
        self.assertIn('<nav class="crumbs" aria-label="Breadcrumb"><a href="/?tab=projects">Projects</a>', editor)
        self.assertIn('action="/logout" data-reload data-confirm=', editor)
        # Forms that delete, stop, change or sign out ask first, saying what follows.
        self.assertIn('action="/logout" data-reload data-confirm="Only this browser is signed out. Automation keeps '
                      'working on your issues." data-confirm-title="Sign out?" data-confirm-action="Sign out">', page)
        self.assertIn('data-confirm-title="Delete your ShadowDev account?" data-confirm-action="Delete account" '
                      'data-confirm-danger>', page)
        self.assertIn('action="/pause" data-confirm=', page)
        self.assertIn('action="/limits" data-confirm=', page)
        self.assertIn('id="delete-runs" data-confirm=', page)
