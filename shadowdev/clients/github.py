from urllib.parse import quote, urlencode

from .rest import HTTPError, download, redirect_location, request_json

API = "https://api.github.com"
MAX_DIFF_BYTES = 2 * 1024**2

THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { state
    reviewThreads(first: 100) { nodes { id isResolved path line
      comments(first: 50) { nodes { author { login } authorAssociation body createdAt diffHunk } } } } } } }"""

REVIEWS_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    latestOpinionatedReviews(first: 100) { nodes { state authorAssociation author { login } } } } } }"""

CI_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { headRefOid
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } }"""


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

    def create_pr(self, owner_repo: str, base: str, head: str, title: str, body: str, draft: bool = False) -> str:
        """Open a PR (a draft one: not ready for review yet); returns its URL."""
        pr = self._request("POST", f"/repos/{owner_repo}/pulls",
                           {"title": title, "head": head, "base": base, "body": body, "draft": draft})
        return pr["html_url"]

    def prs_for_branch(self, owner_repo: str, branch: str) -> list[dict]:
        """The repo's PRs from `branch` (open, closed and merged), newest first."""
        query = urlencode({"head": f"{owner_repo.split('/')[0]}:{branch}", "state": "all", "per_page": 20})
        return self._request("GET", f"/repos/{owner_repo}/pulls?{query}") or []

    def delete_branch(self, owner_repo: str, branch: str) -> None:
        self._request("DELETE", f"/repos/{owner_repo}/git/refs/heads/{quote(branch, safe='')}")

    # Reviews

    def pr(self, owner_repo: str, number: int) -> dict:
        return self._request("GET", f"/repos/{owner_repo}/pulls/{number}")

    def pr_diff(self, owner_repo: str, number: int) -> str:
        """The PR's changes as a unified diff. Raises HTTPError 413 when it is over MAX_DIFF_BYTES."""
        headers = {**self._headers, "Accept": "application/vnd.github.diff"}
        return download(f"{API}/repos/{owner_repo}/pulls/{number}", headers, MAX_DIFF_BYTES, "GitHub").decode(errors="replace")

    def _graphql(self, query: str, **variables) -> dict:
        data = self._request("POST", "/graphql", {"query": query, "variables": variables}) or {}
        if data.get("errors"):
            raise HTTPError(f"GitHub GraphQL: {str(data['errors'])[:300]}", 400)
        return data.get("data") or {}

    def review_threads(self, owner_repo: str, number: int) -> tuple[str, list[dict]]:
        """(the PR's state: OPEN, CLOSED or MERGED; its review threads). A thread: id, resolved,
        path, line, hunk (the code it is on), and comments [{author, association, body, at}]."""
        owner, name = owner_repo.split("/")
        pr = (self._graphql(THREADS_QUERY, owner=owner, name=name, number=number).get("repository") or {}).get("pullRequest")
        if not pr:
            raise HTTPError(f"GitHub: no pull request {owner_repo}#{number}", 404)
        threads = []
        for node in pr["reviewThreads"]["nodes"]:
            comments = node["comments"]["nodes"]
            threads.append({
                "id": node["id"], "resolved": node["isResolved"], "path": node["path"], "line": node["line"],
                "hunk": comments[0]["diffHunk"] if comments else "",
                "comments": [{"author": (c.get("author") or {}).get("login") or "", "association": c["authorAssociation"],
                              "body": c["body"], "at": c["createdAt"]} for c in comments]})
        return pr["state"], threads

    def latest_reviews(self, owner_repo: str, number: int) -> list[dict]:
        """Each reviewer's current verdict on the PR, [{author, association, state}], state APPROVED or
        CHANGES_REQUESTED (a comment-only or dismissed review is not one)."""
        owner, name = owner_repo.split("/")
        pr = (self._graphql(REVIEWS_QUERY, owner=owner, name=name, number=number).get("repository") or {}).get("pullRequest")
        if not pr:
            raise HTTPError(f"GitHub: no pull request {owner_repo}#{number}", 404)
        return [{"author": (r.get("author") or {}).get("login") or "", "association": r["authorAssociation"], "state": r["state"]}
                for r in pr["latestOpinionatedReviews"]["nodes"]]

    def reply_to_thread(self, thread_id: str, body: str) -> None:
        self._graphql("mutation($thread: ID!, $body: String!) { addPullRequestReviewThreadReply(input: "
                      "{pullRequestReviewThreadId: $thread, body: $body}) { clientMutationId } }", thread=thread_id, body=body)

    def resolve_thread(self, thread_id: str) -> None:
        self._graphql("mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { clientMutationId } }",
                      thread=thread_id)

    def create_review(self, owner_repo: str, number: int, event: str, body: str, comments: list[dict] = ()) -> None:
        """Submit a review: event APPROVE or COMMENT; `comments`: [{path, line, body}] on lines of the diff."""
        self._request("POST", f"/repos/{owner_repo}/pulls/{number}/reviews", {
            "event": event, "body": body,
            "comments": [{"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]} for c in comments]})

    # CI

    def ci_state(self, owner_repo: str, number: int) -> tuple[str, str | None]:
        """(the PR's head commit, the combined state of its checks and statuses: SUCCESS, FAILURE,
        ERROR, PENDING or EXPECTED; None when it has none)."""
        owner, name = owner_repo.split("/")
        pr = (self._graphql(CI_QUERY, owner=owner, name=name, number=number).get("repository") or {}).get("pullRequest")
        if not pr:
            raise HTTPError(f"GitHub: no pull request {owner_repo}#{number}", 404)
        commits = pr["commits"]["nodes"]
        rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
        return pr["headRefOid"], rollup.get("state")

    def check_runs(self, owner_repo: str, sha: str) -> list[dict]:
        """The commit's check runs: [{id, name, status, conclusion, html_url, app: {slug}, output}]."""
        return (self._request("GET", f"/repos/{owner_repo}/commits/{sha}/check-runs?per_page=100") or {}).get("check_runs") or []

    def statuses(self, owner_repo: str, sha: str) -> list[dict]:
        """The commit's statuses (CI that reports without checks): [{context, state, description, target_url}]."""
        return (self._request("GET", f"/repos/{owner_repo}/commits/{sha}/status?per_page=100") or {}).get("statuses") or []

    def job_log(self, owner_repo: str, job_id: int, max_bytes: int) -> str:
        """A GitHub Actions job's log. GitHub answers with a redirect to a short-lived signed URL on
        another host, which is fetched without the token. Raises HTTPError 413 when it is too big."""
        url = redirect_location(f"{API}/repos/{owner_repo}/actions/jobs/{job_id}/logs", self._headers, "GitHub")
        return download(url, {}, max_bytes, "GitHub").decode(errors="replace")

    def comment(self, owner_repo: str, number: int, body: str) -> None:
        """A plain comment on the PR's conversation."""
        self._request("POST", f"/repos/{owner_repo}/issues/{number}/comments", {"body": body})


def commit_identity(user: dict) -> tuple[str, str]:
    """(name, email) for commits: the public email, or GitHub's noreply address, which still
    links the commit to the account."""
    name = user.get("name") or user["login"]
    email = user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"
    return name, email
