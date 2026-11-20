"""Atlassian's personal data reporting. ShadowDev stores Atlassian accounts' IDs, names and emails,
so at least once per cycle (7 days, or what Atlassian's Cycle-Period header says) it reports each
stored account, with when its data was fetched. For accounts Atlassian answers are closed, it
erases everything it holds about them; for ones that were updated, it fetches name and email again.
Progress is kept per account in the database, so a restart resumes where it was.
https://developer.atlassian.com/cloud/jira/platform/user-privacy-developer-guide/

The API takes OAuth 2.0 (3LO) apps' bearer tokens, so the stored accounts' tokens are tried in turn."""
import logging
import random
import threading
import time

from ..clients.atlassian import MAX_REPORTED_ACCOUNTS, AtlassianOAuth, JiraAccessLost, access_token, report_accounts
from ..clients.rest import HTTPError
from ..repositories.users import UserRepository
from .accounts import AccountService

log = logging.getLogger("shadowdev")

DEFAULT_CYCLE_SECONDS = 7 * 24 * 3600
PASS_SECONDS = 3600  # how often to look for accounts due; jittered, so never at a fixed time


class PrivacyReporter:
    def __init__(self, users: UserRepository, oauth: AtlassianOAuth, accounts: AccountService):
        self.users = users
        self.oauth = oauth
        self.accounts = accounts
        self.cycle = DEFAULT_CYCLE_SECONDS

    def start(self) -> None:
        threading.Thread(target=self.run_forever, daemon=True, name="privacy").start()

    def run_forever(self) -> None:
        time.sleep(random.uniform(60, 900))  # not right at startup, nor at the same minute each time
        while True:
            try:
                wait = self.report_due()
            except Exception:
                log.exception("personal data reporting failed; trying again later")
                wait = PASS_SECONDS
            time.sleep(wait + random.uniform(0, 600))

    def _tokens(self):
        for jira_account_id in self.users.jira_account_ids():
            try:
                yield access_token(self.oauth, self.users, jira_account_id)()
            except (JiraAccessLost, HTTPError, RuntimeError):
                continue  # revoked or gone: the next account's token

    def report_due(self, now: float | None = None) -> float:
        """Report every account not reported for a cycle, 90 at a time, and act on the answers. An
        account is marked reported only once its answer was acted on: one whose action failed is due
        again on the next pass, and the others of its batch are not held up by it. Returns the seconds
        to wait before the next pass."""
        now = now or time.time()
        while batch := self.users.accounts_to_report(now - self.cycle, MAX_REPORTED_ACCOUNTS):
            for token in self._tokens():
                try:
                    actions, cycle = report_accounts(token, [(account_id, at) for _, account_id, at in batch])
                    break
                except HTTPError as e:
                    if e.status in (401, 403):
                        continue
                    if e.status in (429, 503):
                        retry = e.retry_after or ""
                        return float(retry) if retry.isascii() and retry.isdigit() else PASS_SECONDS
                    raise
            else:
                log.warning("personal data reporting: no stored Atlassian token was accepted; %d account(s) due", len(batch))
                return PASS_SECONDS
            if cycle:
                self.cycle = cycle
            statuses = {str(item.get("accountId") or ""): item.get("status") for item in actions}
            handled = [jira_id for jira_id, account_id, _ in batch if self._act(account_id, statuses.get(account_id))]
            self.users.mark_reported(handled, now)
            log.info("personal data reporting: %d account(s) reported, %d to act on, %d done", len(batch),
                     len(statuses), len(handled))
            if not handled:  # the same accounts would be due again at once: the next pass tries them
                break
        return PASS_SECONDS

    def _act(self, account_id: str, status: str | None) -> bool:
        """Erase a closed account's data, or fetch an updated one's name and email again. True when
        there is nothing (more) to do; False when it failed, so the account is due again."""
        try:
            if status == "closed":
                log.info("personal data reporting: Atlassian account %s was closed; erasing its data", account_id)
                self.accounts.erase_closed_jira(account_id)
            elif status == "updated":
                self.accounts.refresh_jira_profile(account_id)
        except (HTTPError, JiraAccessLost) as e:  # e.g. its access was revoked
            log.warning("personal data reporting: could not act on Atlassian account %s (%s): %s", account_id, status, e)
            return False
        except Exception:
            log.exception("personal data reporting: could not act on Atlassian account %s (%s); due again", account_id, status)
            return False
        return True
