"""Runs: queueing and running issues, the run page, Recent runs, Live logs, and the queue."""
import dataclasses
import http.client
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from shadowdev.automation import limits, pipeline, runlog
from shadowdev.automation.agent import claude_config_dir
from shadowdev.automation.timing import Timings
from shadowdev.clients.rest import HTTPError
from shadowdev.models import settings as settings_model
from shadowdev.models.limits import Limits
from shadowdev.models.publication import Change, Publication
from shadowdev.models.run import PHASES, RunFilter, phase
from shadowdev.models.settings import Settings
from shadowdev.repositories.held import HeldRepository
from shadowdev.routes import settings as settings_routes
from shadowdev.services import accounts, run_queue
from shadowdev.services.run_queue import RunQueue
from shadowdev.views.home import home_page
from shadowdev.views.runs import example_keys, runs_panel
from tests.helpers import HOME, OFFICE, add_developer, add_project, join, repo
from tests.server_base import ME, ME_HOME, Env, QueueEnv, WebTest, fake_process_factory


class RunFormTest(Env):
    def test_its_examples_use_the_developers_own_jira_keys(self):
        me = self.developer()  # marketplace: MAR and MARS
        crm = add_project(self.projects, me, "crm", ["KKA", "CRM"], repos=[repo("crm")])
        self.assertEqual(example_keys([]), ("ABC", "ABC"))
        self.assertEqual(example_keys([self.market]), ("MAR", "MAR"))
        self.assertEqual(example_keys([crm, self.market]), ("CRM", "MAR"))
        form = runs_panel(5, [crm, self.market], "t")
        self.assertIn('placeholder="CRM-123, MAR-45, CRM-1 to CRM-10 except CRM-3"', form)
        self.assertIn("<code>CRM-123:</code>", form)

    def test_the_repos_to_change_are_each_projects(self):
        me = self.developer()  # marketplace: fe and services
        crm = add_project(self.projects, me, "crm", ["CRM"], repos=[repo("crm")])
        panel = lambda joined, answering=None: runs_panel(5, joined, "t", answering)
        box = lambda p, name, ticked=False: (f'<input type="checkbox" name="repos" value="{p.id}:{name}"'
                                             f'{" checked" if ticked else ""}>')
        form = panel([self.market])
        for name in ("fe", "services"):
            self.assertIn(box(self.market, name), form)  # several: none ticked until chosen
        self.assertNotIn('<div class="sub-head">', form)  # one project: no project names
        form = panel([crm, self.market])
        self.assertIn(box(crm, "crm", ticked=True), form)  # the only repo: ticked
        self.assertIn(f'<div data-project="{crm.id}"><div class="sub-head">crm</div>', form)
        # Answering a run's questions: its repos ticked again; all of them when it could change any.
        asked = lambda repos: {"project_id": self.market.id, "issue_key": "MAR-1", "questions": None, "repos": repos}
        form = panel([crm, self.market], asked("services"))
        self.assertIn(box(self.market, "services", ticked=True), form)
        self.assertIn(box(self.market, "fe"), form)
        form = panel([crm, self.market], asked(""))
        self.assertIn(box(self.market, "fe", ticked=True), form)


class RunQueueTest(QueueEnv):
    def test_runs_up_to_the_limit_in_parallel_and_never_one_issue_twice(self):
        me = self.developer()
        running, peak, lock, release = set(), [0], threading.Lock(), threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, **kw):
            with lock:
                running.add(tuple(keys))
                peak[0] = max(peak[0], len(running))
            release.wait(5)
            with lock:
                running.discard(tuple(keys))
            return "done", "ok"

        pool = self.pool(fake_process)
        for n in range(1, 7):
            self.assertIsNone(pool.submit(self.market, [f"MAR-{n}"], me))
        self.assertEqual(pool.submit(self.market, ["MAR-1"], me), "already queued or running: MAR-1")
        self.assertIn("MAR-2", pool.submit(self.market, ["MAR-7", "MAR-2"], me))  # MAR-2 is in another job
        self.wait_for(lambda: len(running) == 5)  # the sixth waits for a free slot
        release.set()
        done = lambda: all(r["status"] == "done" for r in self.runs.recent(me))
        self.wait_for(done)
        self.assertEqual(peak[0], 5)
        self.assertEqual(sorted(r["issue_key"] for r in self.runs.recent(me)), [f"MAR-{n}" for n in range(1, 7)])
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))  # finished, so it can run again
        self.wait_for(done)

    def test_each_developer_runs_up_to_their_own_limit(self):
        me, sara = self.developer(), self.developer("acc-sara")
        self.users.set_limits(me, Limits(max_parallel_runs=2))
        running, release = [], threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            running.append((actor.jira_account_id, keys[0], cfg.max_fix_attempts))
            release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process, start=False)
        for n in range(1, 5):
            pool.submit(self.market, [f"MAR-{n}"], me)
        pool.submit(self.market, ["MAR-9"], sara)
        pool.start()
        self.wait_for(lambda: len(running) == 3)
        time.sleep(0.1)
        self.assertEqual(sorted(running), [(ME, "MAR-1", 3), (ME, "MAR-2", 3), ("acc-sara", "MAR-9", 3)])
        self.users.set_limits(me, Limits(max_parallel_runs=3, max_fix_attempts=5))
        pool.limits_changed()  # as AccountService.set_limits does: waiting runs see the new limit
        self.wait_for(lambda: len(running) == 4)
        self.assertEqual(running[-1], (ME, "MAR-3", 5))  # a run gets its developer's limits when it starts
        release.set()
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))

    def test_a_finished_run_logs_its_end(self):
        me = self.developer()
        pool = self.pool(fake_process_factory(), start=False)
        with self.assertLogs("shadowdev", "INFO") as logs:
            pool.submit(self.market, ["MAR-1", "MAR-2"], me)
            pool.start()
            self.wait_for(lambda: any("===== END =====" in line for line in logs.output))
        self.assertRegex("\n".join(logs.output), r"===== END ===== run \d+ \(MAR-1, MAR-2\) finished after [\d.ms]+")

    def test_the_same_key_on_two_sites_runs_at_once(self):
        me = self.developer()
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME)
        pool = self.pool(fake_process_factory(block=threading.Event()), start=False)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.assertIsNone(pool.submit(side, ["MAR-1"], me))

    def side_project(self, me, **settings):
        """The developer's personal project, joined with their personal accounts and these settings."""
        add_developer(self.users, ME_HOME, sites=(HOME,), user_id=me)
        side = add_project(self.projects, me, "side", ["MAR"], site=HOME)
        join(self.users, me, side, ME_HOME, settings=Settings(**settings))
        return side

    def test_each_project_runs_up_to_its_own_limit(self):
        me = self.developer()
        join(self.users, me, self.market, ME, settings=Settings(parallel_runs="1"))
        side = self.side_project(me)
        running, release = [], threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            running.append((project.name, keys[0]))
            release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process, start=False)
        for n in (1, 2, 3):
            pool.submit(self.market, [f"MAR-{n}"], me)
        pool.submit(side, ["MAR-1"], me)
        pool.start()
        self.wait_for(lambda: len(running) == 2)
        time.sleep(0.1)
        self.assertEqual(sorted(running), [("marketplace", "MAR-1"), ("side", "MAR-1")])  # marketplace's next ones wait
        join(self.users, me, self.market, ME, settings=Settings(parallel_runs="2"))
        pool.limits_changed()  # as ProjectService.join does
        self.wait_for(lambda: len(running) == 3)
        self.assertEqual(running[-1], ("marketplace", "MAR-2"))
        release.set()
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))

    def test_a_free_slot_goes_to_the_project_with_the_highest_priority(self):
        me = self.developer()
        order = []

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            order.append(f"{project.name} {keys[0]}")
            return "done", "ok"

        side = self.side_project(me)
        for priority, expected in (("normal", ["marketplace MAR-1", "side MAR-5", "marketplace MAR-2", "side MAR-6"]),
                                   ("high", ["side MAR-5", "side MAR-6", "marketplace MAR-1", "marketplace MAR-2"])):
            with self.subTest(side=priority):
                order.clear()
                join(self.users, me, side, ME_HOME, settings=Settings(priority=priority))
                pool = self.pool(fake_process, workers=1, start=False)
                for project, keys in ((self.market, ("MAR-1", "MAR-2")), (side, ("MAR-5", "MAR-6"))):
                    for key in keys:
                        self.assertIsNone(pool.submit(project, [key], me))
                pool.start()
                self.wait_for(lambda: len(order) == 4)
                self.assertEqual(order, expected)  # the same priority: in turn

    def test_priority_is_within_a_developers_turn(self):
        me, sara = self.developer(), self.developer("acc-sara")
        side = self.side_project(me, priority="high")
        order = []
        pool = self.pool(fake_process_factory(order), workers=1, start=False)
        pool.submit(self.market, ["MAR-1"], me)
        pool.submit(side, ["MAR-5"], me)
        pool.submit(self.market, ["MAR-9"], sara)
        pool.start()
        self.wait_for(lambda: len(order) == 3)
        self.assertEqual([keys[0] for _, keys in order], ["MAR-5", "MAR-9", "MAR-1"])  # Sara's turn comes between

    def test_developers_take_turns(self):
        me, sara = self.developer(), self.developer("acc-sara")
        order = []
        pool = self.pool(fake_process_factory(order), workers=1, start=False)
        for n in (1, 2, 3):
            pool.submit(self.market, [f"MAR-{n}"], me)
        pool.submit(self.market, ["MAR-9"], sara)
        pool.start()
        self.wait_for(lambda: len(order) == 4)
        self.assertEqual([keys[0] for _, keys in order], ["MAR-1", "MAR-9", "MAR-2", "MAR-3"])

    def test_queued_runs_survive_a_restart(self):
        me = self.developer()
        waiting = self.runs.add("MAR-1, MAR-2", me, self.market.id, "marketplace", "Only the API.")  # left by the previous server
        gone = self.runs.add("MAR-3", me, None, "deleted project")
        ran = []
        self.pool(fake_process_factory(ran))
        self.wait_for(lambda: ran)
        self.assertEqual(ran, [(ME, ("MAR-1", "MAR-2"), "Only the API.")])  # with its note
        status = {r["id"]: (r["status"], r["message"]) for r in self.runs.recent(me)}
        self.wait_for(lambda: self.runs.recent(me)[-1]["status"] == "done")
        self.assertEqual(self.runs.recent(me)[-1]["model"], "haiku")  # the run reports its model
        self.assertEqual(status[gone][0], "failed")
        self.assertIn("project was deleted", status[gone][1])
        self.assertIn(waiting, status)

    def test_work_on_a_pull_request_runs_in_the_queue_and_is_not_resumed_after_a_restart(self):
        me = self.developer()
        left = self.runs.add("acme/fe#3", me, self.market.id, "marketplace")  # queued when the server stopped
        pool = self.pool(fake_process_factory())
        self.assertIn("starts again by itself", next(r for r in self.runs.recent(me) if r["id"] == left)["message"])
        seen, ended = [], []

        def work(project, actor, cfg, github, cancel, on_model):
            seen.append((project.name, actor.display_name, cancel.cancelled))
            on_model("default")
            return "done", "approved https://github.com/acme/fe/pull/7"

        self.assertIsNone(pool.submit(self.market, ["acme/fe#7"], me, work=work, after=lambda: ended.append("review")))
        self.assertEqual(pool.submit(self.market, ["acme/fe#7"], me, work=work), "already queued or running: acme/fe#7")
        self.wait_for(lambda: ended)
        run = self.runs.recent(me)[0]
        self.assertEqual((seen, run["issue_key"], run["status"], run["model"]),
                         ([("marketplace", ME, False)], "acme/fe#7", "done", "default"))
        # One that never starts (its developer paused) still ends, so what asked for it is told.
        self.users.set_paused(me, True)
        self.assertIsNone(pool.submit(self.market, ["acme/fe#8"], me, work=work, after=lambda: ended.append("skipped")))
        self.wait_for(lambda: ended == ["review", "skipped"])
        self.assertEqual(len(seen), 1)

    def test_runs_use_the_project_and_accounts_as_they_are_when_they_start(self):
        me = self.developer()
        seen = []
        pool = self.pool(lambda keys, project, actor, *a, **k: seen.append((project.name, actor.github.login)) or ("done", ""),
                         start=False)
        pool.submit(self.market, ["MAR-1"], me)
        self.users.remove_github(me, self.users.get(me).github_accounts[0].id)  # before it starts
        pool.start()
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "skipped")
        self.assertEqual(seen, [])
        self.assertIn("GitHub or Claude account was removed", self.runs.recent(me)[0]["message"])


class ClaudeLimitTest(QueueEnv):
    def setUp(self):
        super().setUp()
        limits.USAGE.clear()
        self.addCleanup(limits.USAGE.clear)
        patcher = mock.patch.object(limits, "POLL", 0.02)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nearly_used(self, me, resets_in: float, label: str | None = None):
        """The developer's Claude account `label` (default: the project's own) is at 98% of its 5-hour limit."""
        user = self.users.get(me)
        claude = next((c for c in user.claude_accounts if c.label == label), user.actor(self.market.id).claude)
        limits.USAGE.record(str(claude_config_dir(self.cfg, claude)), {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.98, "resetsAt": time.time() + resets_in}}})

    def second_account(self, me, switch: bool):
        """A personal Claude account besides the office one the project runs with, and the switch."""
        self.users.save_claude(me, "personal", "sk-ant-oat01-y", "key-personal")
        sub = self.users.get(me).subscriptions[self.market.id]
        self.users.subscriptions.subscribe(me, dataclasses.replace(sub, settings=Settings(switch_claude=switch)))

    def worked_with(self, me) -> list[str]:
        """The label of the Claude account each run worked with, once the developer's runs are done."""
        labels = []

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            labels.append(actor.claude.label)
            return "done", "ok"

        pool = self.pool(fake_process)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        return labels

    def test_with_switching_on_a_run_starts_on_another_claude_account_and_the_page_says_which_is_active(self):
        me = self.developer()
        self.second_account(me, switch=True)
        self.nearly_used(me, 3600)
        labels = self.worked_with(me)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, ["personal"])
        user = self.users.get(me)
        usage = accounts.AccountService(self.cfg, self.users, mock.Mock(), mock.Mock()).claude_usage(user)
        page = home_page(self.cfg, user, [self.market], {}, self.runs.search(me, RunFilter()), "csrf", "nonce",
                         claude_usage=usage)
        self.assertIn(f"Active Claude account: <b>personal</b>. {ME} is at its limit: 98% of the 5-hour Claude limit is used",
                      page)
        self.assertIn('<small class="usage"><span class="tag warn">at its limit</span> 98% of the 5-hour Claude limit is used',
                      page)  # on the Accounts tab

    def test_with_switching_off_the_run_waits_for_its_own_claude_account(self):
        me = self.developer()
        self.second_account(me, switch=False)
        self.nearly_used(me, 0.5)
        labels = self.worked_with(me)
        self.wait_for(lambda: "not started yet" in self.runs.recent(me)[0]["message"])
        self.assertEqual(labels, [])
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, [ME])  # its own account, after the reset

    def test_with_every_claude_account_at_its_limit_the_run_waits_and_takes_the_first_to_reset(self):
        me = self.developer()
        self.second_account(me, switch=True)
        self.nearly_used(me, 3600)
        self.nearly_used(me, 0.5, "personal")
        labels = self.worked_with(me)
        self.wait_for(lambda: "your other Claude accounts are at their limits too" in self.runs.recent(me)[0]["message"])
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(labels, ["personal"])

    def test_a_run_waits_queued_while_the_claude_limit_is_nearly_used_and_starts_after_the_reset(self):
        me, started = self.developer(), []
        self.nearly_used(me, 0.5)
        pool = self.pool(fake_process_factory(started))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: "98% of the 5-hour Claude limit is used" in self.runs.recent(me)[0]["message"])
        self.assertEqual((self.runs.recent(me)[0]["status"], started), ("queued", []))  # nothing cloned, nothing asked
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(len(started), 1)

    def test_work_that_asks_claude_nothing_is_not_held(self):
        me = self.developer()
        self.nearly_used(me, 3600)
        pool = self.pool(fake_process_factory())
        self.assertIsNone(pool.submit(self.market, ["acme/fe#1"], me, work=lambda *a, **kw: ("done", "approved"),
                                      claude=False))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")

    def test_a_run_held_by_the_claude_limit_can_be_stopped(self):
        me, started = self.developer(), []
        self.nearly_used(me, 3600)
        pool = self.pool(fake_process_factory(started))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: "not started yet" in self.runs.recent(me)[0]["message"])
        self.assertTrue(pool.stop_run(self.runs.recent(me)[0]["id"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "stopped")
        self.assertEqual(started, [])


class BatchTest(QueueEnv):
    def opening(self, prs: dict[str, list[str]], seen: list):
        """A pipeline that opens the given PRs (repo names) per issue key, and hands them over."""
        def fake_process(keys, project, actor, cfg, jira, github, only=(), announce=None, **kw):
            seen.append((keys[0], only, bool(announce)))
            opened = [(project.repo(name), f"https://github.com/{project.repo(name).github}/pull/{keys[0][-1]}")
                      for name in prs.get(keys[0], [])]
            if opened and announce:
                announce(f"{keys[0]}: a bug", opened, keys[0])
            return ("done", "ok") if opened else ("failed", "Claude made no changes")
        return fake_process

    def test_runs_started_together_get_one_review_request_once_the_last_has_ended(self):
        me, seen = self.developer(), []
        pool = self.pool(self.opening({"MAR-1": ["fe"], "MAR-2": ["fe", "services"]}, seen), start=False)
        for key in ("MAR-1", "MAR-2", "MAR-3"):
            self.assertIsNone(pool.submit(self.market, [key], me, repos=("fe", "gone"), batch=7))
        with mock.patch.object(run_queue, "ask_for_reviews", return_value=("C0REVIEWS1", "9.9")) as ask, \
             mock.patch.object(run_queue, "ask_for_review") as ask_one:
            pool.start()
            self.wait_for(lambda: ask.called)
        ask.assert_called_once()  # MAR-3 failed: it is left out
        ask_one.assert_not_called()
        self.assertEqual([(title, repo.name, url.rsplit("/", 3)[1:]) for title, repo, url in ask.call_args.args[2]],
                         [("MAR-1: a bug", "fe", ["fe", "pull", "1"]), ("MAR-2: a bug", "fe", ["fe", "pull", "2"]),
                          ("MAR-2: a bug", "services", ["services", "pull", "2"])])
        self.assertEqual(sorted(seen), [(f"MAR-{n}", ("fe",), True) for n in (1, 2, 3)])  # only the project's own repos
        self.assertEqual(self.runs.batches(), [])  # asked once

    def test_a_batch_with_one_run_that_opened_prs_asks_as_for_a_run_by_itself(self):
        me, seen = self.developer(), []
        pool = self.pool(self.opening({"MAR-2": ["fe"]}, seen), start=False)
        for key in ("MAR-1", "MAR-2"):
            self.assertIsNone(pool.submit(self.market, [key], me, batch=8))
        with mock.patch.object(run_queue, "ask_for_reviews") as ask, mock.patch.object(run_queue, "ask_for_review") as ask_one:
            pool.start()
            self.wait_for(lambda: ask_one.called)
        ask.assert_not_called()
        self.assertEqual(ask_one.call_args.args[2], "MAR-2: a bug")
        self.assertEqual(sorted(seen), [("MAR-1", (), True), ("MAR-2", (), True)])


class NeverRunTest(QueueEnv):
    def test_work_found_only_by_its_sprint_starts_only_if_never_run_before(self):
        me = self.developer()
        pool = self.pool(fake_process_factory([]))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, automatic=True, never_run=True))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(pool.submit(self.market, ["MAR-1"], me, automatic=True, never_run=True), "already run before")
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, automatic=True))  # updated since: new work
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))


class SequenceTest(QueueEnv):
    def test_issues_run_one_after_another_each_after_the_delay(self):
        me, events, release = self.developer(), [], threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, **kw):
            events.append(("start", keys[0], time.monotonic()))
            if keys[0] == "MAR-1":
                release.wait(5)
            events.append(("end", keys[0], time.monotonic()))
            if keys[0] == "MAR-2":
                raise RuntimeError("tests still failing")  # a failed one does not stop the sequence
            return "done", "ok"

        pool = self.pool(fake_process)
        items = [(self.market, [f"MAR-{n}"], "", (), None) for n in (1, 2, 3)]
        self.assertEqual(pool.submit_sequence(items, me, 0.3), [None, None, None])
        self.wait_for(lambda: events)
        waiting = {r["issue_key"]: (r["status"], r["message"]) for r in self.runs.recent(me)}
        self.assertEqual(waiting["MAR-2"], ("queued", "starts after the issue before it, 0.3s later"))
        self.assertEqual([e[:2] for e in events], [("start", "MAR-1")])  # the others wait for it
        release.set()
        self.wait_for(lambda: all(r["status"] in ("done", "failed") for r in self.runs.recent(me)))
        self.assertEqual([e[:2] for e in events], [(what, f"MAR-{n}") for n in (1, 2, 3) for what in ("start", "end")])
        times = {e[:2]: e[2] for e in events}
        for before, after in (("MAR-1", "MAR-2"), ("MAR-2", "MAR-3")):
            self.assertGreaterEqual(times["start", after] - times["end", before], 0.3)
        self.assertEqual(sorted(r["status"] for r in self.runs.recent(me)), ["done", "done", "failed"])

    def test_an_issue_waiting_for_its_turn_can_be_stopped_and_the_next_still_runs(self):
        me, started, release = self.developer(), [], threading.Event()
        pool = self.pool(fake_process_factory(started, block=release))
        items = [(self.market, [f"MAR-{n}"], "", (), None) for n in (1, 2, 3)]
        self.assertEqual(pool.submit_sequence(items, me, 0), [None, None, None])
        self.wait_for(lambda: started)
        second = next(r["id"] for r in self.runs.recent(me) if r["issue_key"] == "MAR-2")
        self.assertTrue(pool.stop_run(second, me))
        self.wait_for(lambda: next(r for r in self.runs.recent(me) if r["id"] == second)["status"] == "stopped")
        release.set()
        self.wait_for(lambda: all(r["status"] in ("done", "stopped") for r in self.runs.recent(me)))
        self.assertEqual([keys for _, keys in started], [("MAR-1",), ("MAR-3",)])

    def test_the_most_urgent_issue_runs_first(self):
        me, started, release = self.developer(), [], threading.Event()
        pool = self.pool(fake_process_factory(started, block=release), workers=1)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: started)
        for key, urgency in (("MAR-2", 1), ("MAR-3", 4), ("MAR-4", 2), ("MAR-5", 4)):
            self.assertIsNone(pool.submit(self.market, [key], me, urgency=urgency))
        release.set()
        self.wait_for(lambda: len(started) == 5)
        self.assertEqual([keys[0] for _, keys in started], ["MAR-1", "MAR-3", "MAR-5", "MAR-4", "MAR-2"])

    def test_runs_work_outside_working_hours_and_leave_their_prs_for_them(self):
        me = self.developer()
        later = datetime.now(UTC) + timedelta(hours=2)
        hours = Settings(work_from=later.strftime("%H:%M"), work_until=(later + timedelta(hours=1)).strftime("%H:%M"),
                         work_days=settings_model.DAYS, work_timezone="UTC")
        sub = self.users.get(me).subscriptions[self.market.id]
        self.users.subscriptions.subscribe(me, dataclasses.replace(sub, settings=hours))
        holds = []

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, hold=None, **kw):
            holds.append(hold)
            hold(Publication(tuple(keys), keys[0], f"{keys[0]}: x", (Change("fe", "main", "new", "body"),)))
            return "done", "pushed; its PRs wait"

        held = HeldRepository(self.runs.db)
        pool = RunQueue(self.cfg, self.users, self.projects, self.runs, mock.Mock(), held=held)
        for p in (mock.patch.object(run_queue, "process_issues", fake_process), mock.patch.object(run_queue, "jira_client")):
            p.start()
            self.addCleanup(p.stop)
        pool.start()
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, automatic=True))  # started at once, whatever the hour
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        (row,) = held.waiting()
        self.assertEqual((row["kind"], row["branch"], row["run_id"]), ("prs", "MAR-1", self.runs.recent(me)[0]["id"]))
        # Run again before its hours: what it leaves replaces what the run before left.
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: len(holds) == 2 and self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(len(held.waiting()), 1)

    def test_a_sequence_goes_on_after_a_restart(self):
        me = self.developer()
        first = self.runs.add("MAR-1", me, self.market.id, "marketplace")
        self.runs.add("MAR-2", me, self.market.id, "marketplace", follows=first, delay=0.2)
        started = []
        pool = self.pool(fake_process_factory(started))  # start() queues again what was waiting
        self.wait_for(lambda: all(r["status"] == "done" for r in self.runs.recent(me)))
        self.assertEqual([keys for _, keys in started], [("MAR-1",), ("MAR-2",)])
        self.assertIsNotNone(pool)


class StopAndPauseTest(QueueEnv):
    def test_stop_runs_stops_a_run_in_progress(self):
        me = self.developer()
        started = []
        pool = self.pool(fake_process_factory(started, block=threading.Event()))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: started)
        self.assertEqual(pool.stop_runs(me), 1)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "stopped")
        self.assertEqual(pool.stop_runs(me), 0)

    def test_a_failed_or_stopped_issue_is_not_started_again_automatically(self):
        # Its failure comment updates the issue, which a webhook or the poller would take for new work.
        me, sara = self.developer(), self.developer("acc-sara")

        def failing(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, **kw):
            raise RuntimeError("tests still failing")

        pool = self.pool(failing)
        self.assertIsNone(pool.submit(self.market, ["MAR-1", "MAR-2"], me, automatic=True))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "failed")
        self.wait_for(lambda: pool.submit(self.market, ["MAR-3"], me) is None)  # the worker is free again
        self.assertIn("the last run of MAR-2 failed", pool.submit(self.market, ["MAR-2"], me, automatic=True))
        self.assertIsNone(pool.submit(self.market, ["MAR-22"], me, automatic=True))  # another issue
        self.assertIsNone(pool.submit(self.market, ["MAR-2"], me))  # by hand
        # Reassigned: it is new work for the other developer (once this developer's run is over).
        self.wait_for(lambda: pool.submit(self.market, ["MAR-2"], sara, automatic=True) is None)
        self.wait_for(lambda: all(r["status"] == "failed" for u in (me, sara) for r in self.runs.recent(u)))
        # One that asked the developer waits for their note, not for the ticket to change.
        run = self.runs.add("MAR-7", me, self.market.id, self.market.name)
        self.runs.update(run, "question", "ShadowDev needs your decision…")
        self.assertIn("the last run of MAR-7 asked you questions", pool.submit(self.market, ["MAR-7"], me, automatic=True))

    def test_questions_for_the_developer_come_back_with_their_answer(self):
        me = self.developer()
        seen = []

        def asking(keys, project, actor, cfg, jira, github, cancel=None, note="", on_model=None, on_pr=None, asked=None,
                   on_questions=None, **kw):
            seen.append((note, asked))
            if not note:
                on_questions({"MAR-1": ["Keep the old API?"]})
                return "question", "ShadowDev needs your decision…"
            return "done", "ok"

        pool = self.pool(asking)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "question")
        self.assertEqual(self.runs.asked(me, self.market.id, ["MAR-1", "MAR-2"]), {"MAR-1": ["Keep the old API?"]})
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me, note="MAR-1: yes, keep it."))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done")
        self.assertEqual(seen, [("", {}), ("MAR-1: yes, keep it.", {"MAR-1": ["Keep the old API?"]})])
        self.assertEqual(self.runs.asked(me, self.market.id, ["MAR-1"]), {})  # answered: the latest run is done

    def test_a_developer_stops_one_of_their_runs(self):
        me, sara = self.developer(), self.developer("acc-sara")
        started = []
        pool = self.pool(fake_process_factory(started, block=threading.Event()), workers=1)
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.assertIsNone(pool.submit(self.market, ["MAR-2"], me))  # waits behind MAR-1
        self.wait_for(lambda: started)
        running, queued = sorted(r["id"] for r in self.runs.recent(me))
        self.assertFalse(pool.stop_run(running, sara))  # not hers
        self.assertTrue(pool.stop_run(queued, me))  # never starts
        self.assertTrue(pool.stop_run(running, me))
        status = lambda run_id: next(r for r in self.runs.recent(me) if r["id"] == run_id)
        self.wait_for(lambda: status(running)["status"] == "stopped" and status(queued)["status"] == "stopped")
        self.assertEqual(status(queued)["message"], "stopped before it started")
        self.assertIn("the ticket was left as it was", status(running)["message"])
        self.assertFalse(pool.stop_run(running, me))  # finished
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))  # can run again
        again = max(r["id"] for r in self.runs.recent(me))
        self.assertTrue(pool.stop_run(again, me))
        self.wait_for(lambda: status(again)["status"] == "stopped")

    def test_stopping_a_projects_runs(self):
        me = self.developer()
        pool = self.pool(fake_process_factory(), start=False)
        pool.submit(self.market, ["MAR-1"], me)
        self.assertEqual(pool.stop_runs(project_ids={self.market.id + 1}), 0)
        self.assertEqual(pool.stop_runs(project_ids={self.market.id}), 1)

    def test_a_paused_developers_queued_runs_are_skipped(self):
        me = self.developer()
        self.users.set_paused(me, True)
        ran = []
        pool = self.pool(fake_process_factory(ran))
        self.assertIsNone(pool.submit(self.market, ["MAR-1"], me))
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "skipped")
        self.assertEqual((ran, self.runs.recent(me)[0]["message"]), ([], "automation is paused"))


class ShutdownTest(QueueEnv):
    def test_stopping_lets_runs_finish_and_leaves_the_queued_ones_for_the_next_start(self):
        me = self.developer()
        started, release = threading.Event(), threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            started.set()
            release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process, workers=1)
        pool.submit(self.market, ["MAR-1"], me)
        started.wait(5)
        pool.submit(self.market, ["MAR-2"], me)
        threading.Timer(0.3, release.set).start()
        self.assertEqual(pool.drain(30, threading.Event()), 0)  # MAR-1 finished by itself
        statuses = {r["issue_key"]: r["status"] for r in self.runs.recent(me)}
        self.assertEqual(statuses, {"MAR-1": "done", "MAR-2": "queued"})  # resumed at the next start
        self.assertEqual([r["issue_key"] for r in self.runs.resume_after_restart()], ["MAR-2"])

    def test_runs_still_working_when_time_is_up_are_stopped(self):
        me = self.developer()
        started = threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            started.set()
            while True:
                cancel.wait(0.05)

        pool = self.pool(fake_process, workers=1)
        pool.submit(self.market, ["MAR-1"], me)
        started.wait(5)
        hurry = threading.Event()
        hurry.set()  # stopped a second time: no waiting
        self.assertEqual(pool.drain(600, hurry), 1)
        self.assertEqual(self.runs.recent(me)[0]["status"], "stopped")

    def test_a_run_records_what_it_logged_and_what_claude_reported(self):
        me = self.developer()
        logger = logging.getLogger("shadowdev")  # at INFO, as the server sets it up
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            logging.getLogger("shadowdev").info("%s: Claude analysing", keys[0])
            runlog.add_usage(0.25, 4)
            runlog.add_usage(0.5, 6)
            return "done", "ok"

        self.pool(fake_process).submit(self.market, ["MAR-1"], me)
        self.wait_for(lambda: self.runs.recent(me)[0]["status"] == "done" and self.runs.recent(me)[0]["has_log"])
        run = self.runs.recent(me)[0]
        self.assertEqual((run["cost"], run["turns"]), (0.75, 10))
        self.assertGreater(run["seconds"], 0)
        self.assertRegex(self.runs.log(me, run["id"]), r"\d\d:\d\d:\d\d MAR-1: Claude analysing")
        self.assertIsNone(self.runs.log(me + 1, run["id"]))


class ProgressTest(QueueEnv):
    """A running run's step, for its progress bar, and what it logged so far, for its developer only."""

    def test_a_running_run_shows_its_step_and_its_log_so_far(self):
        me = self.developer()
        logger = logging.getLogger("shadowdev")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)
        working, release = threading.Event(), threading.Event()

        def fake_process(keys, project, actor, cfg, jira, github, cancel=None, **kw):
            timings = Timings(keys[0])
            with timings.step("clone fe"):
                pass
            with timings.step("Claude analysis"):
                logging.getLogger("shadowdev").info("%s: Claude analysing", keys[0])
                working.set()
                release.wait(5)
            return "done", "ok"

        pool = self.pool(fake_process)
        pool.submit(self.market, ["MAR-1"], me)
        self.assertTrue(working.wait(5))
        run = self.runs.recent(me)[0]
        self.assertEqual((run["status"], run["step"]), ("running", "Claude analysis"))
        self.assertRegex(pool.log(me, run["id"]), r"MAR-1: Claude analysing")
        self.assertIsNone(pool.log(me + 1, run["id"]))  # another developer's
        # Live logs: what it logged so far, then only what is new; never to another developer.
        ((run_id, total, lines),) = pool.live(me, {})
        self.assertEqual(run_id, run["id"])
        self.assertTrue(any("MAR-1: Claude analysing" in line for line in lines))
        self.assertEqual(pool.live(me, {run_id: total}), [(run_id, total, [])])
        self.assertEqual(pool.live(me + 1, {}), [])
        release.set()
        self.wait_for(lambda: self.runs.recent(me)[0]["has_log"])
        run = self.runs.recent(me)[0]
        self.assertEqual((run["status"], run["step"]), ("done", ""))
        self.assertRegex(pool.log(me, run["id"]), r"MAR-1: Claude analysing")  # kept when it ended

    def test_a_live_reader_gets_only_the_lines_it_has_not_seen(self):
        record = runlog.Record()
        for n in range(runlog.MAX_LINES + 5):
            record.add(f"line {n}")
        total, lines = record.since(0)
        self.assertEqual((total, len(lines), lines[0]), (runlog.MAX_LINES + 5, runlog.MAX_LINES, "line 5"))  # the ones kept
        record.add("new")
        self.assertEqual(record.since(total), (total + 1, ["new"]))
        self.assertEqual(record.since(total + 1), (total + 1, []))
        self.assertEqual(record.since(total + 9), (total + 1, []))  # a reader ahead of it gets nothing

    def test_a_step_is_kept_only_while_the_run_is_running(self):
        me = self.developer()
        run_id = self.runs.add("MAR-1", me, self.market.id, "marketplace")
        self.runs.set_step(run_id, "clone fe")
        self.assertEqual(self.runs.recent(me)[0]["step"], "")  # queued
        before = self.runs.version(me)
        self.runs.update(run_id, "running")
        self.runs.set_step(run_id, "clone fe")
        self.assertEqual(self.runs.recent(me)[0]["step"], "clone fe")
        self.assertNotEqual(self.runs.version(me), before)  # what the page's live updates watch

    def test_steps_outside_a_run_are_only_timed(self):
        steps = []
        with Timings("MAR-1").step("clone fe"):  # a dry run: no run record
            pass
        with runlog.recording(on_step=steps.append), Timings("MAR-1").step("clone fe"):
            pass
        self.assertEqual(steps, ["clone fe"])

    def test_every_pipeline_step_has_a_phase(self):
        source = Path(pipeline.__file__).read_text() + Path(pipeline.__file__).with_name("review.py").read_text()
        names = re.findall(r'timings\.step\(f?"([^"]+)"\)', source)
        self.assertGreater(len(names), 20)
        for name in names:
            sample = re.sub(r"\{[^}]*\}", "x", name)
            self.assertIsNotNone(phase(sample), name)
        self.assertEqual(phase("Claude analysis"), (3, "Analysing"))
        self.assertEqual(len(PHASES), 6)


class RunsWebTest(WebTest):
    """Runs: queueing and running issues, the run page, Recent runs, Live logs, and the queue."""

    def test_running_a_range_of_issues(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        self.users.save_github(uid, ME, "gh", ME, ME, f"{ME}@x")
        self.users.save_claude(uid, ME, "sk-ant-oat01-x", f"key-{ME}")
        join(self.users, uid, market, ME)  # bugs only

        def issue(key):
            if key == "MAR-2":
                raise HTTPError("Jira GET … -> 404", 404)
            return {"fields": {"assignee": {"accountId": "acc-sara" if key in ("MAR-3", "MAR-9") else ME},
                               "issuetype": {"name": "Story" if key == "MAR-4" else "Bug"}}}
        self.jira.get_issue.side_effect = issue

        # Of a range, what isn't yours to run is skipped, and listed as a skipped run.
        self.assertEqual(self.run_form(csrf, {"issue_keys": "mar-1 to mar-5, MAR-7"})[1], "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-1"], uid), ("marketplace", ["MAR-5"], uid),
                                       ("marketplace", ["MAR-7"], uid)])
        skipped = self.runs.recent(uid)[0]
        self.assertEqual((skipped["issue_key"], skipped["status"]), ("MAR-2, MAR-3, MAR-4", "skipped"))
        self.assertEqual(skipped["message"], "Not run, from the range: MAR-2 was not found in Jira, or you cannot see it. "
                                             "MAR-3 is not assigned to you. MAR-4 is a Story you don't automate in marketplace.")
        # Left out of the range: not run, not looked up, not listed as skipped.
        self.queued.clear()
        self.jira.get_issue.reset_mock()
        self.assertEqual(self.run_form(csrf, {"issue_keys": "MAR-5 to MAR-8 except MAR-6"})[1], "/?ok=queued")
        self.assertEqual([keys for _, keys, _ in self.queued], [["MAR-5"], ["MAR-7"], ["MAR-8"]])
        self.assertNotIn("MAR-6", [c.args[0] for c in self.jira.get_issue.call_args_list])
        self.assertIn("MAR-13 is left out, but not among the issues", self.run_form(csrf, {"issue_keys": "MAR-5 to MAR-8, !MAR-13"})[2])
        # A key typed by itself must be yours.
        self.queued.clear()
        self.assertIn("MAR-9 is not assigned to you.",
                      self.run_form(csrf, {"issue_keys": "MAR-5 to MAR-6, MAR-9"})[2])
        self.assertEqual(self.queued, [])
        self.assertIn("None of those issues is yours to run",
                      self.run_form(csrf, {"issue_keys": "MAR-2 to MAR-4"})[2])
        self.assertIn("a range stays in one Jira project", self.run_form(csrf, {"issue_keys": "MAR-1 to HR-4"})[2])
        # Together: one job, at most ten issues on one branch.
        self.assertEqual(self.run_form(csrf, {"issue_keys": "MAR-5 to MAR-8", "grouping": "together"})[1],
                         "/?ok=queued")
        self.assertEqual(self.queued, [("marketplace", ["MAR-5", "MAR-6", "MAR-7", "MAR-8"], uid)])
        self.assertIn("At most 10 issues on one branch; 12 are yours",
                      self.run_form(csrf, {"issue_keys": "MAR-10 to MAR-21", "grouping": "together"})[2])

    def test_answering_the_questions_of_a_run(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1, MAR-2", uid, market.id, "marketplace")
        self.runs.update(run_id, "question", "ShadowDev needs your decision before it works on MAR-1: …")
        self.runs.set_questions(run_id, {"MAR-1": ["Keep the old <API>?"]})
        page = self.request("GET", "/")[2]
        self.assertIn(f'<a class="button small" href="/?tab=runs&amp;answer={run_id}#run-form">Answer</a>', page)
        page = self.request("GET", f"/?answer={run_id}")[2]
        self.assertIn('name="issue_keys" value="MAR-1, MAR-2"', page)
        self.assertIn("<li>MAR-1: Keep the old &lt;API&gt;?</li>", page)
        self.assertIn(" autofocus>MAR-1: \n</textarea>", page)
        self.assertIn('value="together" checked', page)  # they were worked on together
        # Someone else's run, or one that asked nothing, fills in nothing.
        other = add_developer(self.users, "acc-sara")
        theirs = self.runs.add("MAR-3", other, market.id, "marketplace")
        self.runs.update(theirs, "question", "…")
        self.runs.set_questions(theirs, {"MAR-3": ["Secret question?"]})
        for answer in (theirs, "x"):
            page = self.request("GET", f"/?answer={answer}")[2]
            self.assertNotIn("Secret question", page)
            self.assertIn('name="issue_keys" value=""', page)

    def test_stop_button(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace", "Only the <API>.")
        self.runs.set_model(run_id, "haiku → sonnet (medium)")
        page = self.request("GET", "/")[2]
        self.assertIn(f'<input type="hidden" name="run_id" value="{run_id}">', page)  # queued: has a Stop button
        self.assertIn("<small>Model: haiku → sonnet (medium)</small><small>Your note: Only the &lt;API&gt;.</small>", page)
        # The page keeps Recent runs up to date by fetching its rows.
        self.assertIn("connect-src 'self'", self.headers["Content-Security-Policy"])
        status, _, rows = self.request("GET", "/runs")
        self.assertEqual(status, 200)
        self.assertTrue(rows.startswith('<div class="results-bar">') and f'name="run_id" value="{run_id}"' in rows
                        and csrf in rows)
        self.assertIn(f'href="{OFFICE.url}/browse/MAR-1"', page)
        self.assertEqual(self.request("POST", "/stop", {"csrf": csrf, "run_id": str(run_id)})[1], "/?ok=stopping")
        self.pool.stop_run.assert_called_once_with(run_id, uid)
        self.pool.stop_run.return_value = False
        self.assertIn("already finished, or is not yours", self.request("POST", "/stop", {"csrf": csrf, "run_id": "999"})[2])
        self.assertIn("already finished, or is not yours", self.request("POST", "/stop", {"csrf": csrf, "run_id": "x"})[2])
        self.runs.update(run_id, "done", "ok")
        finished = self.request("GET", "/")[2]
        self.assertNotIn(f'<input type="hidden" name="run_id" value="{run_id}">', finished)  # finished: no Stop button
        self.assertIn(f'name="run_id" value="{run_id}" form="delete-runs"', finished)  # but it can be selected for deletion

    def test_recent_runs_are_searched_filtered_and_paged_by_the_address(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        for n in range(1, 26):
            self.runs.update(self.runs.add(f"MAR-{n}", uid, market.id, "marketplace"), "failed" if n == 7 else "done")
        page = self.request("GET", "/")[2]
        self.assertIn('id="run-filter"', page)
        self.assertIn("1–20 of 25 runs", page)
        self.assertIn('href="/?tab=recent&amp;page=2" rel="next" data-nav', page)
        self.assertIn('<option value="marketplace">marketplace</option>', page)
        # The same search on the page and in what the page fetches; values are escaped.
        for path in ("/?tab=recent&q=MAR-7", "/runs?q=MAR-7"):
            found = self.request("GET", path)[2]
            self.assertIn("1–1 of 1 run<", found)
            self.assertIn(">MAR-7<", found)
            self.assertNotIn(">MAR-8<", found)
        self.assertIn('value="&lt;b&gt;"', self.request("GET", "/?tab=recent&q=<b>")[2])
        self.assertIn("No runs match this search", self.request("GET", "/runs?q=<b>")[2])
        second = self.request("GET", "/runs?page=2")[2]
        self.assertIn("21–25 of 25 runs", second)
        self.assertIn('rel="prev" data-nav', second)
        self.assertIn(">MAR-1<", second)
        failed = self.request("GET", "/runs?status=failed&kind=issue&project=marketplace")[2]
        self.assertIn(">MAR-7<", failed)
        self.assertIn('data-query="tab=recent&amp;status=failed&amp;project=marketplace&amp;kind=issue"', failed)
        self.assertIn("1–1 of 1 run<", failed)
        self.assertIn("No runs match", self.request("GET", "/runs?kind=pr")[2])
        # The filters sit in a popup with Apply and Clear; the ones in use show as chips that remove them.
        self.assertIn('<details class="filter-menu" id="filter-menu">', page)
        self.assertIn("<button>Apply</button>", page)
        self.assertNotIn('class="chips"', page)
        filtered = self.request("GET", "/?tab=recent&q=MAR&status=failed&kind=issue")[2]
        self.assertIn('data-count>2</span>', filtered)
        self.assertIn('href="/?tab=recent&amp;q=MAR" data-clear>Clear</a>', filtered)  # Clear keeps the search
        self.assertIn('href="/?tab=recent&amp;q=MAR&amp;kind=issue" data-nav aria-label="Remove filter Status: Failed"',
                      filtered)

    def test_finished_runs_are_selected_and_deleted_but_not_active_or_other_peoples(self):
        self.sign_in()
        csrf = self.csrf()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        other = add_developer(self.users, "acc-sara")
        add = lambda key, status, owner=uid: (lambda run: (self.runs.update(run, status), run)[1])(
            self.runs.add(key, owner, market.id, "marketplace"))
        done, failed, running, queued, hers = (add("MAR-1", "done"), add("MAR-2", "failed"), add("MAR-3", "running"),
                                               add("MAR-4", "queued"), add("MAR-5", "done", other))
        page = self.request("GET", "/")[2]
        for run in (done, failed):  # finished: a checkbox, bound to the delete form
            self.assertIn(f'<input type="checkbox" name="run_id" value="{run}" form="delete-runs"', page)
        for run in (running, queued):  # active: stopped first, not deleted
            self.assertNotIn(f'name="run_id" value="{run}" form="delete-runs"', page)
        self.assertIn('id="pick-all"', page)
        self.assertIn('action="/runs/delete"', page)

        self.assertIn("Select the runs to delete", self.request("POST", "/runs/delete", {"csrf": csrf})[2])
        self.assertIn("still queued or running",
                      self.request("POST", "/runs/delete", {"csrf": csrf, "run_id": [str(running), str(queued)]})[2])
        # Selected finished ones go, with junk and other people's ids ignored; active ones in the same request stay.
        form = {"csrf": csrf, "run_id": [str(done), str(failed), str(running), str(hers), "x", "../1"]}
        self.assertEqual(self.request("POST", "/runs/delete", form)[1], "/?ok=runs-deleted")
        self.assertEqual({r["id"] for r in self.runs.recent(uid)}, {running, queued})
        self.assertEqual([r["id"] for r in self.runs.recent(other)], [hers])
        self.assertIn("Selected runs removed", self.request("GET", "/?ok=runs-deleted")[2])
        # Forms need the session's CSRF token.
        self.request("POST", "/runs/delete", {"csrf": "forged", "run_id": [str(hers)]})
        self.assertEqual([r["id"] for r in self.runs.recent(other)], [hers])

    def test_a_run_keeps_its_log_and_cost_for_its_developer_only(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace")
        self.runs.update(run_id, "done", "ok")
        self.runs.set_record(run_id, 125, 0.4321, 12, "10:00:00 MAR-1: Claude analysing\n10:01:00 <b>tests</b> pass")
        page = self.request("GET", "/")[2]
        self.assertIn(f"Took: 2m05s · 12 Claude turns · $0.43 as Claude counts it · "
                      f'<a href="/runs/log?run={run_id}"', page)
        status, _, text = self.request("GET", f"/runs/log?run={run_id}")
        self.assertEqual((status, self.headers["Content-Type"], self.headers["X-Content-Type-Options"]),
                         (200, "text/plain; charset=utf-8", "nosniff"))
        self.assertEqual(text, "10:00:00 MAR-1: Claude analysing\n10:01:00 <b>tests</b> pass")
        sara = add_developer(self.users, "acc-sara")
        theirs = self.runs.add("MAR-2", sara, market.id, "marketplace")
        self.runs.set_record(theirs, 1, 0, 0, "secret of sara")
        for path in (f"/runs/log?run={theirs}", "/runs/log?run=x", "/runs/log"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.runs.delete_finished(uid, [run_id])  # its log goes with it
        self.assertEqual(self.request("GET", f"/runs/log?run={run_id}")[0], 404)
        self.cookies.clear()
        self.assertEqual(self.request("GET", f"/runs/log?run={theirs}")[0], 404)  # signed out

    def test_live_logs_show_only_the_developers_own_working_runs(self):
        self.assertEqual(self.request("GET", "/logs/live")[0], 401)
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace")
        asked = []
        self.pool.live = lambda user_id, seen: asked.append((user_id, seen)) or [(run_id, 3, ["12:00:00 <b>hi</b>"])]
        status, _, body = self.request("GET", "/logs/live?seen=7:120,x:1,8:y," + ",".join(f"{n}:0" for n in range(1000, 1060)))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"runs": [{"id": run_id, "label": "MAR-1", "project": "marketplace",
                                                      "total": 3, "lines": ["12:00:00 <b>hi</b>"]}]})
        user_id, seen = asked[0]
        self.assertEqual((user_id, seen[7], "x" in seen, 8 in seen), (uid, 120, False, False))  # only run:count pairs
        self.assertLessEqual(len(seen), settings_routes.MAX_LIVE_RUNS)
        self.pool.live = lambda user_id, seen: [(run_id + 99, 1, ["not a run of theirs"])]
        self.assertEqual(json.loads(self.request("GET", "/logs/live")[2]), {"runs": []})
        page = self.request("GET", "/?tab=logs")[2]
        self.assertIn('data-tab="logs" aria-selected="true"', page)
        self.assertIn('<div class="terminal" data-terminal role="log"', page)

    def test_a_running_run_shows_its_progress(self):
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        run_id = self.runs.add("MAR-1", uid, market.id, "marketplace")
        self.runs.update(run_id, "running")
        self.runs.set_step(run_id, "test fe (attempt 2)")
        rows = self.request("GET", "/runs")[2]
        self.assertIn('<progress max="6" value="5" title="Testing" aria-label="Testing: phase 5 of 6"></progress>'
                      "<small>test fe (attempt 2)</small>", rows)
        self.assertIn(f'<a href="/runs/log?run={run_id}" target="_blank" rel="noopener">Log so far</a>', rows)
        self.runs.update(run_id, "done", "ok")
        self.assertNotIn("<progress", self.request("GET", "/runs")[2])

    def stream(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        conn.request("GET", "/runs/events", headers={"Cookie": cookie})
        self.addCleanup(conn.close)
        return conn.getresponse()

    def test_live_updates_say_when_a_run_changes(self):
        for name, value in (("CHECK_SECONDS", 0.02), ("BEAT_SECONDS", 0.2)):
            patch = mock.patch.object(settings_routes, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(self.stream().status, 401)  # signed out
        self.sign_in()
        uid = self.me().id
        market = add_project(self.projects, uid, "marketplace", ["MAR"])
        resp = self.stream()
        self.assertEqual((resp.status, resp.getheader("Content-Type"), resp.getheader("Cache-Control")),
                         (200, "text/event-stream; charset=utf-8", "no-store"))
        self.assertEqual(resp.readline(), b"retry: 5000\n")
        resp.readline()
        self.assertEqual(resp.readline(), b": still here\n")  # nothing changed yet
        resp.readline()
        self.runs.add("MAR-1", uid, market.id, "marketplace")
        self.assertEqual(resp.readline(), b"event: runs\n")
        # Another developer's runs are not theirs to hear of.
        sara = add_developer(self.users, "acc-sara")
        self.runs.add("MAR-2", sara, market.id, "marketplace")
        resp.readline(), resp.readline()
        self.assertEqual(resp.readline(), b": still here\n")
        # Too many open: refused, and the page asks by itself instead.
        with mock.patch.object(settings_routes, "MAX_STREAMS_EACH", 1):
            self.assertEqual(self.stream().status, 503)
        # The server stopping ends it.
        self.app.stopping.set()
        self.assertEqual(resp.read(), b"\n")
        self.assertEqual(self.stream().read(), b"retry: 5000\n\n")

    def test_a_closed_stream_frees_its_place_at_once(self):
        with mock.patch.object(settings_routes, "CHECK_SECONDS", 0.02), \
                mock.patch.object(settings_routes, "MAX_STREAMS_EACH", 1):
            self.sign_in()
            for _ in range(3):  # the page replaced again and again: each closes its stream and opens another
                resp = self.stream()
                self.assertEqual(resp.status, 200)
                resp.close()
                time.sleep(0.2)
            self.assertEqual(settings_routes.STREAMS._open.total(), 0)

    def test_live_updates_end_with_the_session(self):
        with mock.patch.object(settings_routes, "CHECK_SECONDS", 0.02):
            self.sign_in()
            resp = self.stream()
            self.assertEqual(resp.readline(), b"retry: 5000\n")
            self.request("POST", "/logout", {"csrf": self.csrf()})
            self.assertEqual(resp.read(), b"\n")
