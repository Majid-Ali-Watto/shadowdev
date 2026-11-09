from .rest import HTTPError, request_json

API = "https://api.github.com"


class GitHub:
    """GitHub REST client acting as one developer with their personal access token: fine-grained
    (Contents and Pull requests read & write on the project repos) or classic (`repo` scope)."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, body: dict | None = None):
        return request_json(method, API + path, self._headers, body, "GitHub")

    def user(self) -> dict:
        return self._request("GET", "/user")

    def repo(self, owner_repo: str) -> dict | None:
        """The repo as this token sees it (permissions, description, default_branch); None if it can't."""
        try:
            return self._request("GET", f"/repos/{owner_repo}")
        except HTTPError as e:
            if e.status in (403, 404):
                return None
            raise

    def can_push(self, owner_repo: str) -> bool:
        repo = self.repo(owner_repo)
        return bool(repo and (repo.get("permissions") or {}).get("push"))

    def create_pr(self, owner_repo: str, base: str, head: str, title: str, body: str) -> str:
        """Open a PR; returns its URL."""
        pr = self._request("POST", f"/repos/{owner_repo}/pulls", {"title": title, "head": head, "base": base, "body": body})
        return pr["html_url"]


def commit_identity(user: dict) -> tuple[str, str]:
    """(name, email) for commits: the public email, or GitHub's noreply address, which still
    links the commit to the account."""
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email
