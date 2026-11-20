"""Reading review requests in Slack (services/review_requests.py): the asks still match, and a long
message can't make the matching slow; and what a failed PR, channel or thread logs (services/reviews.py)."""
import time
import unittest

from shadowdev.clients.rest import HTTPError
from shadowdev.services import review_requests, reviews
from shadowdev.services.errors import UserError

ME = "U0000MEME0"
PR = "https://github.com/acme/fe/pull/7"


class AsksTest(unittest.TestCase):
    def test_asks_in_words_are_found(self):
        for text in ("can you review this?", "could you take a look", "please check these", f"<@{ME}> review",
                     "ptal", "would you approve it", "re-review please", "PLEASE REVIEW"):
            self.assertTrue(review_requests.asks_for_review(text), text)
        for text in ("thanks for the review", "I reviewed it yesterday"):
            self.assertFalse(review_requests.asks_for_review(text), text)

    def test_a_reply_asks_about_the_threads_prs_only_in_words(self):
        parent = f"<@{ME}> please look at {PR}"
        self.assertEqual(review_requests.asked_in_reply(f"<@{ME}> can you review these?", parent, ME), [("acme/fe", 7)])
        self.assertEqual(review_requests.asked_in_reply(f"<@{ME}> can you review these? no need", parent, ME), [])
        self.assertEqual(review_requests.asked_in_reply(f"<@{ME}> can you review {PR}", parent, ME), [])  # it links one itself

    def test_a_long_message_is_matched_in_well_under_half_a_second(self):
        adversarial = [
            "can you " + "word " * 13000,  # no sentence end, many gaps
            "please " + "check " * 10000 + "x",
            "can you " * 8000,
            "<@" + "a" * 60000,  # an unclosed mention
            "<@U1>" * 12000,
            "  " * 30000 + "review",
        ]
        for text in adversarial:
            self.assertLess(len(text), 70000)
            started = time.perf_counter()
            review_requests.asks_for_review(text)
            review_requests.asked_in_reply(text, f"<@{ME}> {PR}", ME)
            review_requests.asks_again(text)
            self.assertLess(time.perf_counter() - started, 0.5, text[:20])

    def test_only_the_start_of_a_message_counts_as_an_ask(self):
        self.assertFalse(review_requests.asks_for_review("x" * 4500 + " can you review this?"))


class GuardedTest(unittest.TestCase):
    def test_an_expected_failure_is_a_warning_and_an_unexpected_one_has_its_traceback(self):
        calls = []

        def fails(error):
            calls.append(error)
            raise error

        with self.assertLogs("shadowdev", "WARNING") as logs:
            reviews._guarded("checking acme/fe#7", fails, HTTPError("GitHub refused", 404))
            reviews._guarded("reading the Slack channel", fails, UserError("channel is gone"))
        self.assertEqual([r.levelname for r in logs.records], ["WARNING", "WARNING"])
        with self.assertLogs("shadowdev", "ERROR") as logs:
            reviews._guarded("checking acme/fe#8", fails, KeyError("bug"))
        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIsNotNone(logs.records[0].exc_info)  # the traceback is kept
        self.assertEqual(len(calls), 3)  # and none of them stopped the caller


if __name__ == "__main__":
    unittest.main()
