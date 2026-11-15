// Fields of Jira statuses, separated by commas (data-statuses): the statuses suggested complete the last
// one typed. A datalist with data-src is filled from there the first time its field is used.
ShadowDev.mount(() => {
  const all = new Map();  // datalist -> its statuses
  const load = async list => {
    if (all.has(list)) return all.get(list);
    all.set(list, [...list.options].map(o => o.value));
    if (list.dataset.src) {
      const response = await fetch(list.dataset.src, { credentials: "same-origin" }).catch(() => null);
      if (response && response.ok) all.set(list, (await response.text()).split("\n").filter(Boolean));
    }
    return all.get(list);
  };
  const suggest = async e => {
    const input = e.target;
    if (!(input instanceof HTMLInputElement) || !input.hasAttribute("data-statuses") || !input.list) return;
    const list = input.list, statuses = await load(list);
    const before = input.value.split(",").slice(0, -1).map(s => s.trim()).filter(Boolean);
    const prefix = before.length ? before.join(", ") + ", " : "";
    list.replaceChildren(...statuses.filter(s => !before.includes(s)).map(s => new Option(prefix + s, prefix + s)));
  };
  document.addEventListener("focusin", suggest);
  document.addEventListener("input", suggest);
  return () => {
    document.removeEventListener("focusin", suggest);
    document.removeEventListener("input", suggest);
  };
});
