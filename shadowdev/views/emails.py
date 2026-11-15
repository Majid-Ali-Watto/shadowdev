"""The HTML of the emails developers get about their own work (clients/mail sends them, with a plain
text version beside). Mail apps drop <style> and run no script, so styles are inline and layout is a
table. What a question says comes from Claude, and status names from Jira: every value is escaped."""
from ..models.project import Project
from ..models.release import StatusRule
from .components import esc

FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
TEXT, MUTED, LINE, ACCENT = "#111827", "#545e6e", "#e3e6ec", "#4f46e5"
TAG = "display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:{};color:{}"


def decision(questions: dict[str, list[str]], site_url: str, note: str = "") -> str:
    """A run stopped to ask the developer: each issue, linked, with its questions. `note`: said
    first, above the title (a test message says it is one)."""
    rows = "".join(
        _row(_issue(site_url, key), "".join(f'<p style="margin:6px 0 0;color:{TEXT}">{esc(q)}</p>' for q in qs))
        for key, qs in questions.items())
    return _page(f"Your decision is needed on {_count(len(questions), 'issue')}",
                 "Work on these issues waits for your answer:", rows,
                 "Answer in a note when you run them again from <b>Run issues now</b>: a line per issue, "
                 "starting with its key.", note)


def missing(project: Project, gaps_of: list[tuple[str, StatusRule, list[str]]], note: str = "") -> str:
    """Tickets the project's status rules held: each, linked, where it stays and what it misses."""
    rows = "".join(
        _row(_issue(project.site_url, key),
             f'<p style="margin:6px 0 0;color:{TEXT}">Stays in {_tag(rule.from_status, "#f0f2f6", TEXT)} '
             f'until it has <b>{esc(", ".join(gaps))}</b>, for {_tag(rule.to_status, "#e9f7ee", "#15803d")}</p>')
        for key, rule, gaps in gaps_of)
    return _page(f"{_count(len(gaps_of), 'ticket')} not complete yet", f"In {esc(project.name)}, these were not moved:",
                 rows, "Add what each misses in Jira; the next check moves it.", note)


def _page(title: str, intro: str, rows: str, footer: str, note: str = "") -> str:
    banner = (f'<p style="margin:0 0 12px;padding:8px 12px;border-radius:8px;background:#fff5e5;color:#a15c07;'
              f'font-size:13px">{esc(note)}</p>') if note else ""
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px 12px;background:#f4f5f8;{FONT}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;margin:0 auto;
  background:#ffffff;border:1px solid {LINE};border-radius:12px">
<tr><td style="padding:24px 24px 8px">{banner}
  <h1 style="margin:0;font-size:20px;color:{TEXT}">{esc(title)}</h1>
  <p style="margin:8px 0 0;font-size:14px;color:{MUTED}">{intro}</p></td></tr>
{rows}
<tr><td style="padding:16px 24px 24px;font-size:13px;color:{MUTED}">{footer}</td></tr>
</table></body></html>"""


def _row(head: str, body: str) -> str:
    return (f'<tr><td style="padding:14px 24px;border-top:1px solid {LINE};font-size:14px;line-height:1.5">'
            f"{head}{body}</td></tr>")


def _issue(site_url: str, key: str) -> str:
    return (f'<a href="{esc(site_url)}/browse/{esc(key)}" style="color:{ACCENT};font-weight:600;'
            f'text-decoration:none">{esc(key)}</a>')


def _tag(text: str, background: str, color: str) -> str:
    return f'<span style="{TAG.format(background, color)}">{esc(text)}</span>'


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"
