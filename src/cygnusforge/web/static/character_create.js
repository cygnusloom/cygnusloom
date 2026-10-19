(() => {
  document.querySelectorAll("[data-template-select]").forEach((select) => {
    const reveal = () => {
      const groups = document.querySelectorAll(`[data-template-group="${select.dataset.templateSelect}"]`);
      for (const group of groups) {
        group.hidden = group.dataset.template !== select.value;
      }
    };
    select.addEventListener("change", reveal);
    reveal();
  });
})();
