from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    """A developer's Jira OAuth grant."""
    access_token: str  # valid for about an hour
    refresh_token: str  # rotating: each refresh returns a new one, and the old one stops working
    expires_at: float
