// The signed-in pages work as one app: their links to each other and their forms load the next page in
// the background and swap it in, with a bar at the top meanwhile, instead of reloading the page. After a
// form's error the fields keep what the developer typed. Forms (or their buttons) with data-confirm ask
// first, in a dialog: data-confirm-title is its question, data-confirm its message, data-confirm-action
// the button that goes ahead (red with data-confirm-danger); see views/components.confirm.
// Each part of a page has its script register with ShadowDev.mount(fn): fn runs now and after every
// swap, and what it returns (a function) is called before the next swap, to stop its timers, streams and
// listeners on document. A part whose fn throws is reported and dropped: it does not run again on this page.
// Without scripts every link and form works as a plain one; pages outside the app (signing in, Atlassian)
// and forms marked data-reload load in full.
(() => {
  const mounts = new Set();  // the parts of the page: one entry per mount call, for as long as the page is loaded
  let cleanups = [];
  // Runs a part; false when it threw, so that it is dropped.
  const start = fn => {
    try {
      const stop = fn();
      if (typeof stop === "function") cleanups.push(stop);
      return true;
    } catch (e) {
      console.error(e);
      return false;
    }
  };
  window.ShadowDev = { mount(fn) { mounts.add(fn); if (!start(fn)) mounts.delete(fn); } };

  const main = document.querySelector("main");
  if (!main || !main.hasAttribute("data-app")) return;
  const PAGES = ["/", "/project"];  // the app's pages, loaded by GET
  const own = url => url.origin === location.origin && PAGES.includes(url.pathname);

  const bar = document.createElement("div");
  bar.className = "loading-bar";
  bar.hidden = true;
  document.body.prepend(bar);  // outside <main>, so swaps keep it

  // A failure that leaves the page as it is: said at the top of the page.
  const fail = text => {
    main.querySelector("[data-app-error]")?.remove();
    const p = document.createElement("p");
    p.className = "alert error";
    p.setAttribute("role", "alert");
    p.dataset.appError = "";
    p.textContent = text;
    main.prepend(p);
    p.scrollIntoView({ block: "nearest" });
  };
  const failure = status => ({
    401: "You were signed out. Reload the page to sign in again.",
    403: "That was refused. Reload the page and try again.",
    413: "The form is too large.",
    429: "Too many requests. Wait a moment, then try again.",
  })[status] || `Something went wrong (${status}). Try again, or reload the page.`;

  // The fields of a form the developer fills in, to put back what they typed after an error.
  const fields = form => [...form.elements].filter(el => el.name && !["hidden", "submit", "button", "file"].includes(el.type));
  const typed = form => fields(form).map(el => [el.name, el.type === "checkbox" || el.type === "radio" ? el.checked : el.value]);
  const retype = (form, values) => {
    const seen = {};
    const els = fields(form);
    for (const [name, value] of values) {
      const n = seen[name] = (seen[name] ?? -1) + 1;
      const el = els.filter(e => e.name === name)[n];
      if (!el) continue;
      if (el.type === "checkbox" || el.type === "radio") el.checked = value; else el.value = value;
      // Fields that depend on a choice (Change on the project picked) follow it, before they are put back.
      if (el.tagName === "SELECT") el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  };

  // Put the page `doc` in place of this one. `address`: what the address bar shows then (null: as it is),
  // `push`: as a new history entry. Returns false when `doc` is not a page of the app.
  const swap = (doc, address, push) => {
    const next = doc.querySelector("main[data-app]");
    if (!next) return false;
    next.querySelectorAll("script").forEach(s => s.remove());  // this page's scripts run the new one
    cleanups.forEach(stop => { try { stop(); } catch (e) { console.error(e); } });
    cleanups = [];
    document.title = doc.title;
    main.replaceChildren(...next.childNodes);
    if (address) history[push ? "pushState" : "replaceState"](null, "", address);
    for (const fn of [...mounts]) if (!start(fn)) mounts.delete(fn);  // a part that throws is dropped
    return true;
  };

  let latest = 0;  // only the answer to the latest request is shown
  // Load `url` (with fetch `init`) and show it. `how`: "push" for a new page, "pop" for back and
  // forward, "form" for what a form's answer brings, `form` that form.
  const load = async (url, init = {}, how = "push", form = null) => {
    const n = ++latest;
    bar.hidden = false;
    const values = form && typed(form);
    const index = form ? [...main.querySelectorAll(`form[action="${CSS.escape(form.getAttribute("action"))}"]`)].indexOf(form) : -1;
    try {
      let response;
      try {
        response = await fetch(url, { credentials: "same-origin", ...init });
      } catch {
        if (n === latest) fail("ShadowDev could not be reached. Check your connection, then try again.");
        return false;
      }
      if (n !== latest) return false;
      const final = new URL(response.url);
      const isPage = (response.headers.get("Content-Type") || "").startsWith("text/html");
      if (!isPage) {
        fail(failure(response.status));
        return false;
      }
      const doc = new DOMParser().parseFromString(await response.text(), "text/html");
      if (n !== latest) return false;
      // A form answered with a page (an error) has the form's address: the address stays as it is.
      const address = how === "pop" ? null : !form || response.redirected ? final.href : null;
      if (!swap(doc, address, how !== "pop")) {
        // Not a page of the app (signed out, say): the browser loads it in full, if it can be loaded again.
        if (own(final) && (!form || response.redirected)) location.assign(final.href);
        else fail(failure(response.status));
        return false;
      }
      if (form && !response.ok && index >= 0) {
        const again = main.querySelectorAll(`form[action="${CSS.escape(form.getAttribute("action"))}"]`)[index];
        if (again) retype(again, values);
      }
      const target = (final.hash && document.getElementById(decodeURIComponent(final.hash.slice(1))))
        || (form && main.querySelector(".alert"));
      if (target) target.scrollIntoView({ block: form ? "nearest" : "start" });
      else if (!form) window.scrollTo(0, 0);
      if (!form) {
        main.tabIndex = -1;
        main.focus({ preventScroll: true });  // screen readers start at the new page
      }
      return true;
    } finally {
      if (n === latest) bar.hidden = true;
    }
  };

  document.addEventListener("click", e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const link = e.target.closest("a[href]");
    if (!link || link.target || link.hasAttribute("download") || link.hasAttribute("data-reload")) return;
    const url = new URL(link.href);
    if (!own(url)) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;  // on this page
    e.preventDefault();
    load(url);
  });

  // The dialog that asks before a form is sent: made once, outside <main>, so swaps keep it.
  let dialog;
  const ask = ({ confirm: message, confirmTitle: title, confirmAction: action, confirmDanger: danger }) => new Promise(resolve => {
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.className = "confirm";
      dialog.setAttribute("aria-labelledby", "confirm-title");
      dialog.setAttribute("aria-describedby", "confirm-message");
      dialog.innerHTML = '<form method="dialog"><h3 id="confirm-title"></h3><p id="confirm-message"></p>'
        + '<div class="actions"><button value="cancel" class="secondary" data-cancel>Cancel</button>'
        + '<button value="ok" data-ok></button></div></form>';
      dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close("cancel"); });  // outside it
      document.body.append(dialog);
    }
    dialog.querySelector("h3").textContent = title || "Are you sure?";
    dialog.querySelector("p").textContent = message;
    const ok = dialog.querySelector("[data-ok]");
    ok.textContent = action || "Continue";
    ok.className = danger === undefined ? "" : "danger-solid";
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    dialog.querySelector("[data-cancel]").focus();  // Enter alone never goes ahead
  });
  let confirmed = null;  // the form being sent after its dialog said yes

  document.addEventListener("submit", e => {
    const form = e.target;
    const asker = e.submitter && e.submitter.dataset.confirm ? e.submitter : form.dataset.confirm ? form : null;
    if (asker && confirmed !== form) {
      e.preventDefault();
      const submitter = e.submitter;
      ask(asker.dataset).then(yes => {
        if (!yes || !form.isConnected) return;
        confirmed = form;
        try {
          if (submitter) form.requestSubmit(submitter); else form.requestSubmit();
        } finally {
          confirmed = null;
        }
      });
      return;
    }
    if (e.defaultPrevented || form.method !== "post" || form.hasAttribute("data-reload")) return;
    const url = new URL(form.action);
    if (url.origin !== location.origin) return;
    e.preventDefault();
    if (form.getAttribute("aria-busy") === "true") return;  // sent already
    let data;
    try {
      data = new FormData(form, e.submitter);
    } catch {  // a browser without the submitter argument
      data = new FormData(form);
      if (e.submitter && e.submitter.name) data.append(e.submitter.name, e.submitter.value);
    }
    const buttons = [...form.querySelectorAll("button"), ...(e.submitter ? [e.submitter] : [])];
    form.setAttribute("aria-busy", "true");
    buttons.forEach(b => b.disabled = true);
    e.submitter?.classList.add("busy");
    load(url, { method: "POST", body: new URLSearchParams(data) }, "form", form).then(swapped => {
      if (swapped) return;  // the form was replaced
      form.removeAttribute("aria-busy");
      buttons.forEach(b => b.disabled = false);
      e.submitter?.classList.remove("busy");
    });
  });

  window.addEventListener("popstate", () => load(new URL(location.href), {}, "pop"));
})();
