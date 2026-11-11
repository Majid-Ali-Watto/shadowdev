"""The Settings tab: the developer's timing and limits, pausing automation, and deleting the ShadowDev account."""
from ..models.limits import BOUNDS, LABELS, MINUTES, Limits, number
from ..models.user import User
from .components import esc

# The numbers of models.limits, by what they are about: (heading, [(limit, unit, what it does)]).
LIMIT_GROUPS = (
    ("Polling", (
        ("jira_poll_minutes", "min", "How often Jira is asked for issues newly assigned to you, while polling is on."),
        ("github_poll_minutes", "min", "How often your pull requests are read on GitHub, for review comments and failing CI."),
        ("slack_poll_minutes", "min", "How often the projects' Slack channels are read, for PRs you are asked to review."),
    )),
    ("Reviews", (
        ("review_quiet_minutes", "min", "Review comments are acted on once no new one came for this long, as a review "
                                        "is often several comments."),
    )),
    ("Runs", (
        ("max_parallel_runs", "runs", "Of your runs at the same time; more wait their turn."),
        ("max_fix_attempts", "tries", "Test runs of a change before the run gives up."),
        ("claude_timeout_minutes", "min", "One Claude call is stopped after this long."),
        ("command_timeout_minutes", "min", "One setup or test command is stopped after this long."),
    )),
)
_DEFAULT = Limits()


def _number_row(limits: Limits, name: str, unit: str, detail: str) -> str:
    low, high = BOUNDS[name]
    default = getattr(_DEFAULT, name)
    polling = ' data-needs-polling' if name == "jira_poll_minutes" else ""
    # A time is minutes with up to two decimals (0.5 is 30 seconds); a count is a whole number.
    step, mode = ("0.01", "decimal") if name in MINUTES else ("1", "numeric")
    return f"""<div class="setting"{polling}>
  <div class="setting-text"><label for="limit-{name}">{esc(LABELS[name])}</label>
    <p class="hint" id="limit-{name}-hint">{esc(detail)}
      <span class="range">Default {number(default)} {esc(unit)} · from {number(low)} to {number(high)}</span></p></div>
  <div class="unit-input"><input type="number" id="limit-{name}" name="{name}" value="{number(getattr(limits, name))}"
    min="{number(low)}" max="{number(high)}" step="{step}" required inputmode="{mode}" aria-describedby="limit-{name}-hint">
    <span aria-hidden="true">{esc(unit)}</span></div>
</div>"""


def _limits_card(user: User, hidden: str) -> str:
    limits = user.limits
    changed = limits != _DEFAULT
    rows = "".join(
        f'<h4 class="setting-group">{esc(heading)}</h4>'
        + (f"""<div class="setting">
  <div class="setting-text"><label for="limit-jira_polling">Poll Jira for new assignments</label>
    <p class="hint" id="limit-jira_polling-hint">For a machine Jira's webhooks can't reach, like a laptop. Runs can
      always be started from Run issues now.</p></div>
  <input type="checkbox" class="switch" role="switch" id="limit-jira_polling" name="jira_polling"
    aria-describedby="limit-jira_polling-hint"{" checked" if limits.jira_polling else ""}>
</div>""" if heading == "Polling" else "")
        + "".join(_number_row(limits, name, unit, detail) for name, unit, detail in items)
        for heading, items in LIMIT_GROUPS)
    tag = f'<span class="tag {"ok" if changed else "off"}">{"customized" if changed else "defaults"}</span>'
    return f"""<section class="card limits">
  <div class="card-head"><h3>Timing and limits</h3>{tag}</div>
  <p class="muted">Yours alone: how often ShadowDev looks for your work, and how much a run may take. Times are
    in minutes, with decimals for less: 0.5 is 30 seconds. Polls use a change from their next round, runs from
    their start.</p>
  <form method="post" action="/limits">{hidden}
{rows}
<div class="actions"><button>Save timing and limits</button></div>
  </form>
</section>"""


def settings_panel(user: User, hidden: str) -> str:
    if user.paused:
        pause_title, pause_text = "Automation paused", "Issues assigned to you are not worked on, and runs can't be started."
        pause_form = f'<form method="post" action="/resume">{hidden}<button>Resume automation</button></form>'
    else:
        pause_title = "Pause automation"
        pause_text = "Stops ShadowDev from working on your issues, without changing your settings. A run in progress finishes."
        pause_form = f'<form method="post" action="/pause">{hidden}<button class="secondary">Pause automation</button></form>'
    return _limits_card(user, hidden) + f"""<section class="card">
  <div class="row">
    <div><h3>{pause_title}</h3><p class="muted">{esc(pause_text)}</p></div>
    {pause_form}
  </div>
  <div class="row">
    <div><h3>Delete account</h3>
      <p class="muted">Stops your queued and running runs (nothing more is pushed or written to Jira), and
        removes your accounts, tokens, settings and run history from ShadowDev. Projects you own pass to
        their longest-standing member. To revoke access completely, also remove it at Atlassian
        (Connected apps), GitHub (your tokens) and Claude.</p></div>
    <form method="post" action="/delete-account" data-confirm="Delete your ShadowDev account? Your queued and running runs are stopped and your stored tokens removed.">{hidden}
      <button class="danger">Delete my ShadowDev account</button></form>
  </div>
</section>"""
