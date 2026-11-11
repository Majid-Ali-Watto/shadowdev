// The theme switcher: light or dark. The choice is kept in a cookie so the server renders every page
// in it (no flash of the other theme); until one is chosen, pages follow the system's setting.
(() => {
  const button = document.querySelector("[data-theme-toggle]");
  if (!button) return;
  const root = document.documentElement;
  const system = matchMedia("(prefers-color-scheme: dark)");
  const current = () => root.dataset.theme || (system.matches ? "dark" : "light");
  const other = () => (current() === "dark" ? "light" : "dark");
  const show = () => {
    const next = other();
    button.setAttribute("aria-label", `Switch to ${next} theme`);
    button.title = `Switch to ${next} theme`;
    for (const icon of button.querySelectorAll("[data-icon]")) icon.style.display = icon.dataset.icon === next ? "" : "none";
  };
  button.addEventListener("click", () => {
    root.dataset.theme = other();
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `shadowdev_theme=${root.dataset.theme}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
    show();
  });
  system.addEventListener("change", show);
  show();
  button.hidden = false;
})();
