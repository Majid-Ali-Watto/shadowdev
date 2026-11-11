// Tabs of the developer's page (WAI-ARIA tabs): a click or the arrow keys switch panels in place,
// and the address keeps ?tab=, so a reload or a shared link opens the same one. Without scripts
// each tab is a plain link to its ?tab=.
(() => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab, focus) => {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", on);
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
    const url = new URL(location.href);
    url.searchParams.set("tab", tab.dataset.tab);
    url.searchParams.delete("ok");  // a notice belongs to the page load that showed it
    history.replaceState(null, "", url);
  };
  for (const tab of tabs) {
    tab.addEventListener("click", e => { e.preventDefault(); select(tab, false); });
  }
  document.querySelector('[role="tablist"]').addEventListener("keydown", e => {
    const i = tabs.indexOf(document.activeElement);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || next === undefined) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
})();
