"""The pages before signing in: sign in with Jira, and the hop to Atlassian to connect another account."""
from .components import MARK, THEME_TOGGLE, alert, esc, page, script


def login_page(public_url: str, nonce: str, theme: str = "", error: str = "") -> str:
    return page("Sign in · ShadowDev", f"""
<section class="card login">
  {THEME_TOGGLE}
  {MARK}
  <h1>ShadowDev</h1>
  <p class="lead">Automates your Jira bugs and tasks: Claude analyses the issue, writes the change
  across the project's repos, runs their tests and opens a pull request per changed repo for you to
  review.</p>
  {alert(error)}
  <a class="button block" href="{esc(public_url)}/login/jira">Sign in with Jira</a>
  <p class="hint">Opens Atlassian, where you grant ShadowDev access to Jira. It then reads the issues
    assigned to you and comments on them as you. No API token needed.</p>
  <p class="hint">Already use ShadowDev with another Jira account, e.g. your office one? Sign in with
    that one, and connect this one from your page, so all your projects are in one place.</p>
</section>
{script("theme.js", nonce)}""", theme)


def continue_page(url: str, nonce: str, theme: str = "") -> str:
    """Opens `url` (Atlassian's consent page) at once; the link is there if scripts are off."""
    return page("Connecting Jira · ShadowDev", f"""
<section class="card login">
  {MARK}
  <h1>Connecting another Jira account</h1>
  <p class="lead">Opening Atlassian… Choose the Jira site and account to connect, and press Accept.</p>
  <a class="button block" id="continue" href="{esc(url)}">Continue to Atlassian</a>
</section>
{script("continue.js", nonce)}""", theme)
