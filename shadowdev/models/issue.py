import re
from dataclasses import dataclass, field

# What a run comments on a ticket. They are posted as the developer, in their own words, without
# naming any tool; a run tells them apart from what people wrote by these starts, and by their
# author being the developer's own Jira account.
PR_COMMENT_HEAD = "||Repo||Base branch||Pull request||"  # the table of a run's PRs
QUESTIONS_HEAD = "Before I start on this, I need a few answers"  # questions for the reporter
ROOT_CAUSE_HEAD = "*Root cause:*"
PLAN_HEAD = "*Implementation plan:*"
NO_CHANGE = "I looked into this, but found no code change to make here; it needs another look."
TESTS_FAILING = "I couldn't get a fix through the tests yet, so nothing is pushed."
PRS_UNOPENED = "I pushed my change, but couldn't open every pull request:"
NOT_FINISHED = "I couldn't finish this one yet; I'll come back to it."
RUN_COMMENT_STARTS = (PR_COMMENT_HEAD, QUESTIONS_HEAD, ROOT_CAUSE_HEAD, PLAN_HEAD, NO_CHANGE, TESTS_FAILING,
                      PRS_UNOPENED, NOT_FINISHED)
OLD_RUN_COMMENT = "ShadowDev"  # comments of earlier versions started by naming it
OLD_QUESTIONS_HEAD = "ShadowDev needs more information"
MENTION_RE = re.compile(r"^\[~accountid:[^\]]*\],?\s*")  # a comment may start by mentioning someone
MAX_COMMENTS = 20  # the latest ones are given to Claude
MAX_COMMENT_CHARS = 2000


@dataclass(frozen=True)
class Comment:
    body: str
    author: str  # display name
    account: str | None  # Atlassian account ID

    @property
    def text(self) -> str:
        """The body without a leading mention."""
        return MENTION_RE.sub("", self.body)


def comments_of(fields: dict) -> list[Comment]:
    """The issue's comments, oldest first."""
    return [Comment(c["body"], (c.get("author") or {}).get("displayName") or "someone",
                    (c.get("author") or {}).get("accountId"))
            for c in (fields.get("comment") or {}).get("comments") or [] if isinstance(c.get("body"), str)]


def by_run(comment: Comment, me: str | None) -> bool:
    """A comment a run posted as the developer `me` (their Atlassian account ID): by them, starting
    as a run's comments do. A person's comment that happens to start the same way is still theirs."""
    if comment.body.startswith(OLD_RUN_COMMENT) or OLD_RUN_COMMENT in comment.body[:40]:
        return True
    return bool(me) and comment.account == me and comment.text.startswith(RUN_COMMENT_STARTS)


def asked(comment: Comment, me: str | None) -> bool:
    """The questions a run asked the issue's reporter."""
    return comment.body.startswith(OLD_QUESTIONS_HEAD) or (by_run(comment, me) and comment.text.startswith(QUESTIONS_HEAD))


@dataclass
class Issue:
    """A Jira issue a run works on, as fetched from Jira."""
    key: str
    fields: dict
    kind: str  # "bug" or "task"
    attachments: list[str] = field(default_factory=list)  # its attached files in the workspace, as relative paths
    me: str = ""  # the Atlassian account the run posts as: its comments are the run's

    @property
    def summary(self) -> str:
        return self.fields.get("summary") or ""

    @property
    def type_name(self) -> str:
        return (self.fields.get("issuetype") or {}).get("name") or self.kind

    @property
    def status(self) -> str | None:
        return (self.fields.get("status") or {}).get("name")

    @property
    def reporter(self) -> str | None:
        return (self.fields.get("reporter") or {}).get("accountId")

    @property
    def comments(self) -> list[tuple[str, str]]:
        """(author, text) of people's latest comments, oldest first, and the questions an earlier run
        asked (their answers make sense only with them). A run's other comments (analysis,
        failures, PR links) are left out."""
        found = []
        for c in comments_of(self.fields):
            if asked(c, self.me):
                found.append((f"{c.author} (questions asked earlier)", c.body[:MAX_COMMENT_CHARS]))
            elif c.body.strip() and not by_run(c, self.me):
                found.append((c.author, c.body.strip()[:MAX_COMMENT_CHARS]))
        return found[-MAX_COMMENTS:]
