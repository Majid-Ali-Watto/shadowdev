"""A developer's account: sign in with Jira, connect more Jira accounts and any number of GitHub
and Claude accounts (e.g. personal and office), pause, and delete. Every token is checked
against Jira, GitHub or Claude before it is saved."""
import secrets
import shutil

from ..automation.agent import check_token, claude_config_dir, token_variable
from ..clients.atlassian import AtlassianOAuth, access_token, jira_api_base
from ..clients.github import GitHub, commit_identity
from ..clients.jira import Jira
from ..clients.rest import HTTPError
from ..config import ANY_SITE, Config
from ..models.user import User
from ..repositories.users import AccountInUse, UserRepository
from .errors import UserError
from .run_queue import RunQueue

MAX_ACCOUNTS = 10  # of each kind, per developer


def without_whitespace(token: str) -> str:
    """Tokens never contain whitespace, but a long one copied from a wrapped terminal line can
    pick up spaces or line breaks; the claude CLI then ignores it ("Not logged in")."""
    return "".join(token.split())


def clean_label(label: str, default: str) -> str:
    label = " ".join(label.split())[:40]
    return label or default


def form_id(value: str) -> int | None:
    return int(value) if value.isdigit() else None


class AccountService:
    def __init__(self, cfg: Config, users: UserRepository, oauth: AtlassianOAuth, queue: RunQueue):
        self.cfg = cfg
        self.users = users
        self.oauth = oauth
        self.queue = queue

    def complete_jira(self, code: str, user: User | None) -> int:
        """Finishes the Jira OAuth grant: the code Atlassian sent back becomes tokens for the Jira
        sites the developer granted that this server allows. Without `user` it signs in (creating
        the developer on first use); with it, it connects another Jira account to them.
        Returns the developer's ID."""
        try:
            tokens = self.oauth.exchange(code)
            granted = self.oauth.sites(tokens.access_token)
            sites = [s for s in granted if self.cfg.allows_site(s.url)]
            if not sites:
                allowed = "any site" if self.cfg.jira_sites == (ANY_SITE,) else ", ".join(self.cfg.jira_sites)
                raise UserError(f"None of the Jira sites you granted is enabled on this ShadowDev ({allowed}). "
                                "Sign in again and choose one of those.")
            me = Jira(jira_api_base(sites[0].cloud_id), lambda: tokens.access_token).myself()
        except HTTPError as e:
            raise UserError(f"Jira sign-in failed ({e.status}). Try again.") from None
        if me.get("accountType") != "atlassian" or not me.get("accountId"):
            raise UserError("Only Jira user accounts can sign in.")
        if user and len(user.jira_accounts) >= MAX_ACCOUNTS \
                and not any(a.account_id == me["accountId"] for a in user.jira_accounts):
            raise UserError(f"You can connect at most {MAX_ACCOUNTS} Jira accounts.")
        try:
            return self.users.save_jira_account(user.id if user else None, me["accountId"],
                                                me.get("displayName") or me["accountId"], me.get("emailAddress") or "",
                                                sites, tokens)
        except AccountInUse:
            raise UserError("That Jira account is already connected to another ShadowDev account. To move it here, "
                            "sign in with it and delete that ShadowDev account first.") from None

    def remove_jira(self, user: User, jira_account_id: str) -> None:
        account = user.jira(form_id(jira_account_id))
        if not account:
            raise UserError("That Jira account is not connected.")
        if len(user.jira_accounts) == 1:
            raise UserError("You sign in with this Jira account, so it can't be removed. "
                            "To leave ShadowDev, delete your account.")
        self.queue.stop_runs(user.id, {p for p, s in user.subscriptions.items() if s.jira_account_id == account.id})
        self.users.remove_jira_account(user.id, account.id)

    def connect_github(self, user: User, token: str, label: str = "") -> None:
        token = without_whitespace(token)
        if not token:
            raise UserError("Paste a GitHub personal access token.")
        try:
            gh_user = GitHub(token).user()
        except HTTPError as e:
            raise UserError("GitHub did not accept this token." if e.status in (401, 403)
                            else f"Could not reach GitHub ({e.status}).") from None
        known = any(g.login == gh_user["login"] for g in user.github_accounts)
        if not known and len(user.github_accounts) >= MAX_ACCOUNTS:
            raise UserError(f"You can connect at most {MAX_ACCOUNTS} GitHub accounts.")
        name, email = commit_identity(gh_user)
        self.users.save_github(user.id, clean_label(label, gh_user["login"]), token, gh_user["login"], name, email)

    def remove_github(self, user: User, github_account_id: str) -> None:
        account = user.github(form_id(github_account_id))
        if not account:
            raise UserError("That GitHub account is not connected.")
        self.queue.stop_runs(user.id, {p for p, s in user.subscriptions.items() if s.github_account_id == account.id})
        self.users.remove_github(user.id, account.id)

    def connect_claude(self, user: User, token: str, label: str = "") -> None:
        """A token under a label the developer already has replaces that account's token."""
        token = without_whitespace(token)
        if not token_variable(token):
            raise UserError("That is not a Claude token: use the sk-ant-oat… token from `claude setup-token`, "
                            "or an sk-ant-api… key from the Anthropic Console.")
        label = clean_label(label, "Claude")
        existing = next((c for c in user.claude_accounts if c.label == label), None)
        if not existing and len(user.claude_accounts) >= MAX_ACCOUNTS:
            raise UserError(f"You can connect at most {MAX_ACCOUNTS} Claude accounts.")
        key = existing.config_key if existing else secrets.token_hex(8)
        try:
            check_token(token, claude_config_dir(self.cfg, key), self.cfg)
        except Exception as e:
            if not existing:
                shutil.rmtree(claude_config_dir(self.cfg, key), ignore_errors=True)
            raise UserError(f"Claude did not accept this token: {e}") from None
        self.users.save_claude(user.id, label, token, key)

    def remove_claude(self, user: User, claude_account_id: str) -> None:
        account = user.claude(form_id(claude_account_id))
        if not account:
            raise UserError("That Claude account is not connected.")
        self.queue.stop_runs(user.id, {p for p, s in user.subscriptions.items() if s.claude_account_id == account.id})
        if self.users.remove_claude(user.id, account.id):
            shutil.rmtree(claude_config_dir(self.cfg, account), ignore_errors=True)

    def set_paused(self, user: User, paused: bool) -> None:
        """Paused: nothing runs for the developer (webhooks are ignored, queued runs are
        skipped, runs can't be started); a run in progress finishes. Settings are kept."""
        self.users.set_paused(user.id, paused)

    def delete_account(self, user: User) -> None:
        """Stops the developer's runs in progress, then removes everything ShadowDev holds for
        them. Projects they own pass to another member. Their access at Atlassian, GitHub and
        Anthropic is theirs to revoke."""
        self.queue.stop_runs(user.id)
        for key in self.users.delete(user.id):
            shutil.rmtree(claude_config_dir(self.cfg, key), ignore_errors=True)

    # Atlassian's personal data reporting (services/privacy.py) says what to do with an account.

    def erase_closed_jira(self, account_id: str) -> None:
        """The Atlassian account was closed: erase what ShadowDev holds about it. It was the
        developer's only Jira account: the whole developer goes, as when they delete their account."""
        user_id = self.users.owner_of(account_id)
        user = self.users.get(user_id) if user_id else None
        account = next((a for a in user.jira_accounts if a.account_id == account_id), None) if user else None
        if not account:
            return
        if len(user.jira_accounts) == 1:
            return self.delete_account(user)
        self.queue.stop_runs(user.id, {p for p, s in user.subscriptions.items() if s.jira_account_id == account.id})
        self.users.remove_jira_account(user.id, account.id)

    def refresh_jira_profile(self, account_id: str) -> None:
        """The account's name or email changed at Atlassian: fetch them again, as the account."""
        user_id = self.users.owner_of(account_id)
        user = self.users.get(user_id) if user_id else None
        account = next((a for a in user.jira_accounts if a.account_id == account_id), None) if user else None
        if not account or not account.sites:
            return
        me = Jira(jira_api_base(account.sites[0].cloud_id), access_token(self.oauth, self.users, account.id)).myself()
        self.users.update_jira_profile(account.id, me.get("displayName") or account_id, me.get("emailAddress") or "")
