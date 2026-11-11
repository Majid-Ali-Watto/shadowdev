"""The Runs tab: run issues by hand, and the developer's recent runs."""
import json
import time

from ..automation.rules import MAX_NOTE_CHARS
from ..models.project import Project
from .components import csrf_field, esc, options

ACTIVE = ("queued", "running", "stopping")  # a run in one of these can't be deleted
SHORT_MESSAGE = 90  # a longer message, or one with a model or note, folds to its first line until opened


def runs_panel(max_parallel_runs: int, joined: list[Project], runs: list, csrf: str, answering=None) -> str:
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
    return f"""<section class="card">
  <div class="card-head"><h3>Run issues now</h3></div>
  <p class="muted">New assignments start automatically. Use this for issues that were assigned to you
    before you joined. Up to {max_parallel_runs} of your runs work at the same time (Settings); more wait their turn.
    An issue that already has a ShadowDev branch on GitHub is skipped, unless it was sent back with a
    comment saying what is still wrong: then it continues on that branch. To start it over, close its PR
    and delete its branch.</p>
  <form method="post" action="/run" id="run-form">{hidden}
<label>Issue keys <span class="muted">(separated by commas, and ranges like <code>MAR-1 to MAR-10</code>, leaving
  some out with <code>except MAR-3, MAR-7</code> or <code>!MAR-3</code>. Of a range, only the issues assigned to you,
  of a type you automate, are run)</span>
  <input name="issue_keys" value="{esc(keys)}" placeholder="MAR-123, MAR-124, MAR-1 to MAR-10 except MAR-3" required spellcheck="false" autocomplete="off"></label>
{project_pick}
{questions}
<label>Note for Claude <span class="muted">(optional: anything the tickets don't say, e.g. where to look or
  what to leave alone. Start a line with a key, like <code>MAR-123:</code>, for a note that is only for
  that issue; lines before those are for all of them)</span>
  <textarea name="note" maxlength="{MAX_NOTE_CHARS}" placeholder="Don't change the API.&#10;MAR-123: the bug is in the checkout total, not the cart."{" autofocus" if asked else ""}>{esc(note)}</textarea></label>
<div class="field"><span class="label">With more than one issue</span>
  <div class="choices">
    <label class="choice"><input type="radio" name="grouping" value="separate"{"" if together else " checked"}>
      <span>Separately<small>A branch and PR for each issue, worked on in parallel</small></span></label>
    <label class="choice"><input type="radio" name="grouping" value="together"{" checked" if together else ""}>
      <span>Together<small>One branch for all of them, one PR per changed repo (same project); each ticket
        still gets its own analysis and comments</small></span></label>
  </div>
</div>
<div class="actions"><button>Run</button></div>
  </form>
</section>

<section class="card">
  <div class="card-head"><h3>Recent runs</h3>
    <form method="post" action="/runs/delete" id="delete-runs" data-confirm="Remove the selected runs from Recent runs? Their tickets and pull requests are not touched.">{hidden}
      <button class="danger small" id="delete-selected" disabled>Delete selected</button></form></div>
  <div class="table"><table><thead><tr><th class="pick"><input type="checkbox" id="pick-all" aria-label="Select all finished runs"></th><th>Issue</th><th>Project</th><th>Status</th><th>Details</th><th>Updated</th></tr></thead>
  <tbody id="runs">{run_rows(runs, csrf)}</tbody></table></div>
</section>"""


def run_rows(runs: list, csrf: str) -> str:
    """The rows of Recent runs: also what GET /runs answers, for the page to refresh itself."""
    hidden = csrf_field(csrf)
    return "".join(
        f'<tr><td class="pick">{_pick(r)}</td><td class="issue">{_issue_links(r)}</td><td>{esc(r["project"])}</td>'
        f'<td class="state"><span class="badge {esc(r["status"])}">{esc(r["status"])}</span>{_stop_button(r, hidden)}</td>'
        f'<td class="msg">{_details(r)}</td>'
        f'<td class="when">{esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"])))}</td></tr>'
        for r in runs
    ) or '<tr><td colspan="6" class="empty">No runs yet. Runs show up here once an issue is queued.</td></tr>'


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
    extras = _extra(run, "model", "Model") + _extra(run, "note", "Your note")
    if not extras and len(message) <= SHORT_MESSAGE and "\n" not in message:
        return esc(message)
    return (f'<details class="run-details" data-run="{esc(run["id"])}"><summary><span class="line">{esc(message)}</span>'
            f'</summary>{extras}</details>')


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
