(() => {
  const form = document.querySelector(".creation-form");

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

  // A template that names classes is offered only to those classes. A kind with nothing left to
  // offer is hidden, so a Warrior never sees a magical tradition at all.
  const refreshTemplateChoices = () => {
    const cls = document.querySelector('input[name="template-class"]:checked');
    const chosenClass = cls ? cls.value : "";
    for (const panel of document.querySelectorAll('[data-choice-panel^="template-"]')) {
      const kind = panel.dataset.choicePanel.slice("template-".length);
      if (kind === "class") continue;
      let offered = false;
      for (const label of panel.querySelectorAll("label.choice")) {
        const radio = label.querySelector('input[type="radio"]');
        if (!radio) continue;
        const classes = JSON.parse(label.dataset.classes ?? "[]");
        const allowed = classes.length === 0 || classes.includes(chosenClass);
        label.hidden = !allowed;
        if (!allowed && radio.checked) {
          radio.checked = false;
          refreshSummary(radio.name);
          revealTables(kind);
        }
        if (allowed) offered = true;
      }
      panel.hidden = !offered;
    }
  };

  // Firefox can restore radio state on reload, so summaries are rebuilt from the DOM at load.
  for (const output of document.querySelectorAll("[data-summary-for]")) {
    refreshSummary(output.dataset.summaryFor);
  }
  const kinds = new Set(
    [...document.querySelectorAll("[data-template-select]")].map((radio) => radio.dataset.templateSelect),
  );
  for (const kind of kinds) revealTables(kind);
  refreshTemplateChoices();

  // A spell pick offers the spells of the chosen Year only: spells from earlier Years are known,
  // not picked. With no Year chosen it offers none. A pick whose spell is no longer offered goes
  // back to Roll.
  const refreshSpellPicks = () => {
    for (const fieldset of document.querySelectorAll("[data-tag-field]")) {
      const year = document.querySelector(`input[type="radio"][name="${fieldset.dataset.tagField}"]:checked`);
      for (const group of fieldset.querySelectorAll("optgroup")) {
        const offered = year !== null && group.dataset.tag === year.value;
        group.hidden = !offered;
        for (const option of group.children) option.disabled = !offered;
      }
      for (const select of fieldset.querySelectorAll("select")) {
        if (select.selectedOptions[0]?.disabled) select.value = "";
      }
    }
  };
  // A restricted choice offers only its options in the allowed groups while its gate is unchosen
  // or takes one of the gate's values. An option it rules out is disabled, and a pick of it is
  // cleared.
  const refreshRestrictions = () => {
    for (const panel of document.querySelectorAll("[data-restrict-field]")) {
      const gate = document.querySelector(`input[type="radio"][name="${panel.dataset.restrictField}"]:checked`);
      const values = JSON.parse(panel.dataset.restrictValues);
      const groups = JSON.parse(panel.dataset.restrictGroups);
      const active = gate === null || values.includes(gate.value);
      for (const radio of panel.querySelectorAll('input[type="radio"]')) {
        radio.disabled = active && !groups.includes(radio.dataset.group);
        if (radio.disabled && radio.checked) {
          radio.checked = false;
          refreshSummary(panel.dataset.choicePanel);
        }
      }
    }
  };
  refreshRestrictions();
  refreshSpellPicks();

  document.addEventListener("change", (event) => {
    const radio = event.target;
    if (!(radio instanceof HTMLInputElement) || radio.type !== "radio") return;
    refreshSummary(radio.name);
    refreshTemplateChoices();
    refreshRestrictions();
    refreshSpellPicks();
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
      (radio) => !radio.closest("[hidden]") && !radio.disabled && !radio.hasAttribute("data-freeform"),
    );
    if (options.length === 0) return;
    const pick = options[Math.floor(Math.random() * options.length)];
    pick.checked = true;
    pick.dispatchEvent(new Event("change", { bubbles: true }));
  };

  // Suggests a name from the chosen templates' names, or from every template's names when no
  // chosen template suggests any. The name typed by the player is replaced.
  const rollName = () => {
    const input = form?.querySelector('input[name="name"]');
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

  // Rolls the visible choices. A template comes before its tables, so rolling it first reveals the
  // tables it needs, and those are rolled in the same pass. A restricted choice is rolled after the
  // unrestricted ones, so its roll uses its gate's final value.
  const randomizeChoices = (onlyUnchosen) => {
    const rolled = new Set();
    for (;;) {
      const pending = [...document.querySelectorAll("[data-dice-for]")].filter((dice) => {
        if (rolled.has(dice) || dice.closest("[hidden]")) return false;
        const panel = dice.closest("[data-choice-panel]");
        if (panel?.dataset.optional === "true") return false;
        const name = dice.dataset.diceFor;
        return !onlyUnchosen || document.querySelector(`input[type="radio"][name="${name}"]:checked`) === null;
      });
      const next = pending.find((dice) => !dice.closest("[data-restrict-field]")) ?? pending[0];
      if (!next) return;
      rolled.add(next);
      rollChoice(next.dataset.diceFor);
    }
  };

  const shuffle = (items) => {
    for (let i = items.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [items[i], items[j]] = [items[j], items[i]];
    }
    return items;
  };

  // Each swap group is rerolled within the rules the server checks. A group assigned from a pool
  // takes its values in any order. A rolled group keeps its roll, or swaps one pair of its values.
  const randomizeStats = () => {
    const groups = new Map();
    for (const select of document.querySelectorAll("select[data-stat-group]")) {
      const members = groups.get(select.dataset.statGroup) ?? [];
      members.push(select);
      groups.set(select.dataset.statGroup, members);
    }
    for (const members of groups.values()) {
      const rolled = members.map((select) => select.dataset.statRolled);
      let values;
      if (members[0].hasAttribute("data-stat-assigned")) {
        values = shuffle([...rolled]);
      } else {
        const pairs = [];
        for (let i = 0; i < rolled.length; i++) {
          for (let j = i + 1; j < rolled.length; j++) pairs.push([i, j]);
        }
        values = [...rolled];
        const pick = Math.floor(Math.random() * (pairs.length + 1));
        if (pick < pairs.length) {
          const [i, j] = pairs[pick];
          [values[i], values[j]] = [values[j], values[i]];
        }
      }
      members.forEach((select, index) => {
        select.value = values[index];
      });
    }
  };

  // Each spell pick takes a spell it offers, and no two picks of a template take the same spell.
  // In the unchosen mode a pick the player already set is kept, and its spell is not taken again.
  const randomizeSpells = (fieldset, onlyUnchosen) => {
    const selects = [...fieldset.querySelectorAll("select")];
    const taken = new Set(onlyUnchosen ? selects.map((select) => select.value).filter(Boolean) : []);
    for (const select of selects) {
      if (onlyUnchosen && select.value !== "") continue;
      const offered = [...select.options]
        .filter((option) => option.value !== "" && !option.disabled && !taken.has(option.value))
        .map((option) => option.value);
      select.value = offered.length > 0 ? offered[Math.floor(Math.random() * offered.length)] : "";
      if (select.value !== "") taken.add(select.value);
    }
  };

  // Each skill or feature pick takes a value it offers. Feature picks of one pick do not repeat a
  // feature; skill picks may repeat when the pool is smaller than the number of picks. In the
  // unchosen mode a pick the player already set is kept, and its value is not taken again.
  const randomizePicks = (fieldset, onlyUnchosen) => {
    const selects = [...fieldset.querySelectorAll("select")];
    const taken = new Set(onlyUnchosen ? selects.map((select) => select.value).filter(Boolean) : []);
    for (const select of selects) {
      if (onlyUnchosen && select.value !== "") continue;
      const offered = [...select.options]
        .filter((option) => option.value !== "" && !option.disabled)
        .map((option) => option.value);
      const fresh = offered.filter((value) => !taken.has(value));
      const pool = fresh.length > 0 ? fresh : offered;
      select.value = pool.length > 0 ? pool[Math.floor(Math.random() * pool.length)] : "";
      if (select.value !== "") taken.add(select.value);
    }
  };

  // The name is rolled last, so it draws on the background that the choices just rolled. The
  // Year is rolled with the choices, so the spells are rolled after it, from the Year's spells. In
  // the unchosen mode a name already typed counts as chosen and is kept, and the stats are always
  // chosen (they are rolled), so they are left alone too.
  const randomizeAll = (onlyUnchosen) => {
    randomizeChoices(onlyUnchosen);
    if (!onlyUnchosen) randomizeStats();
    for (const fieldset of document.querySelectorAll("[data-spell-picks]")) {
      if (!fieldset.closest("[hidden]")) randomizeSpells(fieldset, onlyUnchosen);
    }
    for (const fieldset of document.querySelectorAll("[data-pick-selects]")) {
      if (!fieldset.closest("[hidden]")) randomizePicks(fieldset, onlyUnchosen);
    }
    const input = form?.querySelector('input[name="name"]');
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

    if (target.closest("[data-randomize-stats]")) {
      randomizeStats();
      return;
    }

    const spellPicks = target.closest("[data-randomize-spells]")?.closest("[data-spell-picks]");
    if (spellPicks) {
      randomizeSpells(spellPicks, false);
      return;
    }

    const picks = target.closest("[data-randomize-picks]")?.closest("[data-pick-selects]");
    if (picks) {
      randomizePicks(picks, false);
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
