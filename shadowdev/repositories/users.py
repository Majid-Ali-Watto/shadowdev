"""Developers, and the Jira, GitHub, Claude and Slack accounts they connected (tokens encrypted), and
their activity. Their subscriptions (the projects they joined) are in subscriptions.py, as
`users.subscriptions`."""
import logging
import sqlite3
import time
from contextlib import closing

from ..models.limits import Limits
from ..models.tokens import Tokens
from ..models.user import ClaudeAccount, GitHubAccount, JiraAccount, Site, SlackAccount, User
from .database import Database
from .subscriptions import SubscriptionRepository, pass_ownership, subscription_of

log = logging.getLogger("shadowdev")

KEPT_ACTIVITY = 200  # entries of a developer's activity; older ones are dropped
ACTIVITY_PAGE = 25  # entries on a page of the Activity tab


class AccountInUse(Exception):
    """The Jira account is already connected to another developer."""


class UserRepository:
    def __init__(self, db: Database):
        self.db = db
        self.subscriptions = SubscriptionRepository(db)
        self._unreadable: set[int] = set()  # developers whose stored tokens were reported as unreadable

    # Jira accounts and sign-in

    def save_jira_account(self, user_id: int | None, account_id: str, display_name: str, email: str,
                          sites: list[Site], tokens: Tokens) -> int:
        """Connect an Atlassian account and the sites it reaches, or refresh it. `user_id` None: sign
        in, creating the developer on first use. Returns the developer's ID. Raises AccountInUse
        when it belongs to another developer."""
        with closing(self.db.connect()) as conn, conn:
            row = conn.execute("SELECT id, user_id FROM jira_accounts WHERE account_id = ?", (account_id,)).fetchone()
            if row and user_id is not None and row["user_id"] != user_id:
                raise AccountInUse(account_id)
            if user_id is None:  # the developer is named after the Jira account they first signed in with
                user_id = row["user_id"] if row else conn.execute(
                    "INSERT INTO users (display_name, email) VALUES (?, ?)", (display_name, email)).lastrowid
            secret = (self.db.encrypt(tokens.access_token), self.db.encrypt(tokens.refresh_token), tokens.expires_at)
            if row:
                jira_id = row["id"]
                conn.execute("UPDATE jira_accounts SET display_name = ?, email = ?, access_token = ?, refresh_token = ?, "
                             "expires = ?, profile_at = ?, lost = 0 WHERE id = ?",
                             (display_name, email, *secret, time.time(), jira_id))
            else:
                jira_id = conn.execute(
                    "INSERT INTO jira_accounts (user_id, account_id, display_name, email, access_token, refresh_token, "
                    "expires, profile_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, account_id, display_name, email, *secret, time.time())).lastrowid
            conn.executemany("INSERT INTO sites VALUES (?, ?, ?) ON CONFLICT (cloud_id) DO UPDATE SET "
                             "url = excluded.url, name = excluded.name", [(s.cloud_id, s.url, s.name) for s in sites])
            # Sites no longer granted are dropped; so are subscriptions that needed them.
            conn.execute("DELETE FROM jira_account_sites WHERE jira_account_id = ?", (jira_id,))
            conn.executemany("INSERT INTO jira_account_sites VALUES (?, ?)", [(jira_id, s.cloud_id) for s in sites])
            conn.execute("DELETE FROM subscriptions WHERE jira_account_id = ? AND project_id IN (SELECT id FROM projects "
                         "WHERE cloud_id NOT IN (SELECT cloud_id FROM jira_account_sites WHERE jira_account_id = ?))",
                         (jira_id, jira_id))
        return user_id

    def remove_jira_account(self, user_id: int, jira_account_id: int) -> None:
        """Also removes the subscriptions that used it (ON DELETE CASCADE). A developer named after
        it is renamed after their next Jira account. Their runs' logs are deleted, and the name and
        email of the account are taken out of their runs' messages and questions, so none of its
        personal data is left."""
        with closing(self.db.connect()) as conn, conn:
            gone = conn.execute("DELETE FROM jira_accounts WHERE id = ? AND user_id = ? RETURNING display_name, email",
                                (jira_account_id, user_id)).fetchone()
            if gone:
                conn.execute("DELETE FROM run_logs WHERE run_id IN (SELECT id FROM runs WHERE user_id = ?)", (user_id,))
                for text in (gone["display_name"], gone["email"]):  # what the runs said may name the account
                    if text:
                        conn.execute("UPDATE runs SET message = replace(message, ?, '[removed]'), "
                                     "questions = replace(questions, ?, '[removed]') WHERE user_id = ?",
                                     (text, text, user_id))
                conn.execute("UPDATE users SET (display_name, email) = (SELECT display_name, email FROM jira_accounts "
                             "WHERE user_id = users.id ORDER BY id LIMIT 1) WHERE id = ? AND display_name = ? AND email = ? "
                             "AND EXISTS (SELECT 1 FROM jira_accounts WHERE user_id = users.id)",
                             (user_id, gone["display_name"], gone["email"]))
        self.db.forget_copies()

    def update_jira_profile(self, jira_account_id: int, display_name: str, email: str) -> None:
        """The account's name and email as Atlassian has them now; the developer too, if named after it."""
        with closing(self.db.connect()) as conn, conn:
            old = conn.execute("SELECT user_id, display_name, email FROM jira_accounts WHERE id = ?",
                               (jira_account_id,)).fetchone()
            if not old:
                return
            conn.execute("UPDATE jira_accounts SET display_name = ?, email = ?, profile_at = ? WHERE id = ?",
                         (display_name, email, time.time(), jira_account_id))
            conn.execute("UPDATE users SET display_name = ?, email = ? WHERE id = ? AND display_name = ? AND email = ?",
                         (display_name, email, old["user_id"], old["display_name"], old["email"]))

    # Personal data reporting (services/privacy.py)

    def accounts_to_report(self, reported_before: float, limit: int) -> list[tuple[int, str, float]]:
        """(Jira account ID, Atlassian account ID, when its profile was fetched) of the accounts last
        reported before `reported_before`, least recently reported first."""
        with closing(self.db.connect()) as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT id, account_id, profile_at FROM jira_accounts WHERE reported_at < ? AND account_id != 'unknown' "
                "ORDER BY reported_at, id LIMIT ?", (reported_before, limit))]

    def mark_reported(self, jira_account_ids: list[int], at: float) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.executemany("UPDATE jira_accounts SET reported_at = ? WHERE id = ?", [(at, i) for i in jira_account_ids])

    def jira_account_ids(self) -> list[int]:
        with closing(self.db.connect()) as conn:
            return [r[0] for r in conn.execute("SELECT id FROM jira_accounts ORDER BY id")]

    def jira_tokens(self, jira_account_id: int) -> Tokens:
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT access_token, refresh_token, expires FROM jira_accounts WHERE id = ?",
                               (jira_account_id,)).fetchone()
        if not row:
            raise RuntimeError("Jira account no longer connected")
        return Tokens(self.db.decrypt(row[0]), self.db.decrypt(row[1]), row[2])

    def save_jira_tokens(self, jira_account_id: int, tokens: Tokens) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE jira_accounts SET access_token = ?, refresh_token = ?, expires = ?, lost = 0 WHERE id = ?",
                         (self.db.encrypt(tokens.access_token), self.db.encrypt(tokens.refresh_token), tokens.expires_at,
                          jira_account_id))

    def mark_jira_lost(self, jira_account_id: int) -> None:
        """Atlassian refused the account's grant: its developer is told to sign in again, on their page."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE jira_accounts SET lost = 1 WHERE id = ?", (jira_account_id,))

    def owner_of(self, account_id: str) -> int | None:
        """The developer an Atlassian account belongs to."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT user_id FROM jira_accounts WHERE account_id = ?", (account_id,)).fetchone()
        return row[0] if row else None

    # GitHub, Claude and Slack accounts

    def save_github(self, user_id: int, label: str, token: str, login: str, git_name: str, git_email: str,
                    expires: float = 0) -> int:
        """Add the account, or replace the token of the developer's account with this login.
        `expires`: when GitHub said the token expires (0: never, or it did not say)."""
        with closing(self.db.connect()) as conn, conn:
            return conn.execute(
                "INSERT INTO github_accounts (user_id, label, login, token, git_name, git_email, expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (user_id, login) DO UPDATE SET label = excluded.label, token = excluded.token, "
                "git_name = excluded.git_name, git_email = excluded.git_email, expires = excluded.expires RETURNING id",
                (user_id, label, login, self.db.encrypt(token), git_name, git_email, expires)).fetchone()[0]

    def save_claude(self, user_id: int, label: str, token: str, config_key: str) -> int:
        """Add the account, or replace the token of the developer's account with this label (its
        config dir is kept)."""
        with closing(self.db.connect()) as conn, conn:
            return conn.execute(
                "INSERT INTO claude_accounts (user_id, label, token, config_key) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (user_id, label) DO UPDATE SET token = excluded.token RETURNING id",
                (user_id, label, self.db.encrypt(token), config_key)).fetchone()[0]

    def save_slack(self, user_id: int, token: str, slack_user_id: str, team: str) -> None:
        """The developer's one Slack account; saving again replaces it."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("INSERT INTO slack_accounts VALUES (?, ?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET "
                         "token = excluded.token, slack_user_id = excluded.slack_user_id, team = excluded.team",
                         (user_id, self.db.encrypt(token), slack_user_id, team))

    def remove_slack(self, user_id: int) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM slack_accounts WHERE user_id = ?", (user_id,))

    def remove_github(self, user_id: int, github_account_id: int) -> None:
        """Subscriptions that used it are kept, without a GitHub account (ON DELETE SET NULL)."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM github_accounts WHERE id = ? AND user_id = ?", (github_account_id, user_id))

    def remove_claude(self, user_id: int, claude_account_id: int) -> str | None:
        """Returns its config key, so its config dir can be deleted."""
        with closing(self.db.connect()) as conn, conn:
            row = conn.execute("DELETE FROM claude_accounts WHERE id = ? AND user_id = ? RETURNING config_key",
                               (claude_account_id, user_id)).fetchone()
        return row[0] if row else None

    # The developer

    def get(self, user_id: int) -> User | None:
        """The developer with their accounts. Raises RuntimeError when one of their stored tokens cannot
        be decrypted: a request shows that, and a loop that goes on with other developers uses `loadable`."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._load(conn, row) if row else None

    def loadable(self, user_id: int) -> User | None:
        """`get` for a loop over developers: one whose stored tokens cannot be decrypted is logged and
        left out (None), rather than stopping the loop for everyone."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._loaded(conn, row) if row else None

    def all(self) -> list[User]:
        """Every developer, loaded with their accounts (decrypting their tokens); those whose tokens can't be
        decrypted are left out (see `loadable`). The loops that need only some of them use `timings` first."""
        with closing(self.db.connect()) as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY display_name").fetchall()
            return [user for row in rows if (user := self._loaded(conn, row))]

    def timings(self) -> dict[int, tuple[bool, Limits]]:
        """Each developer's (paused, limits), read without loading or decrypting their accounts."""
        with closing(self.db.connect()) as conn:
            return {row["id"]: (bool(row["paused"]), Limits.from_json(row["limits"]))
                    for row in conn.execute("SELECT id, paused, limits FROM users")}

    def _loaded(self, conn: sqlite3.Connection, row: sqlite3.Row) -> User | None:
        """The developer, or None when one of their stored tokens cannot be decrypted (a v1 token
        under another key, or a changed key): logged once, and left out, rather than stopping every
        loop that reads all developers."""
        try:
            return self._load(conn, row)
        except RuntimeError:  # Database.decrypt: no secret is in the message
            if row["id"] not in self._unreadable:
                self._unreadable.add(row["id"])
                log.exception("developer %s's stored tokens cannot be decrypted: they are left out until it is fixed "
                              "(SHADOWDEV_SECRET_KEY changed?)", row["id"])
            return None

    def _load(self, conn: sqlite3.Connection, row: sqlite3.Row) -> User:
        uid = row["id"]
        sites: dict[int, list[Site]] = {}
        for s in conn.execute("SELECT j.id, s.* FROM jira_accounts j JOIN jira_account_sites a ON a.jira_account_id = j.id "
                              "JOIN sites s ON s.cloud_id = a.cloud_id WHERE j.user_id = ? ORDER BY s.name", (uid,)):
            sites.setdefault(s["id"], []).append(Site(s["cloud_id"], s["url"], s["name"]))
        jira = tuple(JiraAccount(j["id"], j["account_id"], j["display_name"], j["email"], tuple(sites.get(j["id"], ())),
                                 bool(j["lost"]))
                     for j in conn.execute("SELECT * FROM jira_accounts WHERE user_id = ? ORDER BY id", (uid,)))
        github = tuple(GitHubAccount(g["id"], g["label"], g["login"], self.db.decrypt(g["token"]), g["git_name"], g["git_email"],
                                     g["expires"])
                       for g in conn.execute("SELECT * FROM github_accounts WHERE user_id = ? ORDER BY id", (uid,)))
        claude = tuple(ClaudeAccount(c["id"], c["label"], self.db.decrypt(c["token"]), c["config_key"])
                       for c in conn.execute("SELECT * FROM claude_accounts WHERE user_id = ? ORDER BY id", (uid,)))
        subs = {s["project_id"]: subscription_of(s)
                for s in conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (uid,))}
        s = conn.execute("SELECT * FROM slack_accounts WHERE user_id = ?", (uid,)).fetchone()
        slack = SlackAccount(self.db.decrypt(s["token"]), s["slack_user_id"], s["team"]) if s else None
        return User(uid, row["display_name"], row["email"], bool(row["paused"]), jira, github, claude, subs, slack,
                    Limits.from_json(row["limits"]))

    def set_paused(self, user_id: int, paused: bool) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE users SET paused = ? WHERE id = ?", (int(paused), user_id))

    def set_limits(self, user_id: int, limits: Limits) -> None:
        with closing(self.db.connect()) as conn, conn:
            conn.execute("UPDATE users SET limits = ? WHERE id = ?", (limits.to_json(), user_id))

    def limits(self, user_id: int) -> Limits:
        """The developer's limits alone, without loading (and decrypting) their accounts."""
        with closing(self.db.connect()) as conn:
            row = conn.execute("SELECT limits FROM users WHERE id = ?", (user_id,)).fetchone()
        return Limits.from_json(row[0] if row else None)

    # What the developer did (services record it; shown on their Activity tab)

    def record(self, user_id: int, action: str, detail: str = "") -> None:
        """`detail`: a label, login or project name. Never a secret, and nothing about a Jira account,
        so removing one leaves none of its data here. The latest KEPT_ACTIVITY are kept."""
        with closing(self.db.connect()) as conn, conn:
            conn.execute("INSERT INTO activity (user_id, at, action, detail) VALUES (?, ?, ?, ?)",
                         (user_id, time.time(), action, detail[:100]))
            conn.execute("DELETE FROM activity WHERE user_id = ? AND id NOT IN "
                         "(SELECT id FROM activity WHERE user_id = ? ORDER BY id DESC LIMIT ?)", (user_id, user_id, KEPT_ACTIVITY))

    def activity(self, user_id: int, page: int = 1) -> tuple[list[tuple[float, str, str]], int, int]:
        """One page of (when, what, detail) of what the developer did, newest first, with that page and
        how many there are. A page past the last gives the last."""
        with closing(self.db.connect()) as conn:
            total = conn.execute("SELECT COUNT(*) FROM activity WHERE user_id = ?", (user_id,)).fetchone()[0]
            pages = max(1, -(-total // ACTIVITY_PAGE))
            page = min(max(1, page), pages)
            rows = [tuple(r) for r in conn.execute(
                "SELECT at, action, detail FROM activity WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (user_id, ACTIVITY_PAGE, (page - 1) * ACTIVITY_PAGE))]
        return rows, page, pages

    def delete(self, user_id: int) -> list[str]:
        """Removes the developer with their accounts, sessions, subscriptions and runs. Each project
        they own passes to its longest-standing other member; one without other members is deleted.
        Returns the config keys of their Claude accounts, so those dirs can be deleted."""
        with closing(self.db.connect()) as conn, conn:
            keys = [r[0] for r in conn.execute("SELECT config_key FROM claude_accounts WHERE user_id = ?", (user_id,))]
            pass_ownership(conn, user_id)
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.db.forget_copies()
        return keys
