"""Building blocks shared by every page. Every value from outside is escaped with esc()."""
import base64
import html
from pathlib import Path

STATIC = Path(__file__).parent / "static"
STYLE = (STATIC / "style.css").read_text()

# The logo (assets/logo.jpg), inlined as a data: URI so pages load nothing
# else: the Content-Security-Policy allows images only from data: (routes/request.html).
LOGO = "data:image/jpeg;base64," + base64.b64encode((Path(__file__).parent / "assets" / "logo.jpg").read_bytes()).decode()
MARK = f'<img class="mark" src="{LOGO}" alt="">'

THEMES = ("light", "dark")  # what the developer can choose; no choice follows the system's setting

# The theme switcher: hidden until static/theme.js runs, which shows the icon of the theme it switches to.
THEME_TOGGLE = ('<button type="button" class="secondary theme-toggle" data-theme-toggle hidden>'
                '<svg data-icon="dark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>'
                '<svg data-icon="light" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/>'
                '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
                '</button>')

# Scripts are inlined with the page's nonce (the Content-Security-Policy allows no other script).
_SCRIPTS = {path.name: path.read_text() for path in STATIC.glob("*.js")}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def script(name: str, nonce: str) -> str:
    """static/<name> as an inline script carrying the page's nonce."""
    return f'<script nonce="{esc(nonce)}">\n{_SCRIPTS[name]}</script>'


# The signed-in pages' scripts. Every one of those pages has all of them, so moving between them
# without a reload (static/app.js) finds each part's script there; each looks for its own elements.
APP_SCRIPTS = ("app.js", "theme.js", "tabs.js", "runs.js", "overview.js", "logs.js", "project_editor.js", "statuses.js")


def app_scripts(nonce: str) -> str:
    return "".join(script(name, nonce) for name in APP_SCRIPTS)


def csrf_field(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(csrf)}">'


def alert(message: str, kind: str = "error") -> str:
    if not message:
        return ""
    return f'<p class="alert {kind}" role="{"alert" if kind == "error" else "status"}">{esc(message)}</p>'


def tag(text: str, ok: bool) -> str:
    return f'<span class="tag {"ok" if ok else "off"}">{esc(text)}</span>'


def card_head(step: int, title: str, done: bool, tag_html: str = "") -> str:
    """A setup step's heading: its number (a check once done), title, and a tag on the right."""
    return (f'<div class="card-head"><span class="step{" done" if done else ""}" aria-hidden="true">{step}</span>'
            f'<h3>{esc(title)}</h3>{tag_html}</div>')


def options(items: list[tuple[object, str]], selected=None, empty: str = "") -> str:
    first = f'<option value="">{esc(empty)}</option>' if empty else ""
    return first + "".join(f'<option value="{esc(v)}"{" selected" if v == selected else ""}>{esc(label)}</option>'
                           for v, label in items)


def confirm(title: str, message: str, action: str, danger: bool = True) -> str:
    """Attributes that make a form, or one of its buttons, ask first in static/app.js's dialog: its
    `title` (a question), `message` (what follows), and the button that goes ahead (`action`), red
    when `danger` (it removes or stops something). Without scripts the form is sent as it is."""
    return (f' data-confirm="{esc(message)}" data-confirm-title="{esc(title)}" data-confirm-action="{esc(action)}"'
            + (" data-confirm-danger" if danger else ""))


def remove_form(action: str, field: str, value: int, hidden: str, what: str, message: str) -> str:
    """A Remove button, which asks first: "Remove `what`?", and `message`, what removing it does."""
    return (f'<form method="post" action="{action}"{confirm(f"Remove {what}?", message, "Remove")}>{hidden}'
            f'<input type="hidden" name="{field}" value="{esc(value)}"><button class="secondary small">Remove</button></form>')


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def page(title: str, body: str, theme: str = "", app: bool = False) -> str:
    """`theme`: one of THEMES, or "" for the system's. `app`: a signed-in page, which static/app.js
    replaces with another without a reload (with app_scripts in its body)."""
    chosen = f' data-theme="{esc(theme)}"' if theme in THEMES else ""
    return f"""<!doctype html>
<html lang="en"{chosen}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<link rel="icon" type="image/jpeg" href="{LOGO}">
<style>
{STYLE}</style></head>
<body><main{" data-app" if app else ""}>{body}</main></body></html>"""
