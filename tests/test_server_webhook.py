"""The Jira webhook: signatures, routing to the developer, and what it queues."""
import dataclasses
import hashlib
import hmac
import json
import time
from unittest import mock

from shadowdev.models.settings import Settings
from shadowdev.models.user import Site
from shadowdev.services.webhook import WebhookService, matching_issue, verify_signature
from tests.helpers import HOME, add_developer, add_project, join, repo
from tests.server_base import ACTIVE_SPRINT, ME, ME_HOME, Env, payload


class WebhookTest(Env):
    def test_signature(self):
        body = b'{"a":1}'
        good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_signature("s3cret", body, good))
        self.assertFalse(verify_signature("other", body, good))
        self.assertFalse(verify_signature("s3cret", body, None))
        self.assertFalse(verify_signature("s3cret", body, "sha256=caf\u00e9"))  # refused, not an error
        self.assertFalse(verify_signature("s3cret", body, good.removeprefix("sha256=")))

    def test_an_event_that_failed_to_queue_is_processed_when_jira_sends_it_again(self):
        self.developer()
        queue = mock.Mock(submit=mock.Mock(side_effect=RuntimeError("the queue is down")))
        service = WebhookService(self.cfg, self.users, self.projects, self.runs, queue)
        body = json.dumps({**payload(), "timestamp": int(time.time() * 1000)}).encode()
        signature = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        with self.assertRaises(RuntimeError):
            service.handle(body, signature)
        queue.submit.side_effect, queue.submit.return_value = None, None
        self.assertEqual(service.handle(body, signature), (202, "queued"))  # Jira's retry, not "duplicate event"
        self.assertEqual(service.handle(body, signature), (200, "duplicate event"))  # and a replay after that

    def test_routes_to_the_project_on_its_site_and_the_developer_of_that_jira_account(self):
        me = self.developer(ME, mode="all")
        billing = add_project(self.projects, me, "billing", ["BIL"], repos=[repo("billing")])
        join(self.users, me, billing, ME, "all")
        # The same person's personal Jira, GitHub and Claude, for a personal project that also uses MAR.
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME)
        sara = self.developer("acc-sara")
        add_developer(self.users, "acc-new")  # signed in, but in no project
        match = lambda project=None, **kw: matching_issue(payload(**kw), self.projects, self.users, project)

        self.assertEqual(match(), ("MAR-12", self.market, me))
        self.assertEqual(match(key="MARS-3", assignee="acc-sara"), ("MARS-3", self.market, sara))
        self.assertEqual(match(key="BIL-7", type_="Story"), ("BIL-7", billing, me))
        self.assertEqual(match(event="jira:issue_updated", type_="bug")[0], "MAR-12")
        # Only an issue in a status that starts by itself, and in a sprint that has started.
        future = [{**ACTIVE_SPRINT[0], "state": "future"}]
        with self.assertLogs("shadowdev", "INFO") as logs:
            for kw in ({"status": "In Progress"}, {"sprint": None}, {"sprint": future},
                       {"sprint": [{**ACTIVE_SPRINT[0], "state": "closed"}]}):
                self.assertIsNone(match(**kw), kw)
        self.assertIn("MAR-12: not started by itself for", logs.output[0])
        self.assertIn("its status In Progress is not one that starts by itself", logs.output[0])
        self.assertIn("it is in no sprint", logs.output[1])
        self.assertIn("its sprint has not started, or has ended", logs.output[2])
        self.assertEqual(match(status="to do", sprint=[*future, *ACTIVE_SPRINT])[0], "MAR-12")
        # A project without sprints starts on the status alone.
        no_sprints = self.projects.get(self.projects.save(dataclasses.replace(self.market, uses_sprints=False)))
        self.assertEqual(match(no_sprints, sprint=None)[0], "MAR-12")
        uid = self.users.owner_of(ME)
        sub = self.users.get(uid).subscriptions[self.market.id]
        self.users.subscriptions.subscribe(uid, dataclasses.replace(sub, settings=Settings(auto_statuses="", auto_sprint=False)))
        self.assertEqual(match(status="In QA", sprint=None)[0], "MAR-12")  # conditions off: any status, no sprint
        # MAR-12 at home is the personal project's, for the personal Jira account only.
        self.assertEqual(match(site=HOME, assignee=ME_HOME), ("MAR-12", side, me))
        self.assertIsNone(match(site=HOME, assignee=ME))
        self.assertIsNone(match(site=HOME, assignee=ME_HOME, type_="Story"))  # bugs only there
        self.assertEqual(match(project=side, site=None, assignee=ME_HOME), ("MAR-12", side, me))  # per-project URL
        self.assertIsNone(match(project=side, key="BIL-1", site=None, assignee=ME_HOME))
        self.assertIsNone(match(site=Site("x", "https://elsewhere.atlassian.net", "x")))
        self.assertIsNone(match(site=None))  # shared URL: the site must be known
        self.assertIsNone(match(key="BIL-7", assignee="acc-sara"))  # not in billing
        self.assertIsNone(match(type_="Task", assignee="acc-sara"))  # bugs only
        self.assertIsNone(match(type_="Epic"))
        self.assertIsNone(match(assignee="acc-new"))
        self.assertIsNone(match(assignee="acc-unknown"))
        self.assertIsNone(match(key="HR-1"))  # no project
        self.assertIsNone(match(event="comment_created"))
        # Keys become branch and directory names, so anything odd is rejected.
        self.assertIsNone(match(key="MAR-1; rm -rf /"))
        self.assertIsNone(match(key="../MAR-1"))
        self.assertIsNone(match(key="MAR-1\n"))
