// The project editor's repos: add another from the template, remove one (always keeping one to fill in).
(() => {
  const repos = document.getElementById("repos");
  const addRepo = () => repos.appendChild(document.getElementById("repo-template").content.cloneNode(true));
  document.getElementById("add-repo").addEventListener("click", addRepo);
  repos.addEventListener("click", e => {
    const remove = e.target.closest("[data-remove-repo]");
    if (!remove) return;
    remove.closest("fieldset").remove();
    if (!repos.querySelector("fieldset")) addRepo();
  });
})();
