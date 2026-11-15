import dataclasses
import unittest

from shadowdev.automation import rules
from shadowdev.models.issue import Issue
from shadowdev.models.project import Project, Repo

ME = "acc-majid"  # the developer a run posts as: "Majid" and "M" below


def comments(*bodies):
    return {"comment": {"comments": [{"author": {"displayName": who, "accountId": ME if who in ("Majid", "M") else f"acc-{who}"},
                                      "body": body} for who, body in bodies]}}


def repos_of(*names: str) -> list[Repo]:
    return [Repo(name, f"acme/{name}", "", "release/", None, None) for name in names]


class RulesTest(unittest.TestCase):
    def test_issue_keys_and_ranges_as_typed(self):
        keys = lambda text: rules.parse_keys(text)
        self.assertEqual(keys("mar-1, MAR-5;MAR-7  MAR-1"), (["MAR-1", "MAR-5", "MAR-7"], set()))
        for typed in ("MAR-1 to MAR-4", "mar-1 to 4", "MAR-1..MAR-4", "MAR-1 - MAR-4", "MAR-1–MAR-4"):
            self.assertEqual(keys(typed), (["MAR-1", "MAR-2", "MAR-3", "MAR-4"], {"MAR-1", "MAR-2", "MAR-3", "MAR-4"}), typed)
        # Mixed with keys typed by themselves, which are not "only in a range".
        self.assertEqual(keys("MAR-9, MAR-2 to MAR-4, MAR-3, MY_APP-1"),
                         (["MAR-9", "MAR-2", "MAR-3", "MAR-4", "MY_APP-1"], {"MAR-2", "MAR-4"}))
        for bad, error in (("MAR-1 to HR-3", "one Jira project"), ("MAR-9 to MAR-2", "ends before it starts"),
                           ("MAR-1 to MAR-60", "at most 50"), ("MAR-1 to", "Not an issue key: TO"),
                           ("MAR-1, ../etc", "Not an issue key: ../ETC")):
            with self.assertRaisesRegex(ValueError, error):
                rules.parse_keys(bad)

    def test_the_repos_a_run_may_change(self):
        repos = repos_of("fe", "auth", "notification", "documents")
        self.assertEqual(rules.chosen_repos(repos, ["auth", "documents"]), ("auth", "documents"))
        self.assertEqual(rules.chosen_repos(repos, ["documents", "fe", "other"]), ("fe", "documents"))  # in the project's order
        self.assertEqual(rules.chosen_repos(repos, ["fe", "auth", "notification", "documents"]), ())  # all: Claude decides
        self.assertEqual(rules.chosen_repos(repos_of("app"), []), ())  # a project's only repo needs no choosing
        for chosen in ([], ["other"]):
            with self.assertRaisesRegex(ValueError, "choose the repos to change: fe, auth, notification, documents"):
                rules.chosen_repos(repos, chosen)

    def test_issues_left_out_of_a_range(self):
        ten = [f"MAR-{n}" for n in range(1, 11)]
        without = lambda *gone: [k for k in ten if k not in gone]
        for typed in ("mar-1 to mar-10 except mar-3, mar-7", "MAR-1 to MAR-10 skip MAR-3 MAR-7", "MAR-1 to 10 without MAR-3;MAR-7",
                      "MAR-1 to MAR-10, !MAR-3, -MAR-7", "!MAR-3 MAR-1..MAR-10 -MAR-7"):
            keys, ranged = rules.parse_keys(typed)
            self.assertEqual(keys, without("MAR-3", "MAR-7"), typed)
            self.assertEqual(ranged, set(without("MAR-3", "MAR-7")), typed)
        self.assertEqual(rules.parse_keys("MAR-1 to MAR-10 except MAR-3 to MAR-5, MAR-9")[0],
                         without("MAR-3", "MAR-4", "MAR-5", "MAR-9"))  # a range left out
        self.assertEqual(rules.parse_keys("MAR-1 to MAR-10, !MAR-3 to MAR-8")[0], without(*[f"MAR-{n}" for n in range(3, 9)]))
        self.assertEqual(rules.parse_keys("MAR-1 - MAR-3")[0], ["MAR-1", "MAR-2", "MAR-3"])  # still a range, not "-MAR-3"
        self.assertEqual(rules.parse_keys("NOT-1, SKIP-2 to SKIP-4 except SKIP-3")[0], ["NOT-1", "SKIP-2", "SKIP-4"])  # projects so named
        with self.assertRaisesRegex(ValueError, "MAR-13 is left out, but not among the issues"):
            rules.parse_keys("MAR-1 to MAR-10 except MAR-3, MAR-13")  # a typo is not silently ignored
        with self.assertRaisesRegex(ValueError, "Not an issue key: THREE"):
            rules.parse_keys("MAR-1 to MAR-10 except three")

    def test_keys_fixed_together_make_a_short_branch_name_that_says_which_they_are(self):
        self.assertEqual(rules.compact_keys(["MAR-1"]), "MAR-1")
        self.assertEqual(rules.compact_keys(["MAR-2", "MAR-1"]), "MAR-1_MAR-2")  # two stay as they were
        self.assertEqual(rules.compact_keys([f"MAR-{n}" for n in range(10, 0, -1)]), "MAR-1-to-MAR-10")
        self.assertEqual(rules.compact_keys(["MAR-1", "MAR-2", "MAR-3", "MAR-4", "MAR-7", "MAR-8", "MAR-12", "HR-2"]),
                         "HR-2_MAR-1-to-MAR-4_MAR-7_MAR-8_MAR-12")
        self.assertEqual(rules.compact_keys(["MAR-1", "MAR-2", "MAR-3", "MAR-9"], ", ", " to "), "MAR-1 to MAR-3, MAR-9")
        for keys in (["MAR-1"], ["MAR-1", "MAR-2"], [f"MAR-{n}" for n in range(1, 11)], ["MY_APP-1", "MY_APP-2", "MY_APP-3", "MAR-5"]):
            self.assertEqual(sorted(rules.branch_keys(rules.compact_keys(keys))), sorted(keys))
        self.assertEqual(rules.branch_keys("MAR-2_MAR-1_MAR-3"), ["MAR-2", "MAR-1", "MAR-3"])  # an older group branch
        for other in ("main", "feature/MAR-1-totals", "MAR-1-to-HR-4", "MAR-9-to-MAR-2", "MAR-1-to-MAR-900", "MAR-1_"):
            self.assertIsNone(rules.branch_keys(other), other)

    def test_questions_for_the_reporter_and_waiting_for_their_answer(self):
        asked = rules.questions_comment(["Which page?", "Since when?"], "712020:abc-1")
        self.assertEqual(asked, "[~accountid:712020:abc-1] Before I start on this, I need a few answers:\n"
                                "# Which page?\n# Since when?\nA reply here is enough; I'll pick it up from there.")
        self.assertNotIn("accountid", rules.questions_comment(["Which page?"], "x] [~accountid:evil"))  # not an account ID
        self.assertEqual(rules.clean_questions(["  Which\n page? ", "", 3, "x" * 400] + ["more"] * 9),
                         ["Which page?", "x" * rules.MAX_QUESTION_CHARS, "more", "more", "more"])

        self.assertFalse(rules.awaiting_answer({}, ME))
        self.assertTrue(rules.awaiting_answer(comments(("Sara", "It breaks."), ("Majid", asked)), ME))
        answered = comments(("Majid", asked), ("Sara", "The checkout page, since Monday."))
        self.assertFalse(rules.awaiting_answer(answered, ME))
        self.assertEqual(rules.questions_asked(comments(("Majid", asked), ("Sara", "x"), ("Majid", asked)), ME), 2)
        # The same words from someone else are theirs, not the run's questions.
        self.assertFalse(rules.awaiting_answer(comments(("Sara", asked)), ME))
        # Claude sees the earlier questions with the answers; the run's other comments stay out,
        # and so do those of earlier versions, which named the tool.
        issue = Issue("MAR-1", comments(("Majid", "*Root cause:*\nx"), ("Majid", "*Root cause (ShadowDev):* x"),
                                        ("Majid", asked), ("Sara", "The checkout page, since Monday."),
                                        ("Sara", "*Root cause:* it is the cache, I think.")), "bug", me=ME)
        self.assertEqual(issue.comments, [("Majid (questions asked earlier)", asked),
                                          ("Sara", "The checkout page, since Monday."),
                                          ("Sara", "*Root cause:* it is the cache, I think.")])
        old = comments(("Majid", "ShadowDev needs more information before working on this issue."))
        self.assertTrue(rules.awaiting_answer(old, ME))

    def test_an_issue_is_reopened_when_sent_back_with_a_comment_after_its_prs(self):
        prs = rules.pr_links_comment([(*repos_of("fe"), "release/15", "https://github.com/acme/fe/pull/1")], [])

        def fields(status, category, *bodies):
            return {"status": {"name": status, "statusCategory": {"key": category}}, **comments(*bodies)}

        reopened = lambda *args: rules.reopened(fields(*args), "In Review", ME)
        self.assertTrue(reopened("In Progress", "indeterminate", ("M", prs), ("QA", "Still wrong on iOS.")))
        self.assertTrue(reopened("Reopened", "new", ("QA", "first"), ("M", prs), ("QA", "Still wrong.")))
        self.assertTrue(reopened("In Progress", "indeterminate", ("M", prs), ("M", "Checked: it still breaks on iOS.")))  # me, by hand
        self.assertFalse(reopened("In Progress", "indeterminate", ("M", prs)))  # no reason given
        self.assertFalse(reopened("In Progress", "indeterminate", ("QA", "x"), ("M", prs)))
        self.assertFalse(reopened("In Progress", "indeterminate", ("M", prs),
                                  ("M", "I couldn't finish this one yet; I'll come back to it.")))
        self.assertFalse(reopened("In Progress", "indeterminate", ("M", prs), ("M", "ShadowDev pushed…")))  # an older version's
        self.assertFalse(reopened("In review", "indeterminate", ("M", prs), ("QA", "Looks odd.")))
        self.assertFalse(reopened("Done", "done", ("M", prs), ("QA", "Shipped.")))
        self.assertFalse(reopened("In Progress", "indeterminate", ("QA", "Never had a PR.")))
        self.assertFalse(reopened("In Progress", "indeterminate", ("QA", prs), ("QA", "Copied the table.")))  # not mine

    def test_latest_release_branch_compares_numerically(self):
        branches = ["release/1.9.0", "release/1.10.0", "release/1.2", "release/next", "main",
                    "release/082-background-jobs-single-runner"]
        self.assertEqual(rules.latest_release_branch(branches, "release/"), "release/1.10.0")
        self.assertEqual(rules.latest_release_branch(["release/0.12", "release/14", "release/v13"], "release/"), "release/14")
        self.assertIsNone(rules.latest_release_branch(["main"], "release/"))

    def test_the_prs_are_a_table_by_repo_on_tickets(self):
        fe, be = repos_of("fe", "services")
        prs = [(fe, "release/15", "https://github.com/acme/market-place-fe/pull/123"),
               (be, "release/14", "https://github.com/acme/market-place-services/pull/456")]
        self.assertEqual(rules.pr_links_comment(prs, ["MAR-2", "MAR-3"]),
                         "||Repo||Base branch||Pull request||\n"
                         "|*fe*|release/15|[PR #123|https://github.com/acme/market-place-fe/pull/123]|\n"
                         "|*services*|release/14|[PR #456|https://github.com/acme/market-place-services/pull/456]|\n\n"
                         "Fixed together with MAR-2, MAR-3.")
        # The table is the run's own comment: not given back to Claude as a colleague's.
        issue = Issue("MAR-1", comments(("Sara", "Also with discounts."), ("Majid", rules.pr_links_comment(prs, []))),
                      "bug", me=ME)
        self.assertEqual(issue.comments, [("Sara", "Also with discounts.")])

    def test_fix_version_by_the_projects_pick(self):
        # MAR's real versions: Rel 15 wins over Rel 14, "Rel 1 - MVP" and "Rel 0.10".
        names = ["Rel 0.5", "Rel 1 - MVP", "0.4", "Rel 0.6", "Rel 0.9", "Rel 10", "Rel 11", "Rel 0.10",
                 "Rel 12", "Rel 14", "Rel 13", "Rel 15"]
        versions = [{"name": n, "released": False} for n in names] + [{"name": "Rel 16", "released": True},
                                                                       {"name": "Rel 17", "archived": True}]
        self.assertEqual(rules.fix_version(versions, "highest"), "Rel 15")
        self.assertEqual(rules.fix_version(versions), "0.4")  # the next release: the lowest
        self.assertEqual(rules.fix_version(versions, "branch", ["release/13", "main"]), "Rel 13")
        self.assertEqual(rules.fix_version(versions, "branch", ["main"]), "0.4")  # none numbered so: the next
        self.assertEqual(rules.fix_version([{"name": "1.9.0"}, {"name": "1.10.0"}], "highest"), "1.10.0")
        self.assertEqual(rules.fix_version([{"name": "1.10.0"}, {"name": "1.9.0"}]), "1.9.0")
        self.assertIsNone(rules.fix_version([{"name": "Backlog"}, {"name": "1.0", "released": True}]))

    def test_urgency_by_jira_priority(self):
        urgency = lambda name: rules.urgency({"priority": {"name": name}} if name else {})
        self.assertEqual([urgency(n) for n in ("Highest", "Blocker", "High", "Medium", "Low", "Trivial", "Custom", None)],
                         [4, 4, 3, 2, 1, 0, 2, 2])

    def test_issue_kinds_and_modes(self):
        project = Project(1, "p", 1, "c", "u", frozenset({"MAR"}), None, ())
        kind = lambda name, p=project: p.issue_kind({"issuetype": {"name": name}})
        self.assertEqual([kind(n) for n in ("Bug", "Task", "Story", "Sub-task", "Epic", "Incident")],
                         ["bug", "task", "task", "task", None, None])
        ops = dataclasses.replace(project, bug_types=("Bug", "Incident"), task_types=("Service request",))
        self.assertEqual([kind(n, ops) for n in ("incident", "Service Request", "Task")], ["bug", "task", None])
        self.assertTrue(rules.wanted("bug", "bugs"))
        self.assertFalse(rules.wanted("task", "bugs"))
        self.assertTrue(rules.wanted("task", "all"))
        self.assertFalse(rules.wanted(None, "all"))


if __name__ == "__main__":
    unittest.main()
