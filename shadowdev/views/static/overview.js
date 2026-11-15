// Overview: loads itself again from GET /overview whenever Recent runs (static/runs.js) refreshes, which
// happens as the server says a run changed and on its own timer, so both use one live connection.
// The number of things that need the developer, on the sidebar's Overview, follows.
ShadowDev.mount(() => {
  const panel = document.getElementById("panel-overview");
  if (!panel) return;
  // One request at a time; a change heard meanwhile loads it once more afterwards.
  let latest = 0, busy = false, again = false;
  const load = async () => {
    if (busy) { again = true; return; }
    busy = true;
    const n = ++latest;
    const response = await fetch("/overview").catch(() => null);
    const html = response && response.ok ? await response.text().catch(() => null) : null;
    busy = false;
    if (n !== latest) return;
    if (again) { again = false; load(); }
    if (html === null) return;
    panel.querySelector("#overview").outerHTML = html;
    const needs = panel.querySelector("#overview").dataset.needs;
    const count = document.querySelector("[data-needs-count]");
    if (count) {
      count.textContent = needs;
      count.setAttribute("aria-label", `${needs} need you`);
      count.hidden = needs === "0";
    }
  };
  document.addEventListener("shadowdev:runs", load);
  return () => {
    latest++;  // an answer on its way is not shown
    document.removeEventListener("shadowdev:runs", load);
  };
});
