"""The Run issues tab (running issues by hand) and the Recent runs tab (the developer's runs)."""
import json
import re
import time

from ..automation.rules import MAX_NOTE_CHARS
from ..automation.timing import duration
from ..models.project import Project
from ..models.run import ACTIVE, KINDS, MAX_SEARCH, PAGE_SIZE, PHASES, STATUSES, RunPage, phase
from .components import csrf_field, esc, options, plural

SHORT_MESSAGE = 90  # a longer message, or one with a model or note, folds to its first line until opened
# Links in a run's message (escaped already): PRs, and where to open one by hand.
URL_RE = re.compile(r"https://[A-Za-z0-9./_#?=%~+:-]+[A-Za-z0-9/_#=%~+-]")
PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def example_keys(joined: list[Project]) -> tuple[str, str]:
    """Two Jira project keys for the form's examples, from the developer's own projects: their first
    project's, and another project's when there is one (the same key otherwise)."""
    keys = list(dict.fromkeys(min(p.jira_projects) for p in joined if p.jira_projects)) or ["ABC"]
    return keys[0], keys[-1] if len(keys) > 1 else keys[0]


def runs_panel(max_parallel_runs: int, joined: list[Project], csrf: str, answering=None) -> str:
    """`answering`: a run that asked the developer questions; the form is filled in to answer them."""
    hidden = csrf_field(csrf)
    asked = json.loads(answering["questions"]) if answering and answering["questions"] else {}
    keys = answering["issue_key"] if answering else ""
    project_pick = ""
    if len(joined) > 1:
        project_pick = (f'<label>Project <select name="project_id">'  # noqa: S608
                        f'{options([(p.id, p.name) for p in joined], answering and answering["project_id"], "Find it from the issue key")}'
                        '</select></label>')
    questions = ""
    if asked:
        questions = ('<div class="field asked"><span class="label">ShadowDev asked you</span><ol>'
                     + "".join(f"<li>{esc(key)}: {esc(q)}</li>" for key, qs in asked.items() for q in qs)
                     + "</ol><p class=\"hint\">Answer each in the note, after its key. Claude gets the questions with "
                       "your answers.</p></div>")
    note = "".join(f"{key}: \n" for key in asked)
    together = ", " in keys
    k, other = map(esc, example_keys(joined))
    return f"""<section class="card">
  <div class="card-head"><h3>Run issues now</h3></div>
  <p class="muted">For issues assigned to you before you joined. New assignments start by themselves; up to
    {max_parallel_runs} of your runs work at the same time.</p>
  <form method="post" action="/run" id="run-form">{hidden}
<label>Issue keys<span class="sub">Separated by commas, with ranges: <code>{k}-1 to {k}-10 except {k}-3</code>. Of a
  range, only the issues assigned to you run.</span>
  <input name="issue_keys" value="{esc(keys)}" placeholder="{k}-123, {other}-45, {k}-1 to {k}-10 except {k}-3" required spellcheck="false" autocomplete="off"></label>
{project_pick}
{questions}
<label>Note for Claude <span class="optional">optional</span><span class="sub">What the tickets don't say: where to
  look, what to leave alone. A line starting <code>{k}-123:</code> is for that issue only.</span>
  <textarea name="note" maxlength="{MAX_NOTE_CHARS}" placeholder="Don't change the API.&#10;{k}-123: the bug is in the checkout total, not the cart."{" autofocus" if asked else ""}>{esc(note)}</textarea></label>
{_repo_pick(joined, answering)}
<div class="grid">
  <label>Review request in Slack<span class="sub">One lists every PR by repo</span><select name="message">
    <option value="one" selected>One message for all issues</option><option value="each">A message for each issue</option>
  </select></label>
</div>
<div class="field"><span class="label">With more than one issue</span>
  <div class="choices three">
    <label class="choice"><input type="radio" name="grouping" value="separate"{"" if together else " checked"}>
      <span>In parallel<small>A branch and PR each, all at once</small></span></label>
    <label class="choice"><input type="radio" name="grouping" value="sequence">
      <span>One after another<small>A branch and PR each, in the order given</small></span></label>
    <label class="choice"><input type="radio" name="grouping" value="together"{" checked" if together else ""}>
      <span>Together<small>One branch, one PR per repo</small></span></label>
  </div>
</div>
<div class="setting delay">
  <div class="setting-text"><label for="run-delay">Minutes between issues</label>
    <p class="hint">After an issue has ended, the next one starts this much later.</p></div>
  <div class="unit-input"><input id="run-delay" name="delay" type="number" min="0" max="240" step="any" value="0"
    inputmode="decimal"><span>min</span></div>
</div>
<div class="actions"><button>Run</button>
  <p class="hint">An issue that already has its branch is skipped unless it was sent back. To start one over, close
    its PR and delete its branch.</p></div>
  </form>
</section>"""  # noqa: S608 (HTML with <select>, not SQL)


def recent_panel(runs: RunPage, csrf: str) -> str:
    hidden = csrf_field(csrf)
    return f"""<section class="card">
  <div class="card-head"><h3>Recent runs</h3>
    <form method="post" action="/runs/delete" id="delete-runs" data-confirm="Remove the selected runs from Recent runs? Their tickets and pull requests are not touched.">{hidden}
      <button class="danger small" id="delete-selected" disabled>Delete selected</button></form></div>
  {_filters(runs)}
  <p class="sr-only" id="runs-status" role="status"></p>
  <div id="runs-results">{run_results(runs, csrf)}</div>
</section>"""


def _repo_pick(joined: list[Project], answering) -> str:
    """A checkbox per repo, per project (static/runs.js shows the picked project's): the repos the runs
    may change. A project's only repo is ticked; so are those of the run being answered."""
    again = (answering["project_id"], set(filter(None, answering["repos"].split(",")))) if answering else None
    groups = []
    for p in joined:
        every = {r.name for r in p.repos}
        ticked = every if len(every) == 1 else (again[1] or every) if again and again[0] == p.id else set()
        boxes = "".join(
            f'<label class="choice"><input type="checkbox" name="repos" value="{esc(f"{p.id}:{r.name}")}"'
            f'{" checked" if r.name in ticked else ""}><span>{esc(r.name)}<small class="mono">{esc(r.github)}</small>'
            '</span></label>' for r in p.repos)
        head = f'<div class="sub-head">{esc(p.name)}</div>' if len(joined) > 1 else ""
        groups.append(f'<div data-project="{esc(p.id)}">{head}<div class="choices repos">{boxes}</div></div>')
    return f"""<div class="field" id="run-repos"><span class="label">Repos to change<span class="sub">If the fix needs
  more, you are asked in Slack first</span></span>{"".join(groups)}</div>"""


SEARCH_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
               'aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>')
FILTER_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
               'aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>')


def _filters(runs: RunPage) -> str:
    """The search box and, in a popup, the filters with Apply and Clear: a plain GET form of the page
    (the popup a <details>), which static/runs.js sends as you type and on Apply."""
    f = runs.filter
    projects = runs.projects if not f.project or f.project in runs.projects else [*runs.projects, f.project]
    count = sum(1 for v in (f.status, f.project, f.kind) if v)
    return f"""<form method="get" action="/" id="run-filter" class="filters" role="search">
    <input type="hidden" name="tab" value="recent">
    <div class="search-box">{SEARCH_ICON}<input type="search" name="q" value="{esc(f.text)}" maxlength="{MAX_SEARCH}"
      placeholder="Search by issue key, message or note" aria-label="Search runs" spellcheck="false" autocomplete="off"></div>
    <details class="filter-menu" id="filter-menu">
      <summary class="button secondary">{FILTER_ICON}Filters<span class="count" data-count{"" if count else " hidden"}>{count}</span></summary>
      <div class="filter-panel" role="group" aria-label="Filters">
        <label>Status <select name="status">{options([(k, label) for k, (label, _) in STATUSES.items()], f.status, "Any status")}</select></label>
        <label>Project <select name="project">{options([(p, p) for p in projects], f.project, "All projects")}</select></label>
        <label>Type <select name="kind">{options(list(KINDS.items()), f.kind, "Issues and pull requests")}</select></label>
        <div class="panel-actions">
          <a class="button secondary" href="/?{esc(f.query(status="", project="", kind="", page=1))}" data-clear>Clear</a>
          <button>Apply</button>
        </div>
      </div>
    </details>
  </form>"""


def run_results(runs: RunPage, csrf: str) -> str:
    """What the filter found, a page of it: also what GET /runs answers, for the page to refresh itself
    and to show another search, filter or page without reloading."""
    f = runs.filter
    first = (f.page - 1) * PAGE_SIZE + 1
    found = (f"{first}–{first + len(runs.rows) - 1} of {plural(runs.total, 'run')}" if runs.total
             else "No runs match" if _filtered(runs) else "No runs")
    return f"""{_chips(runs)}<div class="results-bar"><p class="summary muted" data-query="{esc(f.query())}">{found}</p>{_pager(runs)}</div>
  <div class="table"><table><thead><tr><th class="pick"><input type="checkbox" id="pick-all" aria-label="Select all finished runs"></th><th>Issue</th><th>Project</th><th>Status</th><th>Details</th><th>Updated</th></tr></thead>
  <tbody id="runs">{_rows(runs, csrf)}</tbody></table></div>"""


def _chips(runs: RunPage) -> str:
    """The filters in use, each a link that removes it."""
    f = runs.filter
    used = [(what, value, field) for what, value, field in (
        ("Status", STATUSES[f.status][0] if f.status else "", "status"), ("Project", f.project, "project"),
        ("Type", KINDS.get(f.kind, ""), "kind")) if value]
    if not used:
        return ""
    return ('<div class="chips">' + "".join(
        f'<a class="chip" href="/?{esc(f.query(**{field: "", "page": 1}))}" data-nav '
        f'aria-label="Remove filter {esc(what)}: {esc(value)}"><span class="muted">{esc(what)}:</span> {esc(value)}'
        '<span class="x" aria-hidden="true">×</span></a>' for what, value, field in used) + "</div>")


def _filtered(runs: RunPage) -> bool:
    f = runs.filter
    return bool(f.text or f.status or f.project or f.kind)


def _pager(runs: RunPage) -> str:
    if runs.pages == 1:
        return ""
    page = runs.filter.page

    def link(to: int, text: str, rel: str) -> str:
        if not 1 <= to <= runs.pages:
            return f'<span class="button secondary small" aria-disabled="true">{text}</span>'
        return f'<a class="button secondary small" href="/?{esc(runs.filter.query(page=to))}" rel="{rel}" data-nav>{text}</a>'
    return (f'<nav class="pager" aria-label="Pages of runs">{link(page - 1, "Previous", "prev")}'
            f'<span class="muted">Page {page} of {runs.pages}</span>{link(page + 1, "Next", "next")}</nav>')


def _rows(runs: RunPage, csrf: str) -> str:
    hidden = csrf_field(csrf)
    empty = ("No runs match this search and these filters." if _filtered(runs)
             else "No runs yet. Runs show up here once an issue is queued.")
    return "".join(
        f'<tr><td class="pick">{_pick(r)}</td><td class="issue">{_issue_links(r)}</td><td>{esc(r["project"])}</td>'
        f'<td class="state"><span class="badge {esc(r["status"])}">{esc(r["status"])}</span>{_stop_button(r, hidden)}'
        f'{_progress(r)}</td>'
        f'<td class="msg">{_details(r)}</td>'
        f'<td class="when">{_when(r["updated"])}</td></tr>'
        for r in runs.rows
    ) or f'<tr><td colspan="6" class="empty">{empty}</td></tr>'


def _progress(run) -> str:
    """How far a running run is: a bar of its phases (models.run.PHASES), and the step it is on."""
    if run["status"] != "running" or not run["step"]:
        return ""
    found = phase(run["step"])
    bar = (f'<progress max="{len(PHASES)}" value="{found[0]}" title="{esc(found[1])}" '
           f'aria-label="{esc(found[1])}: phase {found[0]} of {len(PHASES)}"></progress>' if found else "")
    return f'<div class="progress">{bar}<small>{esc(run["step"])}</small></div>'


def _pick(run) -> str:
    """A finished run's checkbox for the delete form above the table (a form can't wrap the table,
    whose rows have forms of their own)."""
    if run["status"] in ACTIVE:
        return ""
    return (f'<input type="checkbox" name="run_id" value="{esc(run["id"])}" form="delete-runs" '
            f'aria-label="Select the run of {esc(run["issue_key"])}">')


def _details(run) -> str:
    """The run's message; with more to it (a long message, the model, the developer's note), an
    accordion showing the first line until opened. data-run lets the page keep it open across refreshes."""
    message = run["message"] or ""
    extras = _extra(run, "model", "Model") + _extra(run, "note", "Your note") + _took(run) + _log_so_far(run)
    if not extras and len(message) <= SHORT_MESSAGE and "\n" not in message:
        return _linked(message)
    return (f'<details class="run-details" data-run="{esc(run["id"])}"><summary><span class="line">{_linked(message)}</span>'
            f'</summary>{extras}</details>')


def _took(run) -> str:
    """How long a run that has ended worked and what Claude reported for it, with a link to its log."""
    if not run["seconds"] and not run["has_log"]:
        return ""
    parts = [duration(run["seconds"])] if run["seconds"] else []
    if run["turns"]:
        parts.append(plural(run["turns"], "Claude turn"))
    if run["cost"]:
        parts.append(f"${run['cost']:.2f} as Claude counts it")
    log = (f'<a href="/runs/log?run={esc(run["id"])}" target="_blank" rel="noopener">Log</a>' if run["has_log"] else "")
    return f'<small>Took: {esc(" · ".join(parts))}{" · " if parts and log else ""}{log}</small>'


def _log_so_far(run) -> str:
    """While it runs: what it logged so far."""
    if run["status"] not in ("running", "stopping"):
        return ""
    return f'<small><a href="/runs/log?run={esc(run["id"])}" target="_blank" rel="noopener">Log so far</a></small>'


def _linked(message: str) -> str:
    """The message, escaped, with its links clickable; a PR's shown as owner/repo#12."""
    def link(m: re.Match) -> str:
        url = m.group(0)
        pr = PR_URL_RE.fullmatch(url)
        return (f'<a href="{url}" target="_blank" rel="noopener">'
                f'{f"{pr.group(1)}#{pr.group(2)}" if pr else url}</a>')
    return URL_RE.sub(link, esc(message))


def _when(at: float) -> str:
    """When a run was last updated: the time today, else the day too; the full date on hover."""
    local, now = time.localtime(at), time.localtime()
    shown = time.strftime("%H:%M" if local[:3] == now[:3] else "%d %b %H:%M", local)
    return f'<time datetime="{time.strftime("%Y-%m-%dT%H:%M", local)}" title="{time.strftime("%Y-%m-%d %H:%M", local)}">{shown}</time>'


def _extra(run, column: str, what: str) -> str:
    return f'<small>{what}: {esc(run[column])}</small>' if run[column] else ""


def _issue_links(run) -> str:
    if "#" in run["issue_key"]:  # work on a pull request: acme/fe#12
        repo, _, number = run["issue_key"].partition("#")
        return f'<a href="https://github.com/{esc(repo)}/pull/{esc(number)}" target="_blank" rel="noopener">{esc(run["issue_key"])}</a>'
    if not run["site_url"]:
        return esc(run["issue_key"])
    return ", ".join(f'<a href="{esc(run["site_url"])}/browse/{esc(k)}" target="_blank" rel="noopener">{esc(k)}</a>'
                     for k in run["issue_key"].split(", "))


def _stop_button(run, hidden: str) -> str:
    if run["status"] == "question":  # it waits for the developer's answers
        return f'<a class="button small" href="/?tab=runs&amp;answer={esc(run["id"])}#run-form">Answer</a>'
    if run["status"] not in ("queued", "running"):
        return ""
    return (f'<form method="post" action="/stop" class="stop">{hidden}'
            f'<input type="hidden" name="run_id" value="{esc(run["id"])}"><button class="danger small">Stop</button></form>')
