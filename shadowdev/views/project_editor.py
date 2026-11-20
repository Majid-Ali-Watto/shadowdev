"""The project editor: add a project, or edit or delete one the developer owns."""
from ..config import Config
from ..models.project import BUG_TYPES, TASK_TYPES, Project
from ..models.release import LABELS
from ..models.user import User
from .components import alert, app_scripts, confirm, csrf_field, esc, options, page
from .layout import page_head, shell, sidebar
from .projects import webhook_help


def project_page(cfg: Config, user: User, form: dict, project: Project | None, secret: str | None, csrf: str,
                 nonce: str, theme: str = "", error: str = "", statuses: list[str] = (), needs: int = 0) -> str:
    """Add a project (project None) or edit one the developer owns, in the frame of the developer's
    page, as part of its Projects. `form`: the values to show. `statuses`: those of its Jira projects,
    suggested in the status fields. `needs`: what needs the developer, on the sidebar's Overview."""
    hidden = csrf_field(csrf)
    v = lambda k: esc(form.get(k) or "")
    # Ticked unless the form was sent (or is a saved project's) with the box unticked.
    on = lambda k: " checked" if "jira_options" not in form or form.get(k) == "on" else ""
    sites = [(cid, f"{s.name} ({s.url})") for cid, s in sorted(user.sites.items(), key=lambda i: i[1].name)]
    site_field = (f'<label>Jira site <select name="cloud_id" required>{options(sites, form.get("cloud_id") or (sites[0][0] if len(sites) == 1 else None), "Choose…")}</select></label>'
                  if not project else f'<label>Jira site <input value="{esc(user.sites[project.cloud_id].name)}" disabled></label>')
    github = [(g.id, f"{g.label} (@{g.login})") for g in user.github_accounts]
    chosen_text = str(form.get("github_account_id") or "")
    # ASCII digits only: isdigit() also accepts "²", which int() refuses.
    chosen = int(chosen_text) if chosen_text.isascii() and chosen_text.isdigit() else (
        github[0][0] if len(github) == 1 else None)
    repos = "".join(_repo_fields(r) for r in form.get("repos") or [{}])
    title = f"Edit {project.name}" if project else "Add a project"
    head = page_head(title, (("Projects", "/?tab=projects"),), project.name if project else "New project",
                     "Only you can change it. Every member's runs use it, each with their own accounts." if project else
                     "Paste its repos and Jira keys: ShadowDev checks them against GitHub and Jira, and you join it.")
    save_ask = confirm(f"Save changes to {project.name}?", "Every member's runs use them from their next start.", "Save",
                       danger=False) if project else ""
    content = f"""{head}
{alert(error)}
<form method="post" action="/project/save" id="project" class="editor">{hidden}
{f'<input type="hidden" name="project_id" value="{esc(project.id)}">' if project else ""}
<section class="card">
  <div class="card-head"><h3>Project</h3></div>
  <div class="grid">
    <label>Name <input name="name" value="{v('name')}" required maxlength="60" placeholder="e.g. marketplace"></label>
    {site_field}
    <label>Jira projects <input name="jira_keys" value="{v('jira_keys')}" required placeholder="MAR, MARS" spellcheck="false"></label>
  </div>
  <label>Check the repos with GitHub account <select name="github_account_id" required>{options(github, chosen, "Choose…")}</select></label>
  <p class="hint">Your Jira account must see these Jira projects, and this GitHub account must be able to push to
    every repo. Only you can change the project later; members bring their own accounts.</p>
</section>
<section class="card">
  <div class="card-head"><h3>Repos</h3><button type="button" class="secondary small" id="add-repo">Add another repo</button></div>
  <p class="hint">Each run clones the ones it needs into one workspace, by folder name, and tests each it changes.</p>
  <div id="repos">{repos}</div>
</section>
<section class="card">
  <div class="card-head"><h3>Reviews in Slack</h3><span class="tag off">optional</span></div>
  <label>Slack channel for reviews <span class="sub">The channel's ID</span>
    <input name="slack_channel" value="{v('slack_channel')}" placeholder="C012ABC3DE" spellcheck="false" autocomplete="off"></label>
  <p class="hint">Members who connected Slack ask there for review of the PRs ShadowDev opens, mentioning each repo's
    reviewers; and when one of them is mentioned there with a PR of this project, ShadowDev reviews it for them.</p>
</section>
<section class="card">
<details class="more section"><summary><h3>Jira workflow and fields</h3><span class="muted">Statuses, root cause, sprints,
  fix versions, issue types, status rules</span></summary>
  <label>Statuses to move an issue through when work starts <input name="start_statuses" value="{v('start_statuses')}" placeholder="In Progress" list="jira-statuses" autocomplete="off" data-statuses></label>
  <label>Statuses once the PR is open <input name="review_statuses" value="{v('review_statuses')}" placeholder="In Review" list="jira-statuses" autocomplete="off" data-statuses></label>
  <datalist id="jira-statuses">{"".join(f'<option value="{esc(s)}">' for s in statuses)}</datalist>
  <p class="hint">{f"Statuses of these Jira projects: {esc(', '.join(statuses))}." if statuses else "Each must be a status of these Jira projects; saving checks them."}</p>
  <input type="hidden" name="jira_options" value="1">
  <label>Root cause field <span class="optional">optional</span><span class="sub">Empty: bugs get their root cause as a comment</span>
    <input name="root_cause_field" value="{v('root_cause_field')}" placeholder="customfield_10201" spellcheck="false"></label>
  <label class="choice"><input type="checkbox" name="uses_sprints"{on('uses_sprints')}><span>Uses sprints<small>Members can
    start issues by themselves only in an active sprint. Off for a team without sprints (Kanban, operations): issues
    start on their status alone</small></span></label>
  <label class="choice"><input type="checkbox" name="uses_fix_versions"{on('uses_fix_versions')}><span>Uses fix
    versions<small>Bugs get an unreleased Jira version. Off: no fix version is ever set</small></span></label>
  <label>Fix version for bugs <select name="fix_version_pick">{options(FIX_VERSION_LABELS, form.get("fix_version_pick") or "next")}</select></label>
  <label>Issue types worked on as bugs<span class="sub">Root cause, regression test and fix version</span>
    <input name="bug_types" value="{v('bug_types')}" placeholder="{esc(', '.join(BUG_TYPES))}" spellcheck="false" autocomplete="off"></label>
  <label>Issue types worked on as tasks<span class="sub">A plan; for members who automate tasks too</span>
    <input name="task_types" value="{v('task_types')}" placeholder="{esc(', '.join(TASK_TYPES))}" spellcheck="false" autocomplete="off"></label>
  <p class="hint">Jira issue type names, separated by commas, e.g. Bug, Defect, Incident. Other types are never worked on.</p>
  <label>Status rules<span class="sub">For members who switch on Move complete tickets</span>
    <textarea name="status_rules" spellcheck="false" placeholder="Ready for Release -> Done: fix version, parent">{v('status_rules')}</textarea></label>
  <p class="hint">One per line: <code>From status -&gt; To status: checks</code>. A ticket assigned to the member moves
    only once it passes the checks: {esc(", ".join(LABELS.values()))}. Root cause is checked on bugs only (the root cause
    field, or a comment giving it); PR link is a comment linking a pull request of one of the project's repos. No checks: it moves as it is, e.g. <code>Merged -&gt; Ready for QA</code>. Empty: no
    tickets are moved.</p>
</details>
</section>
<div class="form-bar"><a class="button secondary" href="/?tab=projects">Cancel</a>
  <button{save_ask}>{"Save project" if project else "Add project"}</button></div>
</form>
{f'<section class="card">{webhook_help(project, secret, cfg, section=True)}</section>' if project and secret else ""}
{_delete_card(project, hidden) if project else ""}
<template id="repo-template">{_repo_fields({})}</template>"""
    side = sidebar(user, hidden, "projects", needs, tabs=False)
    return page(f"{title} · ShadowDev", shell(side, content) + app_scripts(nonce), theme, app=True)


FIX_VERSION_LABELS = (("next", "The next release (lowest open version)"), ("highest", "The highest open version"),
                      ("branch", "The one numbered like the base branch, else the next"))


def _delete_card(project: Project, hidden: str) -> str:
    return f"""<section class="card danger-zone"><div class="row">
  <div><h3>Delete project</h3><p class="muted">For every member: their runs there stop, and it can't be undone.</p></div>
  <form method="post" action="/project/delete"{confirm(f"Delete {project.name}?", "For every member: their runs there are stopped, and nothing more runs in it. This can't be undone.", "Delete project")}>{hidden}
    <input type="hidden" name="project_id" value="{esc(project.id)}"><button class="danger">Delete project</button></form>
</div></section>"""


def _repo_fields(repo: dict) -> str:
    v = lambda k: esc(repo.get(k) or "")
    return f"""<fieldset class="repo"><legend>{v('github') or "New repo"}</legend>
  <label>GitHub repo <input name="repo_github" value="{v('github')}" placeholder="owner/name or https://github.com/owner/name" spellcheck="false" autocomplete="off"></label>
  <details class="more"><summary>Folder name, description, release branches</summary>
    <label>Folder name <input name="repo_name" value="{v('name')}" placeholder="default: the repo name" spellcheck="false" autocomplete="off"></label>
    <label>Description for Claude <input name="repo_description" value="{v('description')}" placeholder="default: the GitHub description"></label>
    <label>Release branch prefix <input name="repo_release_prefix" value="{v('release_prefix')}" placeholder="release/" spellcheck="false"></label>
  </details>
  <label>Setup command <span class="optional">optional</span><span class="sub">Run once per run, before the tests</span>
    <textarea name="repo_setup" rows="1" spellcheck="false" placeholder="npm ci">{v('setup')}</textarea></label>
  <label>Test command <span class="optional">optional</span><span class="sub">Writes JUnit XML to <code>$SHADOWDEV_REPORT_DIR</code></span>
    <textarea name="repo_test" rows="1" spellcheck="false" placeholder="pytest --junitxml=&quot;$SHADOWDEV_REPORT_DIR/tests.xml&quot;">{v('test')}</textarea></label>
  <label>Reviewers in Slack <span class="optional">optional</span><span class="sub">Member IDs, mentioned when a PR of this repo needs review</span>
    <input name="repo_reviewers" value="{v('reviewers')}" placeholder="U012ABC3DE, U045FGH6IJ" spellcheck="false" autocomplete="off"></label>
  <button type="button" class="secondary small" data-remove-repo>Remove repo</button>
</fieldset>"""
