"""Adding, editing and deleting projects, and joining and leaving them."""
import secrets

from ..models.project import Project
from ..services.errors import UserError
from ..services.projects import ProjectForm, RepoForm
from ..views import pages
from .request import FormPost, Request, form_action
from .settings import action

REPO_FIELDS = ("github", "name", "description", "release_prefix", "setup", "test")


def project_values(project: Project) -> dict:
    """What the editor shows for an existing project."""
    return {"name": project.name, "jira_keys": ", ".join(sorted(project.jira_projects)),
            "root_cause_field": project.root_cause_field or "", "start_statuses": ", ".join(project.start_statuses),
            "review_statuses": ", ".join(project.review_statuses),
            "repos": [{"github": r.github, "name": r.name, "description": r.description,
                       "release_prefix": r.release_prefix, "setup": r.setup or "", "test": r.test or ""}
                      for r in project.repos] + [{}]}


def show_editor(req: Request, user, csrf: str, project: Project | None, values: dict, error: str = "",
                status: int = 200) -> None:
    nonce = secrets.token_urlsafe(16)
    secret = req.app.project_service.webhook_secret(user, project) if project else None
    req.html(pages.project_page(req.app.cfg, user, values, project, secret, csrf, nonce, error), nonce, status=status)


def editor(req: Request) -> None:
    """GET /project (add) or /project?id=N (edit, for its owner)."""
    session = req.session()
    user = req.app.users.get(session[1]) if session else None
    if not user:
        return req.redirect("/")
    project = None
    if req.arg("id"):
        try:
            project = req.app.project_service.owned_project(user, req.arg("id"))
        except UserError:
            return req.redirect("/")
    show_editor(req, user, session[2], project, project_values(project) if project else {})


@form_action
def save(req: Request, post: FormPost) -> None:
    # One value per repo row for each field, in page order.
    columns = {f: post.lists.get(f"repo_{f}", []) for f in REPO_FIELDS}
    count = max(len(c) for c in columns.values())
    rows = [{f: columns[f][i] if i < len(columns[f]) else "" for f in REPO_FIELDS} for i in range(count)]
    form = ProjectForm(
        name=post.form.get("name", ""), cloud_id=post.form.get("cloud_id", ""), jira_keys=post.form.get("jira_keys", ""),
        github_account_id=post.form.get("github_account_id", ""), root_cause_field=post.form.get("root_cause_field", ""),
        start_statuses=post.form.get("start_statuses", ""), review_statuses=post.form.get("review_statuses", ""),
        repos=[RepoForm(**row) for row in rows])
    project_id = post.form.get("project_id", "")
    try:
        req.app.project_service.save(post.user, form, project_id)
    except UserError as e:
        project = None
        if project_id:
            try:
                project = req.app.project_service.owned_project(post.user, project_id)
            except UserError:
                pass
        values = {**post.form, "repos": [r for r in rows if any(r.values())] or [{}]}
        return show_editor(req, post.user, post.csrf, project, values, error=str(e), status=400)
    req.redirect("/?ok=project")


def join(req: Request, post: FormPost) -> None:
    # Base branch fields are named base:<repo>; repo names contain no ":".
    bases = {name.removeprefix("base:"): value for name, value in post.form.items() if name.startswith("base:")}
    req.app.project_service.join(post.user, post.form.get("project_id", ""), post.form.get("jira_account_id", ""),
                                 post.form.get("github_account_id", ""), post.form.get("claude_account_id", ""),
                                 post.form.get("mode", ""), bases)


ROUTES = {
    ("GET", "/project"): editor,
    ("POST", "/project/save"): save,
    ("POST", "/project/delete"): action("project-deleted", lambda req, p: req.app.project_service.delete(
        p.user, p.form.get("project_id", ""))),
    ("POST", "/project/join"): action("joined", join),
    ("POST", "/project/leave"): action("left", lambda req, p: req.app.project_service.leave(
        p.user, p.form.get("project_id", ""))),
}
