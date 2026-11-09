import unittest

from shadowdev.automation import rules


class RulesTest(unittest.TestCase):
    def test_latest_release_branch_compares_numerically(self):
        branches = ["release/1.9.0", "release/1.10.0", "release/1.2", "release/next", "main",
                    "release/082-background-jobs-single-runner"]
        self.assertEqual(rules.latest_release_branch(branches, "release/"), "release/1.10.0")
        self.assertEqual(rules.latest_release_branch(["release/0.12", "release/14", "release/v13"], "release/"), "release/14")
        self.assertIsNone(rules.latest_release_branch(["main"], "release/"))

    def test_fix_version_is_the_highest_open_release(self):
        # MAR's real versions: Rel 15 wins over Rel 14, "Rel 1 - MVP" and "Rel 0.10".
        names = ["Rel 0.5", "Rel 1 - MVP", "0.4", "Rel 0.6", "Rel 0.9", "Rel 10", "Rel 11", "Rel 0.10",
                 "Rel 12", "Rel 14", "Rel 13", "Rel 15"]
        versions = [{"name": n, "released": False} for n in names] + [{"name": "Rel 16", "released": True},
                                                                       {"name": "Rel 17", "archived": True}]
        self.assertEqual(rules.latest_fix_version(versions), "Rel 15")
        self.assertEqual(rules.latest_fix_version([{"name": "1.9.0"}, {"name": "1.10.0"}]), "1.10.0")
        self.assertIsNone(rules.latest_fix_version([{"name": "Backlog"}, {"name": "1.0", "released": True}]))

    def test_issue_kinds_and_modes(self):
        kind = lambda name: rules.issue_kind({"issuetype": {"name": name}})
        self.assertEqual([kind(n) for n in ("Bug", "Task", "Story", "Sub-task", "Epic")],
                         ["bug", "task", "task", "task", None])
        self.assertTrue(rules.wanted("bug", "bugs"))
        self.assertFalse(rules.wanted("task", "bugs"))
        self.assertTrue(rules.wanted("task", "all"))
        self.assertFalse(rules.wanted(None, "all"))


if __name__ == "__main__":
    unittest.main()
