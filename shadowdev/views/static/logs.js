// Live logs: asks GET /logs/live every 1.5 seconds, while its tab is shown and the window is, for what the
// developer's working runs logged since, and adds it to the terminal, each line marked with its run. Lines
// are text (they hold what Claude and the repos' commands wrote), never HTML. It follows the newest line
// unless the developer scrolled up to read; Pause stops asking, Clear empties the terminal.
ShadowDev.mount(() => {
  const panel = document.getElementById("live-logs");
  if (!panel) return;
  const tab = panel.closest('[role="tabpanel"]');
  const terminal = panel.querySelector("[data-terminal]");
  const state = panel.querySelector("[data-live-state]");
  const pauseButton = panel.querySelector("[data-live-pause]");
  const KEEP = 3000;  // lines kept in the terminal; older ones are dropped
  const seen = new Map();  // run id -> lines of it shown so far
  const colours = new Map(), labels = new Map();  // run id -> its colour and label, kept while the page is open
  let paused = false, busy = false, latest = 0;

  const say = (text, kind) => {
    state.textContent = text;
    state.className = `tag ${kind}`;
  };
  const line = (run, text, note) => {
    const p = document.createElement("p");
    if (note) p.className = "t-note";
    if (run) {
      if (!colours.has(run.id)) colours.set(run.id, colours.size % 6);
      const label = document.createElement("span");
      label.className = `t-run c${colours.get(run.id)}`;
      label.textContent = run.label;
      p.append(label, " ");
    }
    p.append(text);
    return p;
  };
  const add = lines => {
    if (!lines.length) return;
    const following = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 40;
    terminal.querySelector(":scope > .t-note.t-idle")?.remove();
    terminal.append(...lines);
    while (terminal.childElementCount > KEEP) terminal.firstElementChild.remove();
    if (following) terminal.scrollTop = terminal.scrollHeight;
  };
  const idle = () => {
    if (terminal.childElementCount) return;
    const p = line(null, "No run of yours is working. Lines appear here as soon as one starts.", true);
    p.classList.add("t-idle");
    terminal.append(p);
  };
  terminal.textContent = "";
  idle();

  const ask = async () => {
    if (busy || paused || document.hidden || (tab && tab.hidden)) return;
    busy = true;
    const n = ++latest;
    const query = [...seen].map(([id, count]) => `${id}:${count}`).join(",");
    const response = await fetch("/logs/live?seen=" + encodeURIComponent(query)).catch(() => null);
    const data = response && response.ok ? await response.json().catch(() => null) : null;
    busy = false;
    if (n !== latest) return;
    if (!data) return say(response && response.status === 401 ? "Signed out" : "Not reachable", "warn");
    const out = [], working = new Set();
    for (const run of data.runs) {
      working.add(run.id);
      labels.set(run.id, run.label);
      if (!seen.has(run.id)) out.push(line(run, `following its log (${run.project})`, true));
      const skipped = run.total - (seen.get(run.id) || 0) - run.lines.length;
      if (seen.has(run.id) && skipped > 0) out.push(line(run, `… ${skipped} lines not kept`, true));
      for (const text of run.lines) out.push(line(run, text));
      seen.set(run.id, run.total);
    }
    for (const [id] of seen) {
      if (working.has(id)) continue;
      out.push(line({ id, label: labels.get(id) }, "ended: its whole log is under Recent runs", true));
      seen.delete(id);
    }
    add(out);
    if (!data.runs.length) idle();
    say(data.runs.length ? `Live · ${data.runs.length} working` : "Live · nothing working", data.runs.length ? "ok" : "off");
  };

  pauseButton.addEventListener("click", () => {
    paused = !paused;
    pauseButton.textContent = paused ? "Resume" : "Pause";
    pauseButton.setAttribute("aria-pressed", paused);
    if (paused) say("Paused", "warn"); else ask();
  });
  panel.querySelector("[data-live-clear]").addEventListener("click", () => {
    terminal.textContent = "";
    idle();
  });
  const timer = setInterval(ask, 1500);
  ask();
  return () => {
    latest++;  // an answer on its way is not shown
    clearInterval(timer);
  };
});
