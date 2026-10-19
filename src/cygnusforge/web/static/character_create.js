(() => {
  // Only the tables of the chosen template are shown, and only those are required: a hidden
  // required radio would block the form with a message the player cannot see.
  const revealTables = (kind) => {
    const chosen = document.querySelector(`input[name="template-${kind}"]:checked`);
    for (const group of document.querySelectorAll(`[data-template-group="${kind}"]`)) {
      const show = chosen !== null && group.dataset.template === chosen.value;
      group.hidden = !show;
      for (const radio of group.querySelectorAll('input[type="radio"]')) {
        radio.required = show;
      }
    }
  };

  document.addEventListener("change", (event) => {
    const kind = event.target.dataset?.templateSelect;
    if (kind) revealTables(kind);
  });

  const kinds = new Set(
    [...document.querySelectorAll("[data-template-select]")].map((radio) => radio.dataset.templateSelect),
  );
  for (const kind of kinds) revealTables(kind);

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-dice-for]");
    if (!button) return;
    const name = button.dataset.diceFor;
    const options = [...document.querySelectorAll(`input[type="radio"][name="${name}"]`)].filter(
      (radio) => !radio.closest("[hidden]"),
    );
    if (options.length === 0) return;
    const pick = options[Math.floor(Math.random() * options.length)];
    pick.checked = true;
    pick.dispatchEvent(new Event("change", { bubbles: true }));
  });
})();
