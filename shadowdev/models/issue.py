from dataclasses import dataclass


@dataclass
class Issue:
    """A Jira issue a run works on, as fetched from Jira."""
    key: str
    fields: dict
    kind: str  # "bug" or "task"

    @property
    def summary(self) -> str:
        return self.fields.get("summary") or ""

    @property
    def type_name(self) -> str:
        return (self.fields.get("issuetype") or {}).get("name") or self.kind
