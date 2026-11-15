"""The Live logs tab: a terminal of what the developer's own runs log as they work (static/logs.js
fills it from GET /logs/live). The server's own log, with every developer's runs, is not shown here:
the admin reads it in the server's terminal (make logs)."""


def logs_panel() -> str:
    return """<section class="card terminal-card" id="live-logs">
  <div class="card-head"><h3>Live logs</h3>
    <span class="tag off" data-live-state role="status">Starting…</span>
    <button type="button" class="secondary small" data-live-pause aria-pressed="false">Pause</button>
    <button type="button" class="secondary small" data-live-clear>Clear</button></div>
  <p class="muted">What your runs log while they work: what Claude reads, says and changes, the commands and their
    results. Only your own runs. A finished run's whole log stays under Recent runs, behind its <b>Log</b> link.</p>
  <div class="terminal" data-terminal role="log" aria-label="Live logs of your runs" tabindex="0">
    <p class="t-note">No run of yours is working. Lines appear here as soon as one starts.</p>
  </div>
  <noscript><p class="hint">Live logs need scripts. Without them, open a running run's <b>Log so far</b> under
    Recent runs.</p></noscript>
</section>"""
