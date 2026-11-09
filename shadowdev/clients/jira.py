from collections.abc import Callable

from .rest import request_json


class Jira:
    """Jira Cloud REST client (API v2, so comments and text fields take plain strings),
    acting as one developer through their OAuth grant (see atlassian.py)."""

    def __init__(self, api_base: str, access_token: Callable[[], str]):
        self.api_base = api_base.rstrip("/")  # https://api.atlassian.com/ex/jira/<cloud id>
        self._access_token = access_token  # called per request, so it can refresh

    def _request(self, method: str, path: str, body: dict | None = None):
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        return request_json(method, self.api_base + path, headers, body, "Jira")

    def myself(self) -> dict:
        """The account the token belongs to: accountId, displayName, emailAddress."""
        return self._request("GET", "/rest/api/2/myself")

    def project(self, project_key: str) -> dict:
        """Raises HTTPError 404 if the account cannot browse the project."""
        return self._request("GET", f"/rest/api/2/project/{project_key}")

    def get_issue(self, key: str) -> dict:
        return self._request(
            "GET", f"/rest/api/2/issue/{key}?fields=summary,description,issuetype,assignee,project,status"
        )

    def project_versions(self, project_key: str) -> list[dict]:
        return self._request("GET", f"/rest/api/2/project/{project_key}/versions")

    def comment(self, key: str, text: str) -> None:
        self._request("POST", f"/rest/api/2/issue/{key}/comment", {"body": text})

    def set_field(self, key: str, field: str, value) -> None:
        self._request("PUT", f"/rest/api/2/issue/{key}", {"fields": {field: value}})

    def set_fix_versions(self, key: str, names: list[str]) -> None:
        """Replace the issue's fix versions, so a wrong one from an earlier run is corrected."""
        self._request("PUT", f"/rest/api/2/issue/{key}", {"fields": {"fixVersions": [{"name": n} for n in names]}})

    def status(self, key: str) -> str:
        return self._request("GET", f"/rest/api/2/issue/{key}?fields=status")["fields"]["status"]["name"]

    def transitions(self, key: str) -> list[dict]:
        """Transitions available from the issue's current status: [{id, name, to: {name}}]."""
        return self._request("GET", f"/rest/api/2/issue/{key}/transitions")["transitions"]

    def transition(self, key: str, transition_id: str) -> None:
        self._request("POST", f"/rest/api/2/issue/{key}/transitions", {"transition": {"id": transition_id}})

    def move_along(self, key: str, path: list[str]) -> tuple[str, list[str]]:
        """Move the issue towards the last status of `path`, one workflow transition at a time,
        e.g. Backlog -> To Do -> In Progress. Each step takes the furthest status of the path
        reachable from where the issue is, and never moves back along the path.
        Returns (final status, statuses moved through)."""
        wanted = [s.lower() for s in path]
        moved: list[str] = []
        current = self.status(key)
        for _ in range(len(path)):
            if current.lower() == wanted[-1]:
                break
            position = wanted.index(current.lower()) if current.lower() in wanted else -1
            reachable = {t["to"]["name"].lower(): t for t in self.transitions(key)}
            ahead = [i for i, s in enumerate(wanted) if i > position and s in reachable]
            if not ahead:
                break
            step = reachable[wanted[max(ahead)]]
            self.transition(key, step["id"])
            current = step["to"]["name"]
            moved.append(current)
        return current, moved
