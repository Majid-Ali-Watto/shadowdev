from dataclasses import dataclass, field

MAX_COMMENTS = 20  # the latest ones are given to Claude
MAX_COMMENT_CHARS = 2000


@dataclass
class Issue:
    """A Jira issue a run works on, as fetched from Jira."""
    key: str
    fields: dict
    kind: str  # "bug" or "task"
    attachments: list[str] = field(default_factory=list)  # its attached files in the workspace, as relative paths

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
    def comments(self) -> list[tuple[str, str]]:
        """(author, text) of people's latest comments, oldest first. ShadowDev's own comments (its
        analysis, PR links, failures) all start by naming it, and are left out."""
        found = []
        for comment in (self.fields.get("comment") or {}).get("comments") or []:
            body = comment.get("body")
            if isinstance(body, str) and body.strip() and "ShadowDev" not in body[:40]:
                author = (comment.get("author") or {}).get("displayName") or "someone"
                found.append((author, body.strip()[:MAX_COMMENT_CHARS]))
        return found[-MAX_COMMENTS:]
