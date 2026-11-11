"""Building blocks shared by every page. Every value from outside is escaped with esc()."""
import html
from pathlib import Path

STATIC = Path(__file__).parent / "static"
STYLE = (STATIC / "style.css").read_text()

# The logo: a shape and its shadow. Inline, so the Content-Security-Policy needs no img-src.
MARK = ('<svg class="mark" viewBox="0 0 32 32" aria-hidden="true">'
        '<rect x="10" y="10" width="19" height="19" rx="5" fill="currentColor" opacity=".3"/>'
        '<rect x="3" y="3" width="19" height="19" rx="5" fill="currentColor"/></svg>')

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


def remove_form(action: str, field: str, value: int, hidden: str, what: str) -> str:
    return (f'<form method="post" action="{action}" data-confirm="Remove {esc(what)}?">{hidden}'
            f'<input type="hidden" name="{field}" value="{esc(value)}"><button class="secondary small">Remove</button></form>')


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def page(title: str, body: str, theme: str = "") -> str:
    """`theme`: one of THEMES, or "" for the system's."""
    chosen = f' data-theme="{esc(theme)}"' if theme in THEMES else ""
    return f"""<!doctype html>
<html lang="en"{chosen}><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{esc(title)}</title>
<style>
{STYLE}</style></head>
<body><main>{body}</main></body></html>"""
