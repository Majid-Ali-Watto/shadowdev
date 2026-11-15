"""The Activity tab: what was done to the developer's account and projects, a page at a time."""
import time

from .components import esc


def activity_panel(rows: list[tuple[float, str, str]], page: int, pages: int) -> str:
    """`rows`, `page`, `pages`: UserRepository.activity. A change the developer did not make shows here."""
    items = "".join(
        f'<li><span>{esc(action)}{f" <span class=muted>{esc(detail)}</span>" if detail else ""}</span>'
        f'<time class="muted" datetime="{time.strftime("%Y-%m-%dT%H:%M", time.localtime(at))}">'
        f'{time.strftime("%d %b %H:%M", time.localtime(at))}</time></li>' for at, action, detail in rows)
    return f"""<section class="card">
  <div class="card-head"><h3>Recent activity</h3>{_pager(page, pages)}</div>
  <p class="muted">Sign-ins and changes to your accounts, settings and projects. Something here you did not do?
    Remove the tokens you saved, and revoke them where they were made.</p>
  <ul class="items">{items or '<li class="muted">Nothing yet.</li>'}</ul>
</section>"""


def _pager(page: int, pages: int) -> str:
    if pages == 1:
        return ""

    def link(to: int, text: str, rel: str) -> str:
        if not 1 <= to <= pages:
            return f'<span class="button secondary small" aria-disabled="true">{text}</span>'
        return f'<a class="button secondary small" href="/?tab=activity&amp;activity_page={to}" rel="{rel}">{text}</a>'
    return (f'<nav class="pager" aria-label="Pages of activity">{link(page - 1, "Previous", "prev")}'
            f'<span class="muted">Page {page} of {pages}</span>{link(page + 1, "Next", "next")}</nav>')
