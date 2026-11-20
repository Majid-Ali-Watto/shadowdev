"""Leaving a project: an owner hands it on to the longest-standing other member, in the same step; an
owner who is the only member is refused (they delete it). Also the IDs read from forms and URLs."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shadowdev.app import build_app
from shadowdev.services import accounts
from shadowdev.services.errors import UserError
from tests.helpers import add_developer, add_project, join, make_cfg

OWNER, SARA, ALI = "acc-owner", "acc-sara", "acc-ali"


class LeaveTest(unittest.TestCase):
    def setUp(self):
        self.app = build_app(make_cfg(Path(tempfile.mkdtemp())), mock.Mock(), mock.Mock())
        self.owner = add_developer(self.app.users, OWNER)
        self.sara = add_developer(self.app.users, SARA)
        self.ali = add_developer(self.app.users, ALI)
        self.market = add_project(self.app.projects, self.owner, "marketplace", ["MAR"])
        join(self.app.users, self.owner, self.market, OWNER)
        join(self.app.users, self.sara, self.market, SARA)  # joined before Ali: the longest-standing other member
        join(self.app.users, self.ali, self.market, ALI)

    def leave(self, user_id: int, project_id: int | None = None) -> None:
        self.app.project_service.leave(self.app.users.get(user_id), str(project_id or self.market.id))

    def test_the_owner_leaving_hands_the_project_to_the_longest_standing_member(self):
        self.leave(self.owner)
        project = self.app.projects.get(self.market.id)
        self.assertEqual(project.owner_id, self.sara)
        self.assertEqual(self.app.project_service.webhook_secret(self.app.users.get(self.sara), project),
                         self.app.projects.webhook_secret(project.id))
        self.assertIsNone(self.app.project_service.webhook_secret(self.app.users.get(self.owner), project))  # the former owner
        self.assertNotIn(self.market.id, self.app.users.get(self.owner).subscriptions)
        self.assertIn(("Left a project", "marketplace"), [(a, d) for _, a, d in self.app.users.activity(self.owner)[0]])
        self.assertIn(("Became the owner of a project", "marketplace"),
                      [(a, d) for _, a, d in self.app.users.activity(self.sara)[0]])

    def test_the_former_owner_can_no_longer_change_the_project(self):
        self.leave(self.owner)
        former = self.app.users.get(self.owner)
        with self.assertRaisesRegex(UserError, "Only the project's owner"):
            self.app.project_service.owned_project(former, str(self.market.id))

    def test_a_member_who_is_not_the_owner_leaves_and_the_owner_stays(self):
        self.leave(self.ali)
        self.assertEqual(self.app.projects.get(self.market.id).owner_id, self.owner)
        self.assertNotIn(self.market.id, self.app.users.get(self.ali).subscriptions)
        self.assertEqual(self.app.users.activity(self.owner)[0], [])  # nothing recorded for the owner

    def test_the_owner_who_is_the_only_member_is_refused_and_nothing_changes(self):
        alone = add_project(self.app.projects, self.owner, "side", ["SIDE"])
        join(self.app.users, self.owner, alone, OWNER)
        with self.assertRaisesRegex(UserError, "delete the project instead"):
            self.app.project_service.leave(self.app.users.get(self.owner), str(alone.id))
        self.assertEqual(self.app.projects.get(alone.id).owner_id, self.owner)
        self.assertIn(alone.id, self.app.users.get(self.owner).subscriptions)
        self.assertEqual(self.app.users.activity(self.owner)[0], [])
        self.app.project_service.delete(self.app.users.get(self.owner), str(alone.id))  # the way out
        self.assertIsNone(self.app.projects.get(alone.id))

    def test_the_owner_leaving_with_no_other_member_left_is_refused_too(self):
        self.leave(self.sara)  # Sara and Ali leave; Majid is the only member, and the owner
        self.leave(self.ali)
        with self.assertRaisesRegex(UserError, "delete the project instead"):
            self.leave(self.owner)
        self.assertEqual(self.app.projects.get(self.market.id).owner_id, self.owner)


class IdsFromFormsTest(unittest.TestCase):
    def test_only_plain_short_digits_are_ids(self):
        self.assertEqual(accounts.form_id("12"), 12)
        for bad in ("", "12a", " 12", "-1", "١٢", "9" * 19, "1.5"):
            self.assertIsNone(accounts.form_id(bad), bad)


if __name__ == "__main__":
    unittest.main()
