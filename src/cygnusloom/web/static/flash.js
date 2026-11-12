(() => {
  // A flash is a one-time page notice. Dismissing it removes it without a reload; the
  // campaign workspace keeps the page loaded between views, so it would otherwise stay up.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-dismiss-flash]");
    if (!button) return;
    button.closest(".flash")?.remove();
  });
})();
