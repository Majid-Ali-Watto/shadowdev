"""End-to-end pipeline test against real git repos, with Jira, GitHub and Claude faked.
Setup and test commands really run, in the bubblewrap sandbox."""
import contextlib
import dataclasses
import io
import json
import re
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from shadowdev.automation import dryrun, pipeline, verify
from shadowdev.automation.cancel import Cancelled
from shadowdev.models.project import Project, Repo
from shadowdev.models.user import Actor, ClaudeAccount, GitHubAccount, Subscription
from shadowdev.repositories.database import Database
from shadowdev.repositories.projects import ProjectRepository
from shadowdev.repositories.users import UserRepository
from tests.helpers import OFFICE, add_developer, make_cfg

ME = "acc-majid"
BUGGY = "def add(a, b):\n    return a - b\n"
WRONG_FIX = "def add(a, b):\n    return a * b\n"
RIGHT_FIX = "def add(a, b):\n    return a + b\n"
# The fix adds this test; it is new, so it must pass.
NEW_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5, f'add(2, 3) == {add(2, 3)}'\n"
# Already failing on the release branch: must not block the fix.
LEGACY_TEST = "def test_legacy():\n    assert False, 'broken before ShadowDev'\n"
# Minimal test runner: runs test_*.py, writes a JUnit report, exits 1 on any failure.
RUNNER = """import glob, importlib, os, sys
sys.path.insert(0, '.')
cases, failed = [], 0
for f in sorted(glob.glob('test_*.py')):
    mod = importlib.import_module(f[:-3])
    for name in [n for n in dir(mod) if n.startswith('test_')]:
        try:
            getattr(mod, name)()
            cases.append(f'<testcase classname="{f}" name="{name}"/>')
        except Exception as e:
            failed += 1
            print(f'FAILED {f}::{name}: {e}')
            cases.append(f'<testcase classname="{f}" name="{name}"><failure>{e}</failure></testcase>')
open(os.path.join(os.environ['SHADOWDEV_REPORT_DIR'], 'run.xml'), 'w').write('<testsuite>' + ''.join(cases) + '</testsuite>')
sys.exit(1 if failed else 0)
"""


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def issue_keys(schema):
    return schema["properties"]["issues"]["items"]["properties"]["key"]["enum"]


def answer(analysis, key="PROJ-1"):
    return {"repos": ["app"], "files": ["app/calc.py"], "issues": [{"key": key, "analysis": analysis}]}


class FakeClaude:
    """Stands in for ClaudeSession: analysis names the "app" repo; each change writes the next
    version of calc.py, and creates a new test file."""

    instances = []

    def __init__(self, cwd, env, fixes, model=None, effort=None, **kw):
        self.cwd = cwd
        self.model, self.effort = model, effort
        self.env = env
        # Like the real CLI: a transcript of the session, which holds the code it read.
        transcript = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / str(cwd).replace("/", "-") / "session.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "user", "cwd": str(cwd), "message": "calc.py: " + BUGGY}) + "\n")
        self.fixes = list(fixes)
        self.prompts = []
        self.label = "fake"
        FakeClaude.instances.append(self)

    def ask(self, prompt, tools, schema):
        self.prompts.append((prompt, tools))
        if "issues" in schema["properties"]:
            assert (self.cwd / "app" / "calc.py").exists() and (self.cwd / "docs" / "README").exists()
            return {"repos": ["app"], "files": ["app/calc.py"],
                    "issues": [{"key": k, "analysis": f"add() subtracts instead of adding ({k})"} for k in issue_keys(schema)]}
        (self.cwd / "app" / "calc.py").write_text(self.fixes.pop(0))
        (self.cwd / "app" / "test_add.py").write_text(NEW_TEST)
        return {"summary": "Changed add() to use + instead of -, and added a test for it."}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.app_origin, self.release_head = self.make_repo(
            "app", {"calc.py": BUGGY, "test_legacy.py": LEGACY_TEST, "run_tests.py": RUNNER},
            old_releases=["release/1.9", "release/082-not-a-release"], latest="release/1.10",
        )
        self.docs_origin, _ = self.make_repo("docs", {"README": "docs\n"}, old_releases=[], latest="release/3")

        def repo(name):
            return Repo(name=name, github=f"acme/{name}", description=f"the {name} repo", release_prefix="release/",
                        setup="touch .setup-done", test="python3 -B run_tests.py")

        self.project = Project(id=1, name="proj", owner_id=1, cloud_id=OFFICE.cloud_id, site_url=OFFICE.url,
                               jira_projects=frozenset({"PROJ"}), root_cause_field=None, repos=(repo("app"), repo("docs")))
        self.cfg = make_cfg(self.tmp, max_fix_attempts=3)
        self.actor = Actor(
            user_id=1, display_name="Majid", jira_account_id=ME,
            github=GitHubAccount(1, "office", "majid", "gh-tok", "Majid Ali", "majid@example.com"),
            claude=ClaudeAccount(1, "office", "sk-ant-oat01-x", "k-office"), mode="bugs", base_branches={},
        )
        self.jira = mock.Mock()
        self.jira.get_issue.return_value = {"fields": {
            "summary": "Totals are wrong", "description": "2 + 3 shows -1",
            "issuetype": {"name": "Bug"}, "assignee": {"accountId": ME}, "project": {"key": "PROJ"},
        }}
        self.jira.move_along.side_effect = lambda key, path: (path[-1], list(path))
        self.jira.project_versions.return_value = [
            {"name": "Rel 1.9", "released": True}, {"name": "Rel 1.10", "released": False}, {"name": "Rel 1.11", "released": False},
        ]
        self.github = mock.Mock()
        self.github.can_push.return_value = True
        self.github.create_pr.return_value = "https://github.com/o/r/pull/1"
        FakeClaude.instances = []
        # The model is chosen by a rating of the issue; here it is rated "standard" unless a test says otherwise.
        self.choose_model = pipeline.choose_model
        patcher = mock.patch.object(pipeline, "choose_model", lambda session, issues, note, label: ("sonnet", "medium"))
        patcher.start()
        self.addCleanup(patcher.stop)
        verify.BASELINES.clear()  # keyed by repo name and commit, which repeat across tests

    def make_repo(self, name, files, old_releases, latest):
        """Bare origin: main and old_releases point at the first commit, latest at one commit after."""
        origin = self.tmp / f"{name}.git"
        sh(self.tmp, "git", "init", "--bare", "-b", "main", str(origin))
        seed = self.tmp / f"{name}-seed"
        sh(self.tmp, "git", "clone", str(origin), str(seed))
        for path, content in files.items():
            (seed / path).write_text(content)
        (seed / ".gitignore").write_text(".setup-done\n__pycache__/\n")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-m", "init")
        sh(seed, "git", "push", "origin", "HEAD:main", *[f"HEAD:{r}" for r in old_releases])
        (seed / "NOTES").write_text("latest\n")
        sh(seed, "git", "add", "-A")
        sh(seed, "git", "commit", "-m", "latest")
        sh(seed, "git", "push", "origin", f"HEAD:{latest}")
        return origin, sh(seed, "git", "rev-parse", "HEAD")

    def local_github(self):
        """"GitHub" is the local bare repos: acme/app -> <tmp>/app.git."""
        return mock.patch.object(pipeline.gitops, "repo_url", lambda owner_repo: str(self.tmp / f"{owner_repo.split('/')[1]}.git"))

    def run_pipeline(self, fixes, keys=("PROJ-1",), **kw):
        with mock.patch.object(pipeline, "ClaudeSession", lambda cwd, env, **kw: FakeClaude(cwd, env, fixes, **kw)), \
             self.local_github():
            return pipeline.process_issues(list(keys), self.project, self.actor, self.cfg, self.jira, self.github, **kw)

    def code_left_on_server(self):
        transcripts = list((self.cfg.data_dir / "claude").glob("*/projects/*/*.jsonl"))
        workspaces = list(self.cfg.worktree_root.iterdir()) if self.cfg.worktree_root.exists() else []
        return workspaces + transcripts

    def test_fixes_bug_after_self_heal_and_opens_pr_as_the_developer(self):
        status, message = self.run_pipeline([WRONG_FIX, RIGHT_FIX])
        self.assertEqual(status, "done")
        self.assertIn("app into release/1.10: https://github.com/o/r/pull/1", message)

        # Branch named after the bug, cut from the latest numeric release (1.10, not 1.9 or 082-...),
        # committed as the developer.
        self.assertEqual(sh(self.app_origin, "git", "rev-parse", "PROJ-1~1"), self.release_head)
        self.assertEqual(sh(self.app_origin, "git", "show", "PROJ-1:calc.py") + "\n", RIGHT_FIX)
        self.assertEqual(sh(self.app_origin, "git", "log", "-1", "--format=%an <%ae>", "PROJ-1"), "Majid Ali <majid@example.com>")
        self.assertIn("test_add.py", sh(self.app_origin, "git", "ls-tree", "--name-only", "PROJ-1"))  # new file
        self.assertNotIn(".setup-done", sh(self.app_origin, "git", "ls-tree", "--name-only", "PROJ-1"))

        # Jira: root cause comment, fix version matching the release branch, PR link.
        self.jira.set_fix_versions.assert_called_once_with("PROJ-1", ["Rel 1.11"])  # highest open release
        # Jira status: In Progress when work starts, In Review once the PR is open.
        self.assertEqual([c.args for c in self.jira.move_along.call_args_list],
                         [("PROJ-1", ["In Progress"]), ("PROJ-1", ["In Review"])])
        comments = [c.args[1] for c in self.jira.comment.call_args_list]
        self.assertIn("Root cause", comments[0])
        self.assertIn("add() subtracts", comments[0])
        self.assertIn("app into release/1.10: https://github.com/o/r/pull/1", comments[-1])

        # One PR, for the repo that changed, into its release branch, on the GitHub repo from origin.
        self.github.create_pr.assert_called_once()
        owner_repo = self.github.create_pr.call_args.args[0]
        self.assertTrue(owner_repo.endswith("/app"))
        self.assertEqual(self.github.create_pr.call_args.kwargs["base"], "release/1.10")
        self.assertEqual(self.github.create_pr.call_args.kwargs["head"], "PROJ-1")
        self.assertIn("1 already failing", self.github.create_pr.call_args.kwargs["body"])

        # The untouched repo: no branch pushed. No code left on the server: no clone, no
        # baseline, no test reports, no Claude transcript.
        self.assertEqual(sh(self.docs_origin, "git", "ls-remote", "--heads", ".", "PROJ-1"), "")
        self.assertEqual(self.code_left_on_server(), [])

        # Claude ran with the token and config dir of the project's Claude account; analysis is read-only; the new
        # test failure (not the legacy one) was fed back.
        claude = FakeClaude.instances[0]
        self.assertEqual(claude.env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-x")
        self.assertEqual(claude.env["CLAUDE_CONFIG_DIR"], str(self.cfg.data_dir / "claude" / "k-office"))
        self.assertNotIn("GH_TOKEN", claude.env)
        self.assertEqual((claude.model, claude.effort), ("sonnet", "medium"))
        self.assertNotIn("developer_note", claude.prompts[0][0])
        self.assertEqual(len(claude.prompts), 3)  # analyse, change, retry
        self.assertEqual(claude.prompts[0][1], "Read,Glob,Grep")
        self.assertIn("- app/ (branch release/1.10)", claude.prompts[0][0])
        self.assertIn("test_add.py :: test_add", claude.prompts[2][0])
        self.assertNotIn("test_legacy.py :: test_legacy", claude.prompts[2][0].split("<test_output>")[1].split("[app]")[0])
        self.assertEqual(sorted(p.name for p in self.cfg.worktree_root.iterdir()), [])

        # A repeat webhook for the same bug is a no-op.
        self.assertEqual(self.run_pipeline([])[0], "skipped")
        self.assertEqual(len(FakeClaude.instances), 1)

    def test_uses_the_developers_base_branch(self):
        self.actor = dataclasses.replace(self.actor, base_branches={"app": "main"})
        status, _ = self.run_pipeline([RIGHT_FIX])
        self.assertEqual(status, "done")
        self.assertEqual(self.github.create_pr.call_args.kwargs["base"], "main")
        self.assertEqual(sh(self.app_origin, "git", "rev-parse", "PROJ-1~1"), sh(self.app_origin, "git", "rev-parse", "main"))
        self.jira.set_fix_versions.assert_called_once_with("PROJ-1", ["Rel 1.11"])

    def test_the_model_follows_the_rating_and_the_note_reaches_claude(self):
        rated = []

        class Triage:
            def __init__(self, cwd, env, model=None, **kw):
                self.model = model

            def ask(self, prompt, tools, schema):
                rated.append((self.model, prompt, tools))
                return {"complexity": "simple"}

        note = "The sign is wrong in add(); leave sub() alone."
        with mock.patch.object(pipeline, "choose_model", self.choose_model), \
             mock.patch.object(pipeline, "ClaudeSession",
                               lambda cwd, env, model=None, **kw: Triage(cwd, env, model) if not rated
                               else FakeClaude(cwd, env, [RIGHT_FIX], model=model, **kw)), self.local_github():
            status, _ = pipeline.process_issues(["PROJ-1"], self.project, self.actor, self.cfg, self.jira, self.github,
                                                note=note)
        self.assertEqual(status, "done")
        (model, prompt, tools), = rated
        self.assertEqual((model, tools), ("haiku", ""))  # the small model, from the text alone
        self.assertIn("Totals are wrong", prompt)
        self.assertIn(note, prompt)
        claude = FakeClaude.instances[0]
        self.assertEqual((claude.model, claude.effort), ("haiku", None))  # a simple issue: the small model
        self.assertIn(f"<developer_note>\n{note}\n</developer_note>", claude.prompts[0][0])
        for text in (self.jira.comment.call_args_list[0].args[1], self.github.create_pr.call_args.kwargs["body"]):
            self.assertNotIn("leave sub() alone", text)  # the note is for Claude only

    def test_an_issue_that_cannot_be_rated_runs_with_the_default_model(self):
        def session(model=None):
            return mock.Mock(ask=mock.Mock(side_effect=RuntimeError("claude failed: model not available")))

        with self.assertLogs("shadowdev", "WARNING"):
            self.assertEqual(self.choose_model(session, [], "", "PROJ-1"), (None, None))
        answers = {"complex": (None, None), "standard": ("sonnet", "medium"), "nonsense": (None, None)}
        for rating, expected in answers.items():
            self.assertEqual(self.choose_model(lambda model=None: mock.Mock(ask=lambda *a: {"complexity": rating}),
                                               [], "", "PROJ-1"), expected)

    def test_tasks_when_the_developer_automates_them(self):
        self.jira.get_issue.return_value["fields"]["issuetype"] = {"name": "Story"}
        self.assertEqual(self.run_pipeline([])[0], "skipped")  # mode "bugs"

        self.actor = dataclasses.replace(self.actor, mode="all")
        status, _ = self.run_pipeline([RIGHT_FIX])
        self.assertEqual(status, "done")
        prompts = FakeClaude.instances[0].prompts
        self.assertIn("For each task, study the code and give a concise implementation plan", prompts[0][0])
        self.assertIn("create new", prompts[1][0])
        self.assertIn("Implementation plan", self.jira.comment.call_args_list[0].args[1])
        self.jira.set_fix_versions.assert_not_called()
        self.assertIn("**Plan:** add() subtracts", self.github.create_pr.call_args.kwargs["body"])

    def test_asks_again_when_the_analysis_is_a_placeholder(self):
        # MAR-2105: Claude's real answers were rejected (it put <repos>/<files> inside the text),
        # then it sent {"analysis": "test"}, which went to Jira as the root cause.
        real_ask = FakeClaude.ask
        answers = iter([answer("Real cause in calc.py.</analysis>\n<repos>[\"app\"]</repos>"), answer("test")])

        def ask(self, prompt, tools, schema):
            if "issues" in schema["properties"]:
                self.prompts.append((prompt, tools))
                return next(answers, None) or real_ask(self, prompt, tools, schema)
            return real_ask(self, prompt, tools, schema)

        with mock.patch.object(FakeClaude, "ask", ask):
            status, _ = self.run_pipeline([RIGHT_FIX])
        self.assertEqual(status, "done")
        prompts = [p for p, _ in FakeClaude.instances[0].prompts]
        self.assertIn("contains <repos>/<files> tags", prompts[1])
        self.assertIn("placeholder", prompts[2])
        self.assertIn("add() subtracts", self.jira.comment.call_args_list[0].args[1])

    def test_fixes_several_issues_together_on_one_branch(self):
        issues = {
            "PROJ-1": {"summary": "Totals are wrong", "issuetype": {"name": "Bug"}},
            "PROJ-2": {"summary": "Sum is negative", "issuetype": {"name": "Bug"}},
        }
        self.jira.get_issue.side_effect = lambda key: {"fields": {
            **issues[key], "description": "2 + 3 shows -1", "assignee": {"accountId": ME}, "project": {"key": "PROJ"}}}
        status, message = self.run_pipeline([RIGHT_FIX], keys=("PROJ-1", "PROJ-2"))
        self.assertEqual(status, "done")

        # One branch, one Claude session, one PR, covering both issues.
        self.assertEqual(sh(self.app_origin, "git", "show", "PROJ-1_PROJ-2:calc.py") + "\n", RIGHT_FIX)
        self.assertEqual(len(FakeClaude.instances), 1)
        self.github.create_pr.assert_called_once()
        pr = self.github.create_pr.call_args.kwargs
        self.assertEqual((pr["head"], pr["title"]), ("PROJ-1_PROJ-2", "PROJ-1, PROJ-2: 2 issues fixed together"))
        self.assertIn("### PROJ-1: Totals are wrong\n**Root cause:** add() subtracts instead of adding (PROJ-1)", pr["body"])
        self.assertIn("### PROJ-2: Sum is negative\n**Root cause:** add() subtracts instead of adding (PROJ-2)", pr["body"])
        self.assertIn('<issue key="PROJ-2" type="Bug">', FakeClaude.instances[0].prompts[0][0])

        # Each ticket gets its own root cause, fix version, status moves and PR comment.
        for key in ("PROJ-1", "PROJ-2"):
            self.jira.set_fix_versions.assert_any_call(key, ["Rel 1.11"])
            self.assertIn((key, ["In Review"]), [c.args for c in self.jira.move_along.call_args_list])
        comments = {c.args[0]: c.args[1] for c in self.jira.comment.call_args_list if "opened" in c.args[1]}
        self.assertIn("Fixed together with PROJ-2.", comments["PROJ-1"])
        self.assertIn("Fixed together with PROJ-1.", comments["PROJ-2"])

        # Later, PROJ-1 alone (e.g. a webhook when it is edited) counts as already handled.
        status, message = self.run_pipeline([], keys=("PROJ-1",))
        self.assertEqual((status, message), ("skipped", "PROJ-1: already handled on branch PROJ-1_PROJ-2"))

    def test_too_long_an_analysis_is_sent_back_to_be_concise(self):
        real_ask = FakeClaude.ask
        answers = iter([answer("The cause is in calc.py. " * 80)])

        def ask(self, prompt, tools, schema):
            if "issues" in schema["properties"]:
                self.prompts.append((prompt, tools))
                return next(answers, None) or real_ask(self, prompt, tools, schema)
            return real_ask(self, prompt, tools, schema)

        with mock.patch.object(FakeClaude, "ask", ask):
            self.assertEqual(self.run_pipeline([RIGHT_FIX])[0], "done")
        self.assertIn("keep it concise, under 1200", FakeClaude.instances[0].prompts[1][0])
        self.assertIn("2 to 5 sentences", FakeClaude.instances[0].prompts[0][0])  # asked for up front

    def test_runs_on_the_same_base_commit_share_one_baseline(self):
        with self.assertLogs("shadowdev", "INFO") as logs:
            self.assertEqual(self.run_pipeline([RIGHT_FIX], keys=("PROJ-1",))[0], "done")
            self.assertEqual(self.run_pipeline([RIGHT_FIX], keys=("PROJ-3",))[0], "done")
        text = "\n".join(logs.output)
        self.assertEqual(text.count("app baseline on release/1.10, tests: $"), 1)
        self.assertIn("PROJ-3: app baseline on release/1.10 (", text)
        self.assertIn("already measured; reusing it", text)

    def test_each_step_and_the_whole_run_are_timed(self):
        with self.assertLogs("shadowdev", "INFO") as logs:
            self.assertEqual(self.run_pipeline([WRONG_FIX, RIGHT_FIX])[0], "done")
        text = "\n".join(logs.output)
        for step in ("Jira and GitHub checks", "clone app", "Claude analysis", "Claude change",
                     "test app (attempt 1)", "Claude fix (attempt 1)", "test app (attempt 2)", "commit, push and PR app"):
            self.assertRegex(text, rf"PROJ-1: step {re.escape(step)} took \d")
        self.assertRegex(text, r"PROJ-1: done in [\d.ms]+ \(Jira and GitHub checks [\d.ms]+ · .*commit, push and PR app")

    def test_trailing_markup_is_trimmed_not_rejected(self):
        # MAR-2105 rerun: a correct analysis, with "</analysis>\n</invoke>" left at its end.
        real_ask = FakeClaude.ask
        analysis = "add() subtracts instead of adding, in calc.py.</analysis>\n</invoke>\n"

        def ask(self, prompt, tools, schema):
            if "issues" in schema["properties"]:
                self.prompts.append((prompt, tools))
                return answer(analysis)
            return real_ask(self, prompt, tools, schema)

        with mock.patch.object(FakeClaude, "ask", ask):
            self.assertEqual(self.run_pipeline([RIGHT_FIX])[0], "done")
        self.assertFalse([p for p, _ in FakeClaude.instances[0].prompts if "cannot be used" in p])  # not asked again
        root_cause = self.jira.comment.call_args_list[0].args[1]
        self.assertTrue(root_cause.endswith("add() subtracts instead of adding, in calc.py."), root_cause)

    def test_fails_when_claude_never_gives_a_usable_analysis(self):
        with mock.patch.object(FakeClaude, "ask", lambda self, p, t, s: answer("test")):
            with self.assertRaisesRegex(RuntimeError, "no usable analysis after 3 tries"):
                self.run_pipeline([])
        self.jira.comment.assert_not_called()
        self.jira.set_field.assert_not_called()

    def test_a_workflow_that_refuses_the_move_does_not_stop_the_work(self):
        self.jira.move_along.side_effect = pipeline.HTTPError("no transition", 400)
        status, _ = self.run_pipeline([RIGHT_FIX])
        self.assertEqual(status, "done")
        self.github.create_pr.assert_called_once()

    def test_gives_up_without_pushing_when_tests_keep_failing(self):
        status, message = self.run_pipeline([WRONG_FIX] * 3)
        self.assertEqual(status, "failed")
        self.github.create_pr.assert_not_called()
        self.assertEqual(sh(self.app_origin, "git", "ls-remote", "--heads", ".", "PROJ-1"), "")
        self.assertIn("could not produce a change", self.jira.comment.call_args.args[1])
        self.assertIn("nothing was pushed. Its last attempt: Changed add()", self.jira.comment.call_args.args[1])
        self.assertEqual(self.code_left_on_server(), [])  # failed runs leave no code either

    def test_a_crashing_run_leaves_no_code_and_unrelated_transcripts_stay(self):
        other = self.cfg.data_dir / "claude" / "someone" / "projects" / "-elsewhere" / "s.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text(json.dumps({"cwd": "/elsewhere"}) + "\n")
        self.jira.set_fix_versions.side_effect = RuntimeError("Jira is down")
        with self.assertRaisesRegex(RuntimeError, "Jira is down"):
            self.run_pipeline([RIGHT_FIX])
        self.assertEqual(self.code_left_on_server(), [other])

    def test_refuses_repos_the_developer_cannot_push_to(self):
        self.github.can_push.return_value = False
        with self.assertRaisesRegex(RuntimeError, "cannot push"):
            self.run_pipeline([RIGHT_FIX])
        self.assertFalse((self.cfg.worktree_root / "1-PROJ-1").exists())

    def test_baseline_without_report_is_a_setup_error(self):
        self.project = Project(**{**self.project.__dict__, "repos": tuple(
            Repo(**{**r.__dict__, "test": "false"}) for r in self.project.repos)})
        with self.assertRaisesRegex(RuntimeError, "fail without writing a JUnit report"):
            self.run_pipeline([RIGHT_FIX])

    def test_skips_issues_that_are_not_the_developers(self):
        fields = self.jira.get_issue.return_value["fields"]
        for change in ({"issuetype": {"name": "Epic"}},
                       {"assignee": {"accountId": "acc-someone-else"}},
                       {"project": {"key": "OTHER"}}):  # e.g. moved to another Jira project
            with self.subTest(change=change):
                with mock.patch.dict(fields, change):
                    self.assertEqual(self.run_pipeline([])[0], "skipped")
                self.assertEqual(FakeClaude.instances, [])

    def test_dry_run_tests_the_change_and_publishes_nothing(self):
        db = Database(self.tmp / "data" / "shadowdev.db", Fernet.generate_key().decode())
        users, projects = UserRepository(db), ProjectRepository(db)
        me = add_developer(users, ME, github="gh-tok", claude="sk-ant-oat01-x")
        pid = projects.save(dataclasses.replace(self.project, id=0, owner_id=me))
        user = users.get(me)
        users.subscribe(me, Subscription(pid, user.jira_accounts[0].id, user.github_accounts[0].id,
                                         user.claude_accounts[0].id, "bugs", {"app": "main"}))

        def run(fixes):
            out = io.StringIO()
            with mock.patch.object(pipeline, "ClaudeSession", lambda cwd, env, **kw: FakeClaude(cwd, env, fixes, **kw)), \
                 self.local_github(), contextlib.redirect_stdout(out):
                dryrun.dry_run(self.cfg, users, projects, None, ME, "Task", "Totals are wrong", "2 + 3 shows -1")
            return out.getvalue()

        out = run([WRONG_FIX, RIGHT_FIX])
        self.assertIn("Result: done: dry run: tests pass", out)
        self.assertIn("Jira comment that would be posted on PROJ-0", out)  # a Task: the plan
        self.assertIn("?? test_add.py", out)  # the new file Claude created
        self.assertRegex(out.rstrip().splitlines()[-1], r"^===== END ===== dry run PROJ-0 finished after [\d.ms]+$")
        worktree = self.cfg.worktree_root / f"{pid}-PROJ-0" / "app"
        self.assertEqual((worktree / "calc.py").read_text(), RIGHT_FIX)
        # Cut from the developer's base branch, and nothing committed or pushed.
        self.assertEqual(sh(worktree, "git", "rev-parse", "HEAD"), sh(self.app_origin, "git", "rev-parse", "main"))
        self.assertEqual(sh(self.app_origin, "git", "ls-remote", "--heads", ".", "PROJ-0"), "")
        self.assertEqual(len(FakeClaude.instances[0].prompts), 3)  # analyse, change, retry after the wrong fix

        # Only the change is kept (no baseline, reports or transcript); the next dry run replaces it.
        self.assertEqual([p.name for p in self.code_left_on_server()], [f"{pid}-PROJ-0"])
        self.assertIn("Result: done", run([RIGHT_FIX]))
        self.assertEqual([p.name for p in self.code_left_on_server()], [f"{pid}-PROJ-0"])

    def with_repos(self, **changes):
        self.project = dataclasses.replace(self.project, repos=tuple(
            dataclasses.replace(r, **changes) for r in self.project.repos))

    def test_commands_that_change_the_code_stop_the_run(self):
        # The owner's test command (or a dependency it runs) must not slip code into a member's PR.
        self.with_repos(test="python3 -B run_tests.py; s=$?; echo 'import os' >> calc.py; exit $s")
        with self.assertRaisesRegex(RuntimeError, r"app: its setup/test commands changed files that would be committed \(calc.py\)"):
            self.run_pipeline([RIGHT_FIX])
        self.github.create_pr.assert_not_called()
        self.assertEqual(sh(self.app_origin, "git", "ls-remote", "--heads", ".", "PROJ-1"), "")
        self.assertEqual(self.code_left_on_server(), [])

    def test_a_repo_without_tests_ships_untested_and_says_so(self):
        self.with_repos(test=None, setup=None)
        status, _ = self.run_pipeline([WRONG_FIX])  # nothing checks it
        self.assertEqual(status, "done")
        self.assertIn("**Not tested:**", self.github.create_pr.call_args.kwargs["body"])

    def test_without_release_branches_the_default_branch_is_the_base(self):
        self.with_repos(release_prefix="nope/")
        self.assertEqual(self.run_pipeline([RIGHT_FIX])[0], "done")
        self.assertEqual(self.github.create_pr.call_args.kwargs["base"], "main")

    def test_commands_see_only_their_own_runs_code(self):
        mine = self.cfg.worktree_root / "PROJ-1" / "app"
        reports = self.cfg.worktree_root / "PROJ-1.shadowdev" / "reports"
        other = self.cfg.worktree_root / "PROJ-2" / "app"
        for d in (mine, reports, other):
            d.mkdir(parents=True)
            (d / "code.py").write_text("x = 1\n")
        ok, output = verify.run_command(
            mine, f'ls "{self.cfg.worktree_root}"; cat code.py; echo y > new.py; ls "{reports}"; ls "{other}" 2>&1 || true',
            self.cfg, "test command")
        self.assertTrue(ok, output)
        lines = output.splitlines()
        self.assertEqual(sorted(lines[:2]), ["PROJ-1", "PROJ-1.shadowdev"])  # PROJ-2 is not there
        self.assertEqual(lines[2:4], ["x = 1", "code.py"])
        self.assertIn("No such file or directory", lines[4])
        self.assertEqual((mine / "new.py").read_text(), "y\n")  # its own files are writable

    def test_planted_git_hooks_and_config_never_run(self):
        # Code run by tests (or a commit) could plant these; ShadowDev runs git there outside the sandbox.
        repo = self.tmp / "planted"
        sh(self.tmp, "git", "init", "-q", str(repo))
        marker = self.tmp / "pwned"
        hook = f"#!/bin/sh\ntouch {marker}\n"
        for name in ("pre-commit", "post-commit", "commit-msg"):
            (repo / ".git" / "hooks" / name).write_text(hook)
            (repo / ".git" / "hooks" / name).chmod(0o755)
        (self.tmp / "fsmonitor.sh").write_text(hook)
        (self.tmp / "fsmonitor.sh").chmod(0o755)
        sh(repo, "git", "config", "core.fsmonitor", str(self.tmp / "fsmonitor.sh"))
        (repo / "a.txt").write_text("a\n")
        pipeline.gitops.git(repo, "status", "--porcelain")
        pipeline.gitops.commit_all(repo, "msg", "Majid", "m@x")
        self.assertFalse(marker.exists())
        self.assertEqual(sh(repo, "git", "log", "-1", "--format=%an"), "Majid")

    def test_a_clones_git_dir_is_read_only_in_the_sandbox(self):
        clone = self.cfg.worktree_root / "PROJ-1" / "app"
        clone.mkdir(parents=True)
        sh(clone, "git", "init", "-q")
        ok, output = verify.run_command(
            clone, "touch .git/hooks/pre-commit 2>&1; echo x > .git/config 2>&1; touch ok.txt && echo wrote-file",
            self.cfg, "test command")
        self.assertIn("Read-only file system", output)
        self.assertIn("wrote-file", output)
        self.assertFalse((clone / ".git" / "hooks" / "pre-commit").exists())
        self.assertTrue((clone / "ok.txt").exists())

    def test_a_stopped_run_writes_nothing_and_leaves_no_code(self):
        token = verify.CancelToken()
        real_ask = FakeClaude.ask

        def ask(self, prompt, tools, schema):
            result = real_ask(self, prompt, tools, schema)
            token.cancel()  # the developer deletes their account while Claude analyses
            return result

        with mock.patch.object(FakeClaude, "ask", ask), self.assertRaises(Cancelled):
            with mock.patch.object(pipeline, "ClaudeSession", lambda cwd, env, **kw: FakeClaude(cwd, env, [RIGHT_FIX])), \
                 self.local_github():
                pipeline.process_issues(["PROJ-1"], self.project, self.actor, self.cfg, self.jira, self.github, cancel=token)
        self.jira.set_fix_versions.assert_not_called()
        self.jira.comment.assert_not_called()
        self.github.create_pr.assert_not_called()
        self.assertEqual(sh(self.app_origin, "git", "ls-remote", "--heads", ".", "PROJ-1"), "")
        self.assertEqual(self.code_left_on_server(), [])

    def test_stopping_kills_a_running_command(self):
        token = verify.CancelToken()
        threading.Timer(0.3, token.cancel).start()
        started = time.monotonic()
        with self.assertRaises(Cancelled):
            verify.run_command(self.tmp, "sleep 30", self.cfg, "test command", token)
        self.assertLess(time.monotonic() - started, 10)

    def test_commands_cannot_see_shadowdev_secrets(self):
        self.cfg.data_dir.mkdir()
        (self.cfg.data_dir / "shadowdev.db").write_text("tokens")
        with mock.patch.dict(os.environ, {"SHADOWDEV_SECRET_KEY": "key"}):
            ok, output = verify.run_command(
                self.tmp, f'ls -A "{self.cfg.data_dir}"; echo "key=$SHADOWDEV_SECRET_KEY"; echo "x=$EXTRA"',
                self.cfg, "test command", EXTRA="y")
        self.assertTrue(ok, output)
        self.assertEqual(output.split(), ["key=", "x=y"])


if __name__ == "__main__":
    unittest.main()
