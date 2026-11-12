// Recent runs: searches as the developer types (once they pause), filters (from a popup) and pages without a reload,
// and keeps what is shown up to date: every 5 seconds while a run is queued or running, else every 30,
// keeping the runs the developer ticked and the details they opened. Also the select-all box and the
// Delete selected button. Without scripts the filter is a plain form and the pages plain links.
(() => {
  const results = document.getElementById("runs-results");
  const filter = document.getElementById("run-filter");
  const said = document.getElementById("runs-status");
  const deleteSelected = document.getElementById("delete-selected");
  const boxes = () => [...results.querySelectorAll('input[name="run_id"]')];
  const sync = () => {
    const pickAll = document.getElementById("pick-all");
    const all = boxes(), picked = all.filter(b => b.checked).length;
    deleteSelected.disabled = !picked;
    deleteSelected.textContent = picked ? `Delete selected (${picked})` : "Delete selected";
    pickAll.disabled = !all.length;
    pickAll.checked = !!all.length && picked === all.length;
    pickAll.indeterminate = picked > 0 && picked < all.length;
  };
  results.addEventListener("change", e => {
    if (e.target.id === "pick-all") boxes().forEach(b => b.checked = e.target.checked);
    sync();
  });
  sync();

  // What is wanted: the query of the last search, filter or page asked for. Only the answer to the
  // latest request is shown, so a slow one can't overwrite a newer search. `moved`: a new search,
  // filter or page, not a refresh; `unsettled` while one is on its way.
  let wanted = results.querySelector("[data-query]").dataset.query, latest = 0, unsettled = false;
  const load = async (query, moved) => {
    wanted = query;
    unsettled = moved;
    const n = ++latest;
    const response = await fetch("/runs?" + query).catch(() => null);
    if (n !== latest || !response || !response.ok) return;
    const html = await response.text();
    if (n !== latest) return;
    const ticked = new Set(boxes().filter(b => b.checked).map(b => b.value));
    const opened = new Set([...results.querySelectorAll("details[open]")].map(d => d.dataset.run));
    results.innerHTML = html;
    boxes().forEach(b => b.checked = ticked.has(b.value));
    results.querySelectorAll("details[data-run]").forEach(d => d.open = opened.has(d.dataset.run));
    sync();
    if (!moved) return;
    unsettled = false;
    const summary = results.querySelector("[data-query]");
    wanted = summary.dataset.query;  // the page the server settled on
    history.replaceState(null, "", "/?" + wanted);  // a reload or a shared link shows the same
    said.textContent = summary.textContent;
    showApplied();
  };

  // The filters live in a popup: they apply on Apply (or Clear), the search as the developer types,
  // with the filters in use. Closing the popup without applying puts back the ones in use.
  const menu = document.getElementById("filter-menu");
  const count = menu.querySelector("[data-count]");
  const FILTERS = ["status", "project", "kind"];
  const showApplied = () => {
    const query = new URLSearchParams(wanted);
    for (const name of FILTERS) filter.elements[name].value = query.get(name) || "";
    const used = FILTERS.filter(name => query.get(name)).length;
    count.textContent = used;
    count.hidden = !used;
  };
  const apply = () => {
    clearTimeout(typing);
    const query = new URLSearchParams();  // without a page: new filters start at the first
    for (const [name, value] of new FormData(filter)) if (value.trim()) query.set(name, value.trim());
    menu.open = false;
    load(query.toString(), true);
  };
  const search = () => {
    clearTimeout(typing);
    const query = new URLSearchParams(wanted), text = filter.q.value.trim();
    query.delete("page");
    if (text) query.set("q", text); else query.delete("q");
    load(query.toString(), true);
  };
  let typing;
  filter.q.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(search, 350); });
  filter.addEventListener("submit", e => {
    e.preventDefault();
    if (e.submitter && menu.contains(e.submitter)) apply(); else search();
  });
  menu.querySelector("[data-clear]").addEventListener("click", e => {
    e.preventDefault();
    for (const name of FILTERS) filter.elements[name].value = "";
    apply();
  });
  menu.addEventListener("toggle", () => {
    if (menu.open) filter.elements.status.focus(); else showApplied();
  });
  menu.addEventListener("keydown", e => {
    if (e.key !== "Escape" || !menu.open) return;
    menu.open = false;
    menu.querySelector("summary").focus();
  });
  document.addEventListener("click", e => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  results.addEventListener("click", e => {  // the pages, and the filters in use (removing one)
    const link = e.target.closest("a[data-nav]");
    if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    load(new URL(link.href).search.slice(1), true);
  });

  let tick = 0;
  setInterval(() => {
    const busy = results.querySelector(".badge.queued, .badge.running, .badge.stopping");
    if (document.hidden || (++tick % 6 && !busy)) return;
    load(wanted, unsettled);
  }, 5000);
})();
