"""The Overview tab: the developer's dashboard. What needs them, numbers for the last 7 days, the runs
working now, their Claude usage, their PRs in review, and what waits for their working hours.
static/overview.js keeps it up to date from GET /overview."""
import json
import time

from ..automation.limits import when
from ..models.overview import Overview, pr_state
from ..models.run import PHASES, phase
from ..models.user import User
from .components import esc, plural

SHOWN = 6  # of the runs working now, and of what needs the developer
CLIP = 160  # characters of a run's message or question shown


def account_problems(user: User) -> list[str]:
    """What stops runs sooner or later, and only the developer can mend (under Accounts)."""
    problems = [f"Jira access of {a.display_name} was lost (expired or revoked): connect that account again under Accounts."
                for a in user.jira_accounts if a.lost]
    problems += [f"The GitHub {warning} ({g.label}): save a new token under Accounts."
                 for g in user.github_accounts if (warning := g.expiry_warning())]
    return problems


def needs_count(user: User, overview: Overview) -> int:
    """How many things need the developer: the number on the Overview tab."""
    return len(overview.needs) + len(account_problems(user))


def overview_panel(user: User, overview: Overview, now: float | None = None) -> str:
    now = now or time.time()
    return (f'<div id="overview" data-needs="{needs_count(user, overview)}">'
            f'{_needs(user, overview, now)}{_tiles(overview)}'
            f'<div class="cols">{_running(overview)}{_claude(overview)}</div>'
            f'<div class="cols">{_prs(overview)}{_held(overview)}</div></div>')


def _clip(text: str) -> str:
    return text if len(text) <= CLIP else text[:CLIP - 1].rstrip() + "…"


def _ago(at: float, now: float) -> str:
    minutes = max(0, int(now - at) // 60)
    if minutes < 60:
        return "just now" if minutes < 1 else f"{minutes} min ago"
    hours = minutes // 60
    return f"{plural(hours, 'hour')} ago" if hours < 24 else f"{plural(hours // 24, 'day')} ago"


def _item(icon: str, kind: str, title: str, detail: str, action: str) -> str:
    return (f'<li><span class="who"><span class="ic {kind}" aria-hidden="true">{icon}</span>'
            f'<span>{title}<small>{detail}</small></span></span>{action}</li>')


def _needs(user: User, o: Overview, now: float) -> str:
    items = []
    for run in o.needs[:SHOWN]:
        key, where = esc(run["issue_key"]), f'{esc(run["project"])} · {_ago(run["updated"], now)}'
        if run["status"] == "question":
            asked = [q for qs in json.loads(run["questions"] or "{}").values() for q in qs]
            items.append(_item("?", "q", f"{key} asks you {plural(len(asked), 'question')}",
                               f'{esc(_clip(asked[0])) if asked else ""}{" · " if asked else ""}{where}',
                               f'<a class="button small" href="/?tab=runs&amp;answer={esc(run["id"])}#run-form">Answer</a>'))
        else:
            log = (f'<a class="button secondary small" href="/runs/log?run={esc(run["id"])}" target="_blank" '
                   f'rel="noopener">Log</a>' if run["has_log"] else "")
            items.append(_item("!", "f", f"{key} failed", f'{esc(_clip(run["message"]))} · {where}', log))
    items += [_item("!", "t", esc(p), "", '<a class="button secondary small" href="/?tab=accounts">Accounts</a>')
              for p in account_problems(user)]
    more = len(o.needs) - SHOWN
    if more > 0:
        items.append(f'<li class="muted">{plural(more, "more run")} under '
                     f'<a href="/?tab=recent&amp;status=question">Recent runs</a></li>')
    count = needs_count(user, o)
    head = (f'<div class="card-head"><h3>Needs you</h3>'
            f'<span class="tag {"warn" if count else "ok"}">{count or "nothing"}</span></div>')
    body = (f'<ul class="items">{"".join(items)}</ul>' if items
            else '<p class="muted">Nothing needs you: no questions to answer, no failed runs, no account problems.</p>')
    return f'<section class="card needs{" calm" if not items else ""}" aria-label="Needs you">{head}{body}</section>'


def _tile(name: str, value: str, detail: str) -> str:
    return f'<div class="tile"><div class="k">{name}</div><div class="v">{value}</div><div class="s">{detail}</div></div>'


def _tiles(o: Overview) -> str:
    running = sum(r["status"] in ("running", "stopping") for r in o.active)
    queued = sum(r["status"] == "queued" for r in o.active)
    attention = sum(pr_state(p["review"])[1] == "warn" for p in o.prs)
    hours, minutes = divmod(int(o.seconds) // 60, 60)
    return ('<div class="tiles">'
            + _tile("Running now", str(running), f"{queued} queued · {o.at_once} at once")
            + _tile("PRs in review", str(len(o.prs)), f"{attention} need attention" if attention else "none need attention")
            + _tile("Done, last 7 days", str(o.count("done")),
                    f'{o.count("failed")} failed · {o.count("question")} asked you')
            + _tile("Claude, last 7 days", f"${o.cost:,.2f}", f"API price · {hours}h {minutes:02d}m worked")
            + "</div>")


def _phases(run) -> str:
    """A running run's progress bar, with the name of each phase under it and the current one marked."""
    found = phase(run["step"] or "")
    at = found[0] if found else 0
    names = "".join(f"<span>{f'<b>{name}</b>' if n == at else name}</span>" for n, (name, _) in enumerate(PHASES, 1))
    label = f"{found[1]}: phase {at} of {len(PHASES)}" if found else "starting"
    return (f'<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="{len(PHASES)}" aria-valuenow="{at}" '
            f'aria-label="{esc(label)}"><i style="width:{round(100 * at / len(PHASES))}%"></i></div>'
            f'<div class="phases">{names}</div>')


def _running(o: Overview) -> str:
    rows = []
    for run in o.active[:SHOWN]:
        status = run["status"]
        head = (f'<div class="top2"><span><b>{esc(run["issue_key"])}</b> <span class="muted">{esc(run["project"])}</span>'
                f'</span><span class="badge {esc(status)}">{esc(status)}</span></div>')
        detail = (_phases(run) if status == "running"
                  else f'<p class="hint">{esc(_clip(run["message"] or "waiting for a free run"))}</p>')
        rows.append(f'<div class="run">{head}{detail}</div>')
    more = len(o.active) - SHOWN
    if more > 0:
        rows.append(f'<p class="hint">and {more} more</p>')
    body = "".join(rows) or '<p class="muted">Nothing is running. New assignments start here.</p>'
    return ('<section class="card"><div class="card-head"><h3>Running now</h3>'
            f'<a class="button secondary small" href="/?tab=recent">All runs</a></div>{body}</section>')


WINDOWS = (("five_hour", "5-hour limit"), ("seven_day", "Weekly limit"))
NEAR = 0.9  # a window this full is shown as a warning


def _meter(label: str, used: float, resets: float) -> str:
    """One usage window: how much of it is used (amber once nearly full), and when it resets."""
    percent = round(used * 100)
    return (f'<div class="meter"><div class="row2"><span>{label}</span><span>{percent}% · resets {esc(when(resets))}</span>'
            f'</div><div class="bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent}" '
            f'aria-label="{label}"><i class="{"warn" if used >= NEAR else ""}" style="width:{min(100, percent)}%"></i></div></div>')


def _claude(o: Overview) -> str:
    accounts = []
    for account in o.claude:
        tag = ' <span class="tag warn">at its limit</span>' if account.held else ""
        meters = "".join(_meter(label, *account.windows[name]) for name, label in WINDOWS if name in account.windows)
        accounts.append(f'<div class="acct">{esc(account.label)}{tag}</div>'
                        + (meters or '<p class="hint">Not known yet: it shows after this account\'s next request.</p>'))
    body = "".join(accounts) or '<p class="muted">No Claude account connected. Add one under Accounts.</p>'
    return f'<section class="card"><div class="card-head"><h3>Claude usage</h3></div>{body}</section>'


def _prs(o: Overview) -> str:
    working = {r["issue_key"]: r["status"] for r in o.active}
    rows = []
    for pr in o.prs:
        name = f'{pr["github"]}#{pr["number"]}'
        words, kind = pr_state(pr["review"])
        status = working.get(name)
        state = (f'<span class="badge {esc(status)}">{"fixing" if status == "running" else esc(status)}</span>' if status
                 else f'<span class="tag {kind}">{"approved" if kind == "ok" else "waiting" if kind == "off" else "to fix"}</span>')
        reminded = f" · reminded {pr['reminders']}×" if pr["reminders"] else ""
        rows.append(f'<tr><td><a class="mono" href="https://github.com/{esc(pr["github"])}/pull/{esc(pr["number"])}" '
                    f'target="_blank" rel="noopener">{esc(name)}</a><small>{esc(pr["branch"])}</small></td>'
                    f'<td class="muted">{esc(words)}{reminded}</td>'
                    f'<td>{state}</td></tr>')
    body = (f'<table class="mini">{"".join(rows)}</table>' if rows
            else '<p class="muted">No open PRs of yours are watched. PRs your runs open show here until they close.</p>')
    return f'<section class="card"><div class="card-head"><h3>PRs in review</h3></div>{body}</section>'


def _held(o: Overview) -> str:
    groups = []
    for h in o.held:
        items = []
        if h.branches:
            items.append(f'<li><span>Pull requests of {esc(", ".join(h.branches))}</span><span class="tag off">held</span></li>')
        if h.comments:
            items.append(f'<li><span>{plural(h.comments, "comment")} on Jira and GitHub</span><span class="tag off">held</span></li>')
        if h.slack:
            items.append(f'<li><span>{h.slack} Slack {"reply" if h.slack == 1 else "replies"} to reviewers</span><span class="tag off">held</span></li>')
        groups.append(f'<p class="hint">{esc(h.project)}: your hours begin {esc(when(h.starts))}. These go out then.</p>'
                      f'<ul class="items held">{"".join(items)}</ul>')
    body = "".join(groups) or ('<p class="muted">Nothing is waiting. Outside your working hours, PRs and comments '
                               'others would see wait here.</p>')
    return f'<section class="card"><div class="card-head"><h3>Waiting for your working hours</h3></div>{body}</section>'
