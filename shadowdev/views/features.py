"""The Features tab: what ShadowDev does, at a glance. Fixed text, nothing from outside.
FEATURES.md says the same for the repo; keep the two alike."""


def _flow(label: str, steps: list[tuple[str, str]]) -> str:
    """A left-to-right diagram of steps (top to bottom on a narrow screen). A step's title may hold
    several choices separated by " / ", drawn stacked: any one of them leads on."""
    def step(title: str, detail: str) -> str:
        choices = title.split(" / ")
        head = (f'<b>{title}</b>' if len(choices) == 1
                else '<span class="flow-or">' + "".join(f"<b>{c}</b>" for c in choices) + "</span>")
        return f"<li>{head}<span>{detail}</span></li>"
    return f'<ol class="flow" aria-label="{label}">{"".join(step(t, d) for t, d in steps)}</ol>'


def _bullets(items: list[tuple[str, str]]) -> str:
    return '<ul class="features">' + "".join(f"<li><b>{t}</b> {d}</li>" for t, d in items) + "</ul>"


# (title, a diagram or "", the use cases): one card each, in this order.
SECTIONS = [
    ("What ShadowDev does", _flow("A run, step by step", [
        ("Assigned", "a Jira bug or task, to you"),
        ("Analysed", "root cause or plan, on the ticket"),
        ("Changed", "code, plus a test proving the fix"),
        ("Tested", "in a sandbox, against the base branch"),
        ("PR opened", "as you, ticket to In Review"),
    ]), [
        ("Bugs.", "Root cause on the ticket, a regression test that fails without the fix, the fix version set."),
        ("Tasks and stories.", "An implementation plan on the ticket, then the change."),
        ("Several repos.", "One run changes frontend and backend, with a PR in each; or only the repos you tick."),
        ("Your conventions.", "Follows each repo's CLAUDE.md, AGENTS.md, Copilot, Cursor and other AI rules."),
        ("Sent back.", "Comment on a ticket after its PR and it works again, updating the same PR."),
        ("Right model.", "Rates each issue: Haiku, Sonnet or your default; a stronger one takes over if needed."),
    ]),
    ("How work starts", _flow("How runs start", [
        ("Webhook / Polling / Run now", "instant, every 15 min, or by hand"),
        ("Queue", "fair between developers, by project and Jira priority"),
        ("Run", "with that project's accounts"),
    ]), [
        ("By itself.", "New assignments in chosen statuses and the active sprint start on their own."),
        ("Run issues now.", "Keys or ranges: MAR-1 to MAR-10 except MAR-3."),
        ("Several at once.", "In parallel, one after another (with a gap), or together on one branch."),
        ("Note for Claude.", "Where to look, what to leave alone; for all issues or one. Never posted."),
        ("Priorities.", "Runs at the same time and priority per project; urgent Jira issues first."),
    ]),
    ("When the issue is unclear", _flow("What happens to an open question", [
        ("Code settles it / Behaviour / Costly design", "what kind of question it is"),
        ("Decides / Asks reporter / Asks you", "PR assumptions, ticket comment, Slack or email"),
        ("Continues", "after the answer, or with what it has"),
    ]), [
        ("Assumptions.", "What it decided itself is listed in the PR for reviewers to check."),
        ("Ask the reporter.", "Missing steps or expected behaviour, on the ticket; resumes on their reply."),
        ("Ask you.", "Public APIs, stored data, two valid designs; Answer fills in the next run."),
    ]),
    ("After the PR", _flow("After the PR is open", [
        ("Review comments / Failing CI / Conflicts", "on a PR it opened"),
        ("Fixed", "on the PR's branch, tests run again"),
        ("Pushed", "threads answered, reviewers asked again"),
    ]), [
        ("How it publishes.", "A PR, a draft PR, or only the branch for you to open."),
        ("Review requests.", "Posted in the project's Slack channel, tagging each repo's reviewers."),
        ("Reminders.", "Nobody reviewed it after a few hours? Reviewers are asked again."),
        ("Reviews for you.", "Mention yourself with a PR link in Slack: it reviews, comments or approves."),
        ("Approve without review.", "Branches you name, like release/* into main, are approved as you."),
    ]),
    ("Jira kept tidy", "", [
        ("Status moves.", "To In Progress, then In Review; back where it was if the run fails."),
        ("Status rules.", "Moves finished tickets on once they have root cause, fix version, time logged."),
        ("Told what's missing.", "Tickets a rule can't move yet are sent to you, with what each lacks."),
    ]),
    ("You stay in control", "", [
        ("It is you.", "Commits, PRs, comments and Slack messages use your accounts, in the first person."),
        ("Your switches.", "Every automation is on or off per project, under Projects → Automation."),
        ("Working hours.", "Work runs any time; PRs and comments others see wait for your hours."),
        ("Stop and pause.", "Stop any run, or pause everything; settings are kept."),
        ("Overview.", "A dashboard: what needs you, what runs, PRs in review, Claude usage, held work."),
        ("Recent runs.", "Live progress, logs, time and cost; search, filter, delete."),
        ("Live logs.", "Your runs' log lines as they happen, like a terminal."),
        ("Asks first.", "Deleting, stopping, saving changes and signing out are confirmed in a dialog."),
        ("Tell me.", "Decisions and missing ticket fields by Slack, email, or only on this page."),
        ("Activity.", "Every sign-in and change to your accounts and projects."),
    ]),
    ("Accounts and limits", "", [
        ("Many accounts.", "Office and personal Jira, GitHub and Claude; each project picks its own."),
        ("Usage limits.", "Holds runs before a Claude limit cuts them off and resumes after the reset."),
        ("Switch Claude.", "Or carries on with your other Claude account, conversation included."),
        ("Shared projects.", "Add one by pasting repo URLs and Jira keys; teammates join with their own accounts."),
    ]),
    ("For the admin", "", [
        ("One server, whole team.", "Linux directly, or Docker on macOS, Windows and Linux."),
        ("Dry run.", "The real pipeline on a made-up issue; nothing sent to Jira or GitHub."),
        ("Backups and keys.", "make backup, and key rotation without anyone reconnecting."),
        ("Graceful restarts.", "Runs in progress finish; queued ones start again."),
    ]),
    ("Safe by design", "", [
        ("Sandboxed.", "Claude and the tests run in bubblewrap: no secrets, no other runs, no home folder."),
        ("No code kept.", "A run's clones and Claude's transcript are deleted when it ends."),
        ("Your issues only.", "Only issues assigned to you, checked against Jira before each run."),
        ("Encrypted tokens.", "Stored encrypted; never shown back, never in a URL or log."),
        ("Tests can't cheat.", "Setup and test commands may not change committed code; no new failures allowed."),
        ("Humans approve.", "PRs still need your team's review; CI stays the judge of tests."),
    ]),
]


def features_panel() -> str:
    return "".join(
        f'<section class="card"><div class="card-head"><h3>{title}</h3></div>'
        f'{diagram}{_bullets(items)}</section>' for title, diagram, items in SECTIONS)
