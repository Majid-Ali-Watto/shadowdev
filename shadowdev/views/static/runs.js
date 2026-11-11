// Keeps Recent runs up to date: every 5 seconds while a run is queued or running, else every 30,
// keeping the runs the developer ticked and the details they opened. Also the select-all box and the Delete selected button.
(() => {
  const runs = document.getElementById("runs");
  const pickAll = document.getElementById("pick-all");
  const deleteSelected = document.getElementById("delete-selected");
  const boxes = () => [...runs.querySelectorAll('input[name="run_id"]')];
  const sync = () => {
    const all = boxes(), picked = all.filter(b => b.checked).length;
    deleteSelected.disabled = !picked;
    deleteSelected.textContent = picked ? `Delete selected (${picked})` : "Delete selected";
    pickAll.disabled = !all.length;
    pickAll.checked = !!all.length && picked === all.length;
    pickAll.indeterminate = picked > 0 && picked < all.length;
  };
  pickAll.addEventListener("change", () => { boxes().forEach(b => b.checked = pickAll.checked); sync(); });
  runs.addEventListener("change", sync);
  sync();
  let tick = 0;
  setInterval(async () => {
    const busy = runs.querySelector(".badge.queued, .badge.running, .badge.stopping");
    if (document.hidden || (++tick % 6 && !busy)) return;
    const response = await fetch("/runs").catch(() => null);
    if (!response || !response.ok) return;
    const ticked = new Set(boxes().filter(b => b.checked).map(b => b.value));
    const opened = new Set([...runs.querySelectorAll("details[open]")].map(d => d.dataset.run));
    runs.innerHTML = await response.text();
    boxes().forEach(b => b.checked = ticked.has(b.value));
    runs.querySelectorAll("details[data-run]").forEach(d => d.open = opened.has(d.dataset.run));
    sync();
  }, 5000);
})();
