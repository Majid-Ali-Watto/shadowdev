"""Watching pull requests in review: review comments on ShadowDev's PRs, and review requests in Slack."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from shadowdev.clients import github as github_client
from shadowdev.clients import slack as slack_client
from shadowdev.clients.github import GitHub
from shadowdev.clients.rest import HTTPError
from shadowdev.clients.slack import Slack
from shadowdev.models.limits import Limits
from shadowdev.models.project import Project
from shadowdev.models.settings import Settings
from shadowdev.services import reviews
from tests.helpers import OFFICE, add_developer, join, make_cfg, repo

ME, SARA = "acc-majid", "acc-sara"
NOW = 1_800_000_000.0
MY_SLACK = "U0000MEME0"


def at(seconds_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


def thread(author="sara", association="MEMBER", seconds_ago=600, resolved=False):
    return {"id": "T1", "resolved": resolved, "path": "a.py", "line": 1, "hunk": "",
            "comments": [{"author": author, "association": association, "body": "fix this", "at": at(seconds_ago)}]}


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.jobs = []  # (project name, keys, developer, work, after)
        queue = mock.Mock(submit=lambda project, keys, uid, work=None, after=None: self.jobs.append(
            (project.name, keys, uid, work, after)))
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), queue)
        users, projects = self.app.users, self.app.projects
        self.me = add_developer(users, ME)
        pid = projects.save(Project(0, "marketplace", self.me, OFFICE.cloud_id, OFFICE.url, frozenset({"MAR"}), None,
                                    (repo("fe"), repo("services")), slack_channel="C0REVIEWS1"))
        self.market = projects.get(pid)
        join(users, self.me, self.market, ME)
        users.save_slack(self.me, "xoxp-me", MY_SLACK, "acme")
        self.github, self.slack = mock.Mock(), mock.Mock()
        self.github.review_threads.return_value = ("OPEN", [])
        self.github.latest_reviews.return_value = []
        self.github.ci_state.return_value = ("c0ffee", "SUCCESS")
        self.github.check_runs.return_value = []
        self.github.statuses.return_value = []
        self.slack.history.return_value = []
        self.slack.replies.return_value = []
        self.slack.message.return_value = None
        for name, client in (("GitHub", self.github), ("Slack", self.slack)):
            patch = mock.patch.object(reviews, name, return_value=client)
            patch.start()
            self.addCleanup(patch.stop)
        self.watcher, self.reviews = self.app.watcher, self.app.reviews

    def poll(self):
        self.jobs.clear()
        self.watcher.poll(NOW)
        return [(name, keys) for name, keys, *_ in self.jobs]

    def test_github_and_slack_are_read_each_at_the_developers_own_interval(self):
        self.reviews.track_pr(self.me, self.market.id, "fe", "acme/fe", 7, "MAR-1", ("C0REVIEWS1", "1.2"))
        self.app.users.set_limits(self.me, Limits(github_poll_minutes=0.5, slack_poll_minutes=5))
        self.app.users.mark_slack_seen(self.me, self.market.id, f"{NOW - 60:.6f}")  # the channel was read before
        read = lambda: (self.github.review_threads.call_count, self.slack.history.call_count)
        self.watcher.poll(NOW, due_only=True)
        self.assertEqual(read(), (1, 1))  # both, the first time
        self.watcher.poll(NOW + 29, due_only=True)
        self.assertEqual(read(), (1, 1))  # neither is due
        self.watcher.poll(NOW + 30, due_only=True)
        self.assertEqual(read(), (2, 1))  # GitHub only
        self.watcher.poll(NOW + 300, due_only=True)
        self.assertEqual(read(), (3, 2))  # both

    def test_review_comments_on_a_tracked_pr_start_a_fix_once(self):
        self.reviews.track_pr(self.me, self.market.id, "fe", "acme/fe", 7, "MAR-1", ("C0REVIEWS1", "1.2"))
        self.assertEqual(self.poll(), [])  # no comments yet
        for waiting_for_someone_else in (thread(seconds_ago=30), thread(association="NONE"), thread(author=ME),
                                         thread(resolved=True)):  # still being written; a passer-by; mine; done
            self.github.review_threads.return_value = ("OPEN", [waiting_for_someone_else])
            self.assertEqual(self.poll(), [])
        self.github.review_threads.return_value = ("OPEN", [thread()])
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])
        work, after = self.jobs[0][3:]
        with mock.patch.object(reviews, "address_review", return_value=("done", "ok")) as address:
            user = self.app.users.get(self.me)
            self.assertEqual(work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None), ("done", "ok"))
        self.assertEqual(address.call_args.args[:4], (self.market.repo("fe"), 7, "MAR-1", ("C0REVIEWS1", "1.2")))
        after()
        self.assertEqual(self.poll(), [])  # those comments are handled, however the run ended
        self.github.review_threads.return_value = ("OPEN", [thread(seconds_ago=300)])  # a newer one
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])
        # At most five rounds per PR; and a merged PR is no longer watched.
        for _ in range(5):
            self.reviews.pr_handled(self.reviews.tracked_prs()[0]["id"], "")
        self.assertEqual(self.poll(), [])
        self.github.review_threads.return_value = ("MERGED", [])
        self.reviews.pr_handled(self.reviews.tracked_prs()[0]["id"], "")
        with self.app.reviews.db.connect() as conn:
            conn.execute("UPDATE pull_requests SET rounds = 0")
            conn.commit()
        self.poll()
        self.assertEqual(self.reviews.tracked_prs(), [])

    def test_failing_ci_on_a_tracked_pr_starts_a_fix_once_per_commit_after_review_comments(self):
        self.reviews.track_pr(self.me, self.market.id, "fe", "acme/fe", 7, "MAR-1", None)
        failed = {"id": 41, "name": "lint", "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"}}
        self.github.ci_state.return_value = ("sha-1", "FAILURE")
        self.github.check_runs.return_value = [failed, {**failed, "name": "e2e", "status": "in_progress", "conclusion": None}]
        self.assertEqual(self.poll(), [])  # a check still running: wait for all of them
        self.github.check_runs.return_value = [failed]
        self.github.review_threads.return_value = ("OPEN", [thread()])
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])  # the review comments go first
        self.assertEqual(self.jobs[0][3].__qualname__.split(".")[1], "_review_comments")
        self.jobs[0][4]()
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])  # then the CI
        work, after = self.jobs[0][3:]
        with mock.patch.object(reviews, "fix_ci", return_value=("done", "ok")) as fix:
            user = self.app.users.get(self.me)
            self.assertEqual(work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None), ("done", "ok"))
        self.assertEqual(fix.call_args.args[:4], (self.market.repo("fe"), 7, "MAR-1", "sha-1"))
        after()
        self.assertEqual(self.poll(), [])  # that commit's CI is handled, however the run ended
        self.github.ci_state.return_value = ("sha-2", "ERROR")
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])  # a new commit's is not
        self.jobs[0][4]()
        self.github.ci_state.return_value = ("sha-3", "PENDING")
        self.assertEqual(self.poll(), [])
        # At most three rounds per PR.
        self.github.ci_state.return_value = ("sha-4", "FAILURE")
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])
        self.jobs[0][4]()
        self.github.ci_state.return_value = ("sha-5", "FAILURE")
        self.assertEqual(self.poll(), [])

    def test_each_developer_chooses_what_is_watched_for_them(self):
        self.reviews.track_pr(self.me, self.market.id, "fe", "acme/fe", 7, "MAR-1", None)
        self.github.review_threads.return_value = ("OPEN", [thread()])
        self.github.ci_state.return_value = ("sha-1", "FAILURE")
        self.github.check_runs.return_value = [{"id": 1, "name": "lint", "status": "completed", "conclusion": "failure"}]
        join(self.app.users, self.me, self.market, ME, settings=Settings(fix_review_comments=False))
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])
        self.assertEqual(self.jobs[0][3].__qualname__.split(".")[1], "_failing_ci")  # the CI, not the comments
        join(self.app.users, self.me, self.market, ME,
             settings=Settings(fix_review_comments=False, fix_ci=False, review_for_me=False))
        self.github.review_threads.reset_mock()
        self.slack.history.return_value = [{"ts": "1800000100.0", "user": "U0SARA", "text": f"<@{MY_SLACK}> please "
                                            "review https://github.com/acme/fe/pull/9"}]
        self.assertEqual(self.poll(), [])
        self.github.review_threads.assert_not_called()  # its PRs aren't even looked at
        self.slack.history.assert_not_called()  # nor its Slack channel
        self.assertEqual(self.reviews.in_state("requested"), [])

    def test_github_ci_checks_and_job_logs(self):
        rollup = {"headRefOid": "abc", "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}}
        with mock.patch.object(github_client, "request_json",
                               lambda *a, **k: {"data": {"repository": {"pullRequest": rollup}}}):
            self.assertEqual(GitHub("tok").ci_state("acme/fe", 7), ("abc", "FAILURE"))
        rollup["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
        with mock.patch.object(github_client, "request_json",
                               lambda *a, **k: {"data": {"repository": {"pullRequest": rollup}}}):
            self.assertEqual(GitHub("tok").ci_state("acme/fe", 7), ("abc", None))  # no CI at all
        # The log is a redirect to a signed URL elsewhere: followed without the token.
        fetched = []
        with mock.patch.object(github_client, "redirect_location", lambda url, headers, label: (
                fetched.append((url, headers["Authorization"])) or "https://logs.example/signed?sig=1")), \
             mock.patch.object(github_client, "download", lambda url, headers, limit, label: (
                fetched.append((url, headers)) or b"2026-01-01T00:00:00.0000000Z error: x")):
            self.assertEqual(GitHub("tok").job_log("acme/fe", 41, 100), "2026-01-01T00:00:00.0000000Z error: x")
        self.assertEqual(fetched, [("https://api.github.com/repos/acme/fe/actions/jobs/41/logs", "Bearer tok"),
                                   ("https://logs.example/signed?sig=1", {})])

    def test_a_redirect_is_read_not_followed(self):
        import urllib.error
        from email.message import Message

        from shadowdev.clients import rest

        def answer(code, location):
            headers = Message()
            if location:
                headers["Location"] = location

            def open_(request, timeout):
                self.assertEqual(request.headers["Authorization"], "Bearer tok")
                raise urllib.error.HTTPError(request.full_url, code, "x", headers, None)
            return mock.patch.object(rest._direct, "open", open_)

        with answer(302, "https://logs.example/x"):
            self.assertEqual(rest.redirect_location("https://api.github.com/l", {"Authorization": "Bearer tok"}),
                             "https://logs.example/x")
        for code, location in ((302, "http://plain.example/x"), (302, None), (404, None)):
            with answer(code, location), self.assertRaises(HTTPError):
                rest.redirect_location("https://api.github.com/l", {"Authorization": "Bearer tok"})

    def test_an_approved_pr_is_not_fixed_even_with_review_comments(self):
        self.reviews.track_pr(self.me, self.market.id, "fe", "acme/fe", 7, "MAR-1", None)
        self.github.review_threads.return_value = ("OPEN", [thread()])
        self.github.latest_reviews.return_value = [{"author": "sara", "association": "MEMBER", "state": "APPROVED"}]
        self.assertEqual(self.poll(), [])
        self.github.latest_reviews.return_value = [{"author": "sara", "association": "MEMBER", "state": "CHANGES_REQUESTED"}]
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#7"])])

    def test_a_mention_in_slack_with_a_pr_of_the_project_starts_a_review_and_again_when_asked(self):
        self.assertEqual(self.poll(), [])  # the first look only notes where the channel is
        self.slack.history.assert_not_called()
        self.slack.history.return_value = [
            {"user": "U0000SARA0", "ts": "1800000010.000100", "text": f"<@{MY_SLACK}> please review <https://github.com/Acme/FE/pull/12>"},
            {"user": "U0000SARA0", "ts": "1800000011.000100", "text": "https://github.com/acme/fe/pull/13 anyone?"},  # not for me
            # not the project's
            {"user": "U0000SARA0", "ts": "1800000012.000100", "text": f"<@{MY_SLACK}> https://github.com/other/repo/pull/1"},
            {"user": MY_SLACK, "ts": "1800000013.000100", "text": f"<@{MY_SLACK}> https://github.com/acme/fe/pull/14"},  # my own message
        ]
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#12"])])
        self.assertEqual(self.slack.history.call_args.args, ("C0REVIEWS1", 20))
        self.assertEqual(self.app.users.slack_seen(self.me, self.market.id), "1800000013.000100")
        work, after = self.jobs[0][3:]
        user = self.app.users.get(self.me)
        with mock.patch.object(reviews, "review_pr", return_value=("done", "1 comment", "commented")) as review_pr:
            self.assertEqual(work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None), ("done", "1 comment"))
        self.assertEqual(review_pr.call_args.args[:3], (self.market.repo("fe"), 12, ("C0REVIEWS1", "1800000010.000100")))
        after()
        self.slack.history.return_value = []
        self.assertEqual(self.poll(), [])  # commented: waiting to be asked again
        self.assertEqual(self.slack.replies.call_args.args, ("C0REVIEWS1", "1800000010.000100", "1800000010.000100"))
        self.slack.replies.return_value = [{"user": MY_SLACK, "ts": "1800000020.000100", "text": "Comments added (1)"},
                                           {"user": "U0000SARA0", "ts": "1800000030.000100", "text": "thanks!"}]
        self.assertEqual(self.poll(), [])
        self.slack.replies.return_value.append({"user": "U0000SARA0", "ts": "1800000040.000100", "text": "fixed, review again please"})
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#12"])])
        self.jobs[0][4]()  # that run never started (stopped): the request counts as failed, not asked again by itself
        self.slack.replies.return_value = []
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.slack.replies.call_args.args[2], "1800000040.000100")
        # A paused developer is not watched.
        self.app.users.set_paused(self.me, True)
        self.slack.replies.reset_mock()
        self.assertEqual(self.poll(), [])
        self.slack.replies.assert_not_called()


    def test_mentions_in_the_threads_of_recent_messages_count_each_once(self):
        self.poll()  # the first look
        seen = f"{NOW:.6f}"
        mention = f"<@{MY_SLACK}> https://github.com/acme/fe/pull/21"
        parents = [{"user": "U0000SARA0", "ts": "1799999000.000100", "text": "old, no replies"},
                   {"user": "U0000SARA0", "ts": "1799999100.000100", "text": "old, quiet thread", "latest_reply": "1799999200.000100"},
                   {"user": "U0000SARA0", "ts": "1799999300.000100", "text": "old, new reply", "latest_reply": "1800000050.000100"}]
        self.slack.history.return_value = parents
        self.slack.replies.return_value = [{"user": "U0000SARA0", "ts": "1800000050.000100", "thread_ts": "1799999300.000100",
                                            "text": mention}]
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#21"])])
        self.assertEqual(self.slack.replies.call_args_list, [mock.call("C0REVIEWS1", "1799999300.000100", seen)])  # only that thread
        self.assertEqual(self.reviews.in_state("requested")[0]["ts"], "1799999300.000100")  # answered in that thread
        self.assertEqual(self.app.users.slack_seen(self.me, self.market.id), "1800000050.000100")
        # Read again a minute later: nothing new, so no thread is fetched and nothing is asked twice.
        self.reviews.finish(self.reviews.in_state("requested")[0]["id"], "approved")
        self.slack.replies.reset_mock()
        self.assertEqual(self.poll(), [])
        self.slack.replies.assert_not_called()
        self.assertEqual(self.reviews.in_state("requested"), [])


    def test_a_message_asking_several_people_gives_each_the_prs_after_their_mention(self):
        fe, services = "https://github.com/acme/fe/pull", "https://github.com/acme/services/pull"
        text = (f"please review these PRs:\n<@U0000RAJA0> <@{MY_SLACK}|majid>\n<{fe}/830>\n<{fe}/831>\n\n"
                f"<@U0000FASI0> <@U0000NABI0>\n<{services}/939>\n<{services}/940>")
        self.assertEqual(reviews.asked_prs(text, MY_SLACK), [("acme/fe", 830), ("acme/fe", 831)])
        self.assertEqual(reviews.asked_prs(text, "U0000NABI0"), [("acme/services", 939), ("acme/services", 940)])
        self.assertEqual(reviews.asked_prs(f"{fe}/1 and {fe}/2, <@{MY_SLACK}> please, cc <@U0000LEAD0>", MY_SLACK),
                         [("acme/fe", 1), ("acme/fe", 2)])  # links before the first mention belong to it
        self.assertEqual(reviews.asked_prs(f"{fe}/1 <@U0000RAJA0>", MY_SLACK), [])

    def test_a_pr_a_colleague_asked_with_me_reacted_eyes_to_first_is_theirs(self):
        fe = "https://github.com/acme/fe/pull"
        text = f"<@U0000RAJA0> <@{MY_SLACK}> {fe}/830 <@U0000FASI0> https://github.com/acme/services/pull/939"
        eyes = lambda *users: {"text": text, "reactions": [{"name": "thumbsup", "users": ["U0000RAJA0"]},
                                                            {"name": "eyes", "users": list(users)}]}
        self.assertEqual(reviews.claimed_by(eyes("U0000RAJA0", MY_SLACK), MY_SLACK, "acme/fe", 830), "U0000RAJA0")
        self.assertIsNone(reviews.claimed_by(eyes(MY_SLACK, "U0000RAJA0"), MY_SLACK, "acme/fe", 830))  # mine came first
        self.assertIsNone(reviews.claimed_by(eyes("U0000FASI0"), MY_SLACK, "acme/fe", 830))  # not asked about it
        self.assertIsNone(reviews.claimed_by({"text": text}, MY_SLACK, "acme/fe", 830))
        # A review about to start is skipped.
        self.reviews.request(self.me, self.market.id, "acme/fe", 830, "C0REVIEWS1", "1800000010.000100", "1800000010.000100")
        self.slack.message.return_value = eyes("U0000RAJA0")
        self.poll()
        (_, _, _, work, after), = self.jobs
        user = self.app.users.get(self.me)
        with mock.patch.object(reviews, "review_pr") as review_pr:
            status, message = work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None)
        review_pr.assert_not_called()
        self.assertEqual(status, "skipped")
        self.assertIn("U0000RAJA0", message)
        self.assertEqual(self.slack.message.call_args.args, ("C0REVIEWS1", "1800000010.000100", "1800000010.000100"))
        after()
        self.assertEqual(self.reviews.in_state("closed")[0]["number"], 830)

    def test_mentioned_later_in_the_thread_of_a_request_to_others_reviews_its_prs_unless_one_started(self):
        self.poll()  # the first look
        fe, services = "https://github.com/acme/fe/pull", "https://github.com/acme/services/pull"
        first = {"user": "U0000SARA0", "ts": "1799999000.000100", "latest_reply": "1800000030.000100",
                 "text": f"please review <@U0000RAJA0> <{fe}/830> <@U0000FASI0> <{services}/939>"}
        self.slack.history.return_value = [first]
        replies = [  # not asking me: thanks, a no, someone else, a question
            {"user": "U0000SARA0", "ts": "1800000010.000100", "thread_ts": first["ts"], "text": f"<@{MY_SLACK}> thanks"},
            {"user": "U0000SARA0", "ts": "1800000011.000100", "thread_ts": first["ts"], "text": f"<@{MY_SLACK}> no need to review"},
            {"user": "U0000SARA0", "ts": "1800000012.000100", "thread_ts": first["ts"], "text": "<@U0000NABI0> can you review?"},
            {"user": "U0000SARA0", "ts": "1800000013.000100", "thread_ts": first["ts"], "text": f"<@{MY_SLACK}> are you reviewing?"},
            {"user": MY_SLACK, "ts": "1800000014.000100", "thread_ts": first["ts"], "text": f"<@{MY_SLACK}> please review"}]
        self.slack.replies.return_value = replies
        self.assertEqual(self.poll(), [])
        join(self.app.users, self.me, self.market, ME, settings=Settings(no_review_repos=("services",)))
        self.slack.replies.return_value = [{"user": "U0000SARA0", "ts": "1800000030.000100", "thread_ts": first["ts"],
                                            "text": f"<@{MY_SLACK}> can you review these? Raja is out"}]
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#830"])])  # not services: I don't review it
        row = self.reviews.in_state("requested")[0]
        self.assertEqual((row["ts"], row["last_ts"]), (first["ts"], "1800000030.000100"))  # answered in the thread
        # When it starts, Raja, asked first, has reacted 👀 on the first message: he has it.
        asking = {"text": self.slack.replies.return_value[0]["text"], "reactions": [{"name": "eyes", "users": [MY_SLACK]}]}
        self.slack.message.side_effect = lambda channel, thread, ts: asking if ts != thread else {
            **first, "reactions": [{"name": "eyes", "users": ["U0000RAJA0"]}]}
        work = self.jobs[0][3]
        user = self.app.users.get(self.me)
        with mock.patch.object(reviews, "review_pr", return_value=("done", "1 comment", "commented")) as review_pr:
            self.assertEqual(work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None)[0], "skipped")
            review_pr.assert_not_called()
            first["reactions"] = [{"name": "eyes", "users": ["U0000FASI0"]}]  # 👀 only from those asked about services
            self.slack.message.side_effect = lambda channel, thread, ts: asking if ts != thread else first
            work(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None)
            review_pr.assert_called_once()

    def test_only_the_chosen_repos_are_reviewed_and_the_asking_message_gets_reactions(self):
        self.poll()  # the first look
        fe, services = "https://github.com/acme/fe/pull", "https://github.com/acme/services/pull"
        self.slack.history.return_value = [{"user": "U0000SARA0", "ts": "1800000010.000100", "text":
                                            f"<@{MY_SLACK}> {fe}/1 {fe}/2 {services}/3"}]
        join(self.app.users, self.me, self.market, ME, settings=Settings(no_review_repos=("services",)))
        self.assertEqual(self.poll(), [("marketplace", ["acme/fe#1"]), ("marketplace", ["acme/fe#2"])])
        asking = ("C0REVIEWS1", "1800000010.000100")
        self.assertEqual(self.slack.react.call_args_list, [mock.call(*asking, "eyes", on=True)] * 2)
        self.slack.react.reset_mock()
        (_, _, _, work1, after1), (_, _, _, work2, after2) = self.jobs
        user = self.app.users.get(self.me)
        with mock.patch.object(reviews, "review_pr", return_value=("done", "approved", "approved")):
            work1(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None)
        after1()
        self.assertEqual(self.slack.react.call_args_list, [mock.call(*asking, "eyes", on=True)])  # #2 is still waiting
        self.slack.react.reset_mock()
        with mock.patch.object(reviews, "review_pr", return_value=("done", "1 comment", "commented")):
            work2(self.market, user.actor(self.market.id), self.app.cfg, self.github, None, None)
        after2()
        self.assertEqual(self.slack.react.call_args_list, [mock.call(*asking, "eyes", on=False), mock.call(*asking, "memo")])
        # Turned off after being asked: not reviewed.
        self.reviews.request(self.me, self.market.id, "acme/fe", 9, *asking, "1800000020.000100")
        join(self.app.users, self.me, self.market, ME, settings=Settings(no_review_repos=("fe",)))
        self.slack.history.return_value = []
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.reviews.in_state("requested"), [])


class ClientsTest(unittest.TestCase):
    def test_slack_errors_are_raised_and_messages_come_oldest_first(self):
        calls = []

        def fake(method, url, headers, body=None, label=""):
            calls.append((method, url, body))
            if "auth.test" in url:
                return {"ok": False, "error": "invalid_auth"}
            if "chat.postMessage" in url:
                return {"ok": True, "ts": "5.5"}
            if "reactions." in url:
                return {"ok": False, "error": "already_reacted" if body["name"] == "eyes" else "missing_scope"}
            return {"ok": True, "messages": [{"ts": "3.0"}, {"ts": "1.0"}, {"ts": "2.0"}]}

        with mock.patch.object(slack_client, "request_json", fake):
            slack = Slack("xoxp-1")
            with self.assertRaisesRegex(HTTPError, "invalid_auth"):
                slack.whoami()
            self.assertEqual(slack.post("C1", "hi", "1.0"), "5.5")
            self.assertEqual(calls[-1][2], {"channel": "C1", "text": "hi", "unfurl_links": False, "thread_ts": "1.0"})
            self.assertEqual([m["ts"] for m in slack.history("C1", 20)], ["1.0", "2.0", "3.0"])
            self.assertIn("conversations.history?channel=C1&limit=20", calls[-1][1])
            self.assertEqual([m["ts"] for m in slack.replies("C1", "1.0", "2.0")], ["3.0"])  # not the thread's first message
            slack.react("C1", "1.0", "eyes")  # already there: fine
            self.assertEqual(calls[-1][2], {"channel": "C1", "timestamp": "1.0", "name": "eyes"})
            with self.assertRaisesRegex(HTTPError, "missing_scope"):
                slack.react("C1", "1.0", "memo", on=False)
            self.assertIn("reactions.remove", calls[-1][1])
            self.assertEqual(slack.message("C1", "1.0", "2.0"), {"ts": "2.0"})
            self.assertIn("conversations.replies?channel=C1&ts=1.0&oldest=2.0&latest=2.0&inclusive=true", calls[-1][1])
        self.assertEqual(slack_client.escape("a <b> & c"), "a &lt;b&gt; &amp; c")

    def test_github_review_threads_and_reviews(self):
        calls = []
        node = {"id": "T1", "isResolved": False, "path": "a.py", "line": None, "comments": {"nodes": [
            {"author": None, "authorAssociation": "MEMBER", "body": "fix", "createdAt": "2026-01-01T00:00:00Z", "diffHunk": "@@"}]}}

        def fake(method, url, headers, body=None, label=""):
            calls.append((method, url, body))
            if "mutation" in (body or {}).get("query", ""):
                return {"errors": [{"message": "Resource not accessible"}]}
            return {"data": {"repository": {"pullRequest": {"state": "OPEN", "reviewThreads": {"nodes": [node]}}}}}

        with mock.patch.object(github_client, "request_json", fake):
            github = GitHub("tok")
            state, threads = github.review_threads("acme/fe", 7)
            self.assertEqual(calls[-1][2]["variables"], {"owner": "acme", "name": "fe", "number": 7})
            self.assertEqual((state, threads), ("OPEN", [{"id": "T1", "resolved": False, "path": "a.py", "line": None, "hunk": "@@",
                                                         "comments": [{"author": "", "association": "MEMBER", "body": "fix",
                                                                       "at": "2026-01-01T00:00:00Z"}]}]))
            node_review = {"state": "APPROVED", "authorAssociation": "MEMBER", "author": {"login": "sara"}}
            with mock.patch.object(github_client, "request_json", lambda *a, **k: {"data": {"repository": {"pullRequest": {
                    "latestOpinionatedReviews": {"nodes": [node_review]}}}}}):
                self.assertEqual(github.latest_reviews("acme/fe", 7), [{"author": "sara", "association": "MEMBER", "state": "APPROVED"}])
            with self.assertRaisesRegex(HTTPError, "Resource not accessible"):
                github.resolve_thread("T1")
            github.create_review("acme/fe", 7, "COMMENT", "summary", [{"path": "a.py", "line": 3, "body": "b"}])
            self.assertEqual(calls[-1], ("POST", "https://api.github.com/repos/acme/fe/pulls/7/reviews", {
                "event": "COMMENT", "body": "summary", "comments": [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "b"}]}))


if __name__ == "__main__":
    unittest.main()
