"""The project editor: add a project, or edit or delete one the developer owns."""
from ..config import Config
from ..models.project import Project
from ..models.release import DEFAULT_RULES, LABELS, rules_text
from ..models.user import User
from .components import MARK, THEME_TOGGLE, alert, csrf_field, esc, options, page, script
from .projects import webhook_help


def project_page(cfg: Config, user: User, form: dict, project: Project | None, secret: str | None, csrf: str,
                 nonce: str, theme: str = "", error: str = "") -> str:
    """Add a project (project None) or edit one the developer owns. `form`: the values to show."""
    hidden = csrf_field(csrf)
    v = lambda k: esc(form.get(k) or "")
    sites = [(cid, f"{s.name} ({s.url})") for cid, s in sorted(user.sites.items(), key=lambda i: i[1].name)]
    site_field = (f'<label>Jira site <select name="cloud_id" required>{options(sites, form.get("cloud_id") or (sites[0][0] if len(sites) == 1 else None), "Choose…")}</select></label>'
                  if not project else f'<p class="muted">Jira site: {esc(user.sites[project.cloud_id].name)}</p>')
    github = [(g.id, f"{g.label} (@{g.login})") for g in user.github_accounts]
    chosen = int(form["github_account_id"]) if str(form.get("github_account_id") or "").isdigit() else (
        github[0][0] if len(github) == 1 else None)
    repos = "".join(_repo_fields(r) for r in form.get("repos") or [{}])
    title = f"Edit {project.name}" if project else "Add a project"
    return page(f"{title} · ShadowDev", f"""
<header class="top">
  <div class="brand">{MARK}<div><h1>{esc(title)}</h1><p class="muted"><a href="/?tab=projects">Back to your projects</a></p></div></div>
  {THEME_TOGGLE}
</header>
{alert(error)}
<section class="card">
  <form method="post" action="/project/save" id="project">{hidden}
{f'<input type="hidden" name="project_id" value="{esc(project.id)}">' if project else ""}
<label>Name <input name="name" value="{v('name')}" required maxlength="60" placeholder="e.g. marketplace"></label>
{site_field}
<label>Jira projects <input name="jira_keys" value="{v('jira_keys')}" required placeholder="MAR, MARS" spellcheck="false"></label>
<label>Check the repos with GitHub account <select name="github_account_id" required>{options(github, chosen, "Choose…")}</select></label>
<p class="hint">Your Jira account must see these Jira projects, and this GitHub account must be able to push to
  every repo. Only you can change the project later; members bring their own accounts.</p>
<span class="label">Repos</span>
<div id="repos">{repos}</div>
<button type="button" class="secondary" id="add-repo">Add another repo</button>
<label>Slack channel for reviews <span class="optional">optional</span><span class="sub">The channel's ID</span>
  <input name="slack_channel" value="{v('slack_channel')}" placeholder="C012ABC3DE" spellcheck="false" autocomplete="off"></label>
<p class="hint">Members who connected Slack ask there for review of the PRs ShadowDev opens, mentioning each repo's
  reviewers; and when one of them is mentioned there with a PR of this project, ShadowDev reviews it for them.</p>
<details class="more"><summary>Jira workflow and fields</summary>
  <label>Statuses to move an issue through when work starts <input name="start_statuses" value="{v('start_statuses')}" placeholder="In Progress"></label>
  <label>Statuses once the PR is open <input name="review_statuses" value="{v('review_statuses')}" placeholder="In Review"></label>
  <label>Root cause field <input name="root_cause_field" value="{v('root_cause_field')}" placeholder="customfield_10201 (empty: post it as a comment)" spellcheck="false"></label>
  <label>Status rules<span class="sub">For members who switch on Move complete tickets</span>
    <textarea name="status_rules" spellcheck="false" placeholder="{esc(rules_text(DEFAULT_RULES))}">{v('status_rules')}</textarea></label>
  <p class="hint">One per line: <code>From status -&gt; To status: checks</code>. A ticket assigned to the member moves
    only once it passes the checks: {esc(", ".join(LABELS.values()))}. Root cause is checked on bugs only (the root cause
    field, or a comment giving it). No checks: it moves as it is, e.g. <code>Merged -&gt; Ready for QA</code>. Empty: the
    rule above.</p>
</details>
<div class="actions"><button>{"Save project" if project else "Add project"}</button></div>
  </form>
</section>
{f'<section class="card">{webhook_help(project, secret, cfg)}</section>' if project and secret else ""}
{_delete_card(project, hidden) if project else ""}
<template id="repo-template">{_repo_fields({})}</template>
{script("theme.js", nonce)}{script("project_editor.js", nonce)}{script("confirm.js", nonce)}""", theme)


def _delete_card(project: Project, hidden: str) -> str:
    return f"""<section class="card"><div class="row">
  <div><h3>Delete project</h3><p class="muted">For every member.</p></div>
  <form method="post" action="/project/delete" data-confirm="Delete {esc(project.name)}? Its members stop running there and its queued runs are stopped.">{hidden}
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
