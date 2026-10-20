(() => {
  const form = document.querySelector(".character-form");

  // Only the tables of the chosen template are shown. Hidden tables are skipped on submit.
  const revealTables = (kind) => {
    const chosen = document.querySelector(`input[name="template-${kind}"]:checked`);
    for (const group of document.querySelectorAll(`[data-template-group="${kind}"]`)) {
      group.hidden = chosen === null || group.dataset.template !== chosen.value;
    }
  };

  const refreshSummary = (name) => {
    const output = document.querySelector(`[data-summary-for="${name}"]`);
    if (!output) return;
    const chosen = document.querySelector(`input[type="radio"][name="${name}"]:checked`);
    output.textContent = chosen ? chosen.dataset.title : "Not chosen";
    output.classList.toggle("is-chosen", chosen !== null);
  };

  // Firefox can restore radio state on reload, so summaries are rebuilt from the DOM at load.
  for (const output of document.querySelectorAll("[data-summary-for]")) {
    refreshSummary(output.dataset.summaryFor);
  }
  const kinds = new Set(
    [...document.querySelectorAll("[data-template-select]")].map((radio) => radio.dataset.templateSelect),
  );
  for (const kind of kinds) revealTables(kind);

  document.addEventListener("change", (event) => {
    const radio = event.target;
    if (!(radio instanceof HTMLInputElement) || radio.type !== "radio") return;
    refreshSummary(radio.name);
    radio.closest("[data-choice-panel]")?.classList.remove("is-missing");
    if (radio.dataset.templateSelect) revealTables(radio.dataset.templateSelect);
  });

  // The list scrolls, so a chosen option is brought into view when the dialog opens.
  const openDialog = (id) => {
    const dialog = document.getElementById(id);
    if (!dialog || dialog.open) return;
    dialog.showModal();
    const chosen = dialog.querySelector('input[type="radio"]:checked');
    chosen?.closest(".choice")?.scrollIntoView({ block: "nearest" });
  };

  const rollChoice = (name) => {
    const options = [...document.querySelectorAll(`input[type="radio"][name="${name}"]`)].filter(
      (radio) => !radio.closest("[hidden]"),
    );
    if (options.length === 0) return;
    const pick = options[Math.floor(Math.random() * options.length)];
    pick.checked = true;
    pick.dispatchEvent(new Event("change", { bubbles: true }));
  };

  // Suggests a name from the chosen templates' names, or from every template's names when no
  // chosen template suggests any. The name typed by the player is replaced.
  const rollName = () => {
    const input = form.querySelector('input[name="name"]');
    const lists = [...document.querySelectorAll("[data-name-source]")];
    const chosen = new Set(
      [...document.querySelectorAll('input[type="radio"][name^="template-"]:checked')].map((radio) => radio.value),
    );
    const namesOf = (sources) =>
      sources.flatMap((list) => [...list.querySelectorAll("li")].map((item) => item.textContent));
    let names = namesOf(lists.filter((list) => chosen.has(list.dataset.nameSource)));
    if (names.length === 0) names = namesOf(lists);
    if (!input || names.length === 0) return;
    input.value = names[Math.floor(Math.random() * names.length)];
  };

  // Rolls the visible choices in page order. A template comes before its tables, so rolling it
  // first reveals the tables it needs, and those are rolled in the same pass.
  const randomizeChoices = (onlyUnchosen) => {
    const rolled = new Set();
    for (;;) {
      const next = [...document.querySelectorAll("[data-dice-for]")].find((dice) => {
        if (rolled.has(dice) || dice.closest("[hidden]")) return false;
        const name = dice.dataset.diceFor;
        return !onlyUnchosen || document.querySelector(`input[type="radio"][name="${name}"]:checked`) === null;
      });
      if (!next) return;
      rolled.add(next);
      rollChoice(next.dataset.diceFor);
    }
  };

  // The name is rolled last, so it draws on the background that the choices just rolled. In the
  // unchosen mode a name already typed counts as chosen and is kept.
  const randomizeAll = (onlyUnchosen) => {
    randomizeChoices(onlyUnchosen);
    const input = form.querySelector('input[name="name"]');
    if (onlyUnchosen && input?.value.trim()) return;
    rollName();
  };

  document.addEventListener("click", (event) => {
    const target = event.target;

    // A click on the dialog element is either its backdrop or its padding. Only a click outside the box closes it.
    if (target instanceof HTMLDialogElement && target.classList.contains("choice-dialog")) {
      const rect = target.getBoundingClientRect();
      const outside =
        event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) target.close();
      return;
    }

    const opener = target.closest("[data-open-choice]");
    if (opener) {
      openDialog(opener.dataset.openChoice);
      return;
    }

    if (target.closest("[data-close-choice]")) {
      target.closest("dialog")?.close();
      return;
    }

    // Only pointer picks (detail > 0) close the dialog, so arrow-key moves between options do not.
    const dialog = target.closest(".choice")?.closest("dialog");
    if (dialog && event.detail > 0) {
      dialog.close();
      return;
    }

    const dice = target.closest("[data-dice-for]");
    if (dice) {
      rollChoice(dice.dataset.diceFor);
      return;
    }

    if (target.closest("[data-name-dice]")) {
      rollName();
      return;
    }

    const randomize = target.closest("[data-randomize]");
    if (randomize) {
      randomizeAll(randomize.dataset.randomize === "unchosen");
    }
  });

  // Radios inside a closed dialog are not rendered, so the browser cannot report a missing
  // choice itself. This check covers the same ground and points at the first gap.
  form?.addEventListener("submit", (event) => {
    const error = form.querySelector(".form-error");
    const missing = [...form.querySelectorAll("[data-choice-panel]")].filter(
      (panel) => !panel.closest("[hidden]") && panel.querySelector('input[type="radio"]:checked') === null,
    );
    for (const panel of form.querySelectorAll("[data-choice-panel].is-missing")) {
      if (!missing.includes(panel)) panel.classList.remove("is-missing");
    }
    if (missing.length === 0) {
      if (error) error.hidden = true;
      return;
    }
    event.preventDefault();
    for (const panel of missing) panel.classList.add("is-missing");
    if (error) {
      const labels = missing.map((panel) => panel.dataset.choiceLabel).join(", ");
      error.textContent = `Choose an option for: ${labels}.`;
      error.hidden = false;
    }
    const first = missing[0];
    first.querySelector("[data-open-choice]")?.focus();
    first.scrollIntoView({ block: "center" });
  });
})();
