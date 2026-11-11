// Asks before forms marked data-confirm (or their submit button) are sent.
document.addEventListener("submit", e => {
  const ask = (e.submitter && e.submitter.dataset.confirm) || e.target.dataset.confirm;
  if (ask && !confirm(ask)) e.preventDefault();
});
