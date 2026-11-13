"""The Accounts tab: the developer's Jira, GitHub, Claude and Slack accounts."""
from ..models.user import User
from .components import card_head, esc, plural, remove_form, tag


def accounts_panel(user: User, hidden: str, claude_usage: dict[int, tuple[str, bool]]) -> str:
    return f"""
<p class="muted intro">Connect each account you work with, e.g. a personal and an office one of each. Every
  project you join says which ones it runs with.</p>
{_jira_card(user, hidden)}
{_github_card(user, hidden)}
{_claude_card(user, hidden, claude_usage)}
{_slack_card(user, hidden)}"""


def _count_tag(count: int) -> str:
    return tag(plural(count, "account") if count else "not connected", bool(count))


def _warning(text: str) -> str:
    return f' <span class="tag warn">{esc(text)}</span>' if text else ""


def _jira_card(user: User, hidden: str) -> str:
    removable = len(user.jira_accounts) > 1  # the account signed in with can't leave the developer without one
    items = "".join(
        f'<li><span>{esc(a.display_name)}{f" <span class=muted>({esc(a.email)})</span>" if a.email else ""}'
        f'{_warning("access lost: connect it again") if a.lost else ""}'
        f'<br><span class="muted">{esc(", ".join(s.name for s in a.sites) or "no Jira site")}</span></span>'
        f'{remove_form("/jira/remove", "jira_account_id", a.id, hidden, a.display_name) if removable else ""}</li>'
        for a in user.jira_accounts)
    return f"""<section class="card">
  {card_head(1, "Jira", True, _count_tag(len(user.jira_accounts)))}
  <ul class="items">{items}</ul>
  <form method="post" action="/jira/connect">{hidden}<button class="secondary">Connect another Jira account</button></form>
  <p class="hint">ShadowDev reads the issues assigned to you and comments as you. Access renews itself;
    if you revoke it at Atlassian, connect the account again.</p>
</section>"""


def _github_card(user: User, hidden: str) -> str:
    items = "".join(
        f'<li><span>{esc(g.label)} <span class="muted">@{esc(g.login)} · commits as {esc(g.git_name)}</span>'
        f'{_warning(g.expiry_warning())}</span>'
        f'{remove_form("/github/remove", "github_account_id", g.id, hidden, g.label)}</li>' for g in user.github_accounts)
    count = len(user.github_accounts)
    return f"""<section class="card">
  {card_head(2, "GitHub", bool(count), _count_tag(count))}
  <ul class="items">{items}</ul>
  <form method="post" action="/github">{hidden}
<div class="grid">
  <label>Label <input name="github_label" placeholder="e.g. office (default: the login)" autocomplete="off" maxlength="40"></label>
  <label>Personal access token <input name="github_token" type="password" autocomplete="off" required
    placeholder="github_pat_… or ghp_…"></label>
</div>
<p class="hint">A <b>fine-grained token</b> with access to the projects' repositories and <b>Contents</b> and
  <b>Pull requests</b> set to read and write, or a <b>classic token</b> with the <b>repo</b> scope (add
  <b>workflow</b> if changes may touch <code>.github/workflows</code>). With organization SSO, authorize the
  token for it. A token for an account you already connected replaces its old one.</p>
<button>Save GitHub account</button>
  </form>
</section>"""


def _claude_card(user: User, hidden: str, usage: dict[int, tuple[str, bool]]) -> str:
    """Each account with what is known of its usage limits (from its last request)."""
    def used(account_id: int) -> str:
        text, held = usage.get(account_id, ("", False))
        if not text:
            return ""
        flag = '<span class="tag warn">at its limit</span> ' if held else ""
        return f'<small class="usage">{flag}{esc(text)}</small>'

    items = "".join(
        f'<li><span>{esc(c.label)}{used(c.id)}</span>'
        f'{remove_form("/claude/remove", "claude_account_id", c.id, hidden, c.label)}</li>'
        for c in user.claude_accounts)
    count = len(user.claude_accounts)
    return f"""<section class="card">
  {card_head(3, "Claude", bool(count), _count_tag(count))}
  <ul class="items">{items}</ul>
  <form method="post" action="/claude">{hidden}
<div class="grid">
  <label>Label <input name="claude_label" placeholder="e.g. personal (default: Claude)" autocomplete="off" maxlength="40"></label>
  <label>Claude token <input name="claude_token" type="password" autocomplete="off" required
    placeholder="sk-ant-oat… or sk-ant-api…"></label>
</div>
<div class="hint">To get a token for your Claude subscription:
  <ol>
    <li>In a terminal, run <code>claude setup-token</code>. No Claude Code yet? Install it first:
      <code>curl -fsSL https://claude.ai/install.sh | bash</code></li>
    <li>Your browser opens: sign in to the Claude account you want (personal or office) and press <b>Authorize</b>.</li>
    <li>Copy the <code>sk-ant-oat…</code> token the terminal prints, and paste it here.</li>
  </ol>
  An API key from the Anthropic Console works too. A token under a label you already have replaces it.</div>
<button>Save Claude account</button>
  </form>
</section>"""


def _slack_card(user: User, hidden: str) -> str:
    slack = user.slack
    current = (f'<ul class="items"><li><span>{esc(slack.team)} <span class="muted">member {esc(slack.user_id)}</span></span>'
               f'<form method="post" action="/slack/remove" data-confirm="Remove your Slack account?">{hidden}'
               f'<button class="secondary small">Remove</button></form></li></ul>') if slack else ""
    return f"""<section class="card">
  {card_head(4, "Slack (optional)", bool(slack), tag(slack.team if slack else "not connected", bool(slack)))}
  {current}
  <form method="post" action="/slack">{hidden}
<label>Slack user token <input name="slack_token" type="password" autocomplete="off" required placeholder="xoxp-…"></label>
<p class="hint">For projects with a Slack channel: ShadowDev asks there for review of the PRs it opens, as you,
  and reviews the PRs you are mentioned on. Create a Slack app for yourself at <b>api.slack.com/apps</b>, add the
  <b>User Token Scopes</b> <code>chat:write</code>, <code>reactions:write</code>, <code>channels:history</code>
  and <code>groups:history</code>,
  install it to the workspace, and paste its <b>User OAuth Token</b>.</p>
<button>Save Slack account</button>
  </form>
</section>"""
