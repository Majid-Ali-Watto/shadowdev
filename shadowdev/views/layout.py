"""The frame of every signed-in page: a sidebar with the sections of the developer's page and who is
signed in, beside the page's own content. On the developer's page the sections are its tabs
(static/tabs.js switches them in place); elsewhere (the project editor) they are links back to them.
On a narrow screen the sidebar is a bar on top (style.css)."""
from ..models.user import User
from .components import MARK, THEME_TOGGLE, confirm, esc, plural

TABS = (("overview", "Overview"), ("runs", "Run issues"), ("recent", "Recent runs"), ("logs", "Live logs"), ("projects", "Projects"),
        ("accounts", "Accounts"), ("settings", "Settings"), ("activity", "Activity"), ("features", "Features"))

# Each section's icon in the sidebar (24x24 outline paths, drawn in the text's colour).
ICONS = {
    "overview": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/>'
                '<rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "runs": '<path d="M6 4l14 8-14 8z"/>',
    "recent": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "logs": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    "projects": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "accounts": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/>'
                '<circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "features": '<path d="M12 3l2.6 5.6 6 .7-4.5 4.1 1.2 6L12 16.4 6.7 19.4l1.2-6L3.4 9.3l6-.7z"/>',
}


def _icon(key: str) -> str:
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS[key]}</svg>')



def automation_state(user: User) -> tuple[str, str, str, str]:
    """Whether automation works for the developer: (kind, "ok" or "warn"; a word or two, for the
    sidebar; the sentence, for Overview; where to act on it)."""
    ready = user.ready_projects
    if user.paused:
        return ("warn", "Paused", "Paused: nothing runs for you until you resume. Your settings are kept.",
                "/?tab=settings")
    if not ready:
        return ("warn", "Not set up yet", "Not active yet: connect GitHub and Claude, and join a project with them.",
                "/?tab=accounts" if not (user.github_accounts and user.claude_accounts) else "/?tab=projects")
    return ("ok", f"Active · {plural(len(ready), 'project')}",
            f"Active in {plural(len(ready), 'project')}: issues assigned to you there are worked on automatically.",
            "/?tab=projects")


def sidebar(user: User, hidden: str, current: str, needs: int, tabs: bool) -> str:
    """`current`: the section shown, or the one the page is part of (the project editor: projects).
    `needs`: how many things need the developer (overview.needs_count), on Overview. `tabs`: the
    sections are this page's tabs; else links to them."""
    def link(key: str, label: str) -> str:
        on = key == current
        attrs = (f' id="tab-{key}" role="tab" aria-controls="panel-{key}" data-tab="{key}"'
                 f' aria-selected="{"true" if on else "false"}" tabindex="{0 if on else -1}"' if tabs
                 else ' aria-current="page"' if on else "")
        count = (f'<span class="count" data-needs-count aria-label="{needs} need you"{"" if needs else " hidden"}>{needs}</span>'
                 if key == "overview" else "")
        return f'<a href="/?tab={key}"{attrs}>{_icon(key)}<span>{label}</span>{count}</a>'
    nav_role = ' role="tablist" aria-orientation="vertical"' if tabs else ""
    kind, word, sentence, href = automation_state(user)
    sign_out = confirm("Sign out?", "Only this browser is signed out. Automation keeps working on your issues.", "Sign out",
                       danger=False)
    return f"""<aside class="side">
  <div class="side-brand">{MARK}<span>ShadowDev</span></div>
  <a class="side-status {kind}" href="{href}" title="{esc(sentence)}" role="status">{esc(word)}</a>
  <nav class="side-nav"{nav_role} aria-label="Sections">{"".join(link(k, label) for k, label in TABS)}</nav>
  <div class="side-foot">
    <p class="side-user">Signed in as {esc(user.display_name)}{f" ({esc(user.email)})" if user.email else ""}</p>
    <div class="side-actions">{THEME_TOGGLE}
      <form method="post" action="/logout" data-reload{sign_out}>{hidden}<button class="secondary">Sign out</button></form></div>
  </div>
</aside>"""


def shell(side: str, content: str) -> str:
    return f'<div class="shell">\n{side}\n<div class="content">\n{content}\n</div>\n</div>\n'


def page_head(title: str, crumbs: tuple[tuple[str, str], ...], here: str, intro: str = "") -> str:
    """A page's title, under the way back to where it belongs: `crumbs`, (label, link) each, then
    `here`, this page (none for a page of its own). `intro` (HTML) says what the page is for."""
    trail = "".join(f'<a href="{esc(href)}">{esc(label)}</a><span aria-hidden="true">/</span>' for label, href in crumbs)
    way = (f'<nav class="crumbs" aria-label="Breadcrumb">{trail}<span aria-current="page">{esc(here)}</span></nav>'
           if crumbs else "")
    return (f'<header class="page-head">{way}<h1>{esc(title)}</h1>'
            + (f'<p class="muted">{intro}</p>' if intro else "") + "</header>")
