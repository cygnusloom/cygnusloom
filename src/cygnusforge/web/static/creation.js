(() => {
  const form = document.querySelector(".creation-form");
  const templateRadios = [...document.querySelectorAll("input[type=radio][data-template-select]")];
  const templateChoicesByKind = new Map();
  for (const radio of templateRadios) {
    const choices = templateChoicesByKind.get(radio.dataset.templateSelect) ?? [];
    choices.push(radio);
    templateChoicesByKind.set(radio.dataset.templateSelect, choices);
  }

  // Only the tables of the chosen template are shown. Hidden tables are skipped on submit.
  const revealTables = (kind) => {
    const chosen = templateRadios.find(
      (radio) => radio.checked && radio.dataset.templateSelect === kind,
    );
    for (const group of document.querySelectorAll("[data-template-group]")) {
      if (group.dataset.templateGroup === kind) {
        group.hidden = !chosen || group.dataset.template !== chosen.value;
      }
    }
  };

  const refreshSummary = (name) => {
    const output = [...document.querySelectorAll("[data-summary-for]")].find(
      (item) => item.dataset.summaryFor === name,
    );
    if (!output) return;
    const chosen = [...document.querySelectorAll('input[type="radio"]')].find(
      (radio) => radio.name === name && radio.checked,
    );
    output.textContent = chosen ? chosen.dataset.title : "Not chosen";
    output.classList.toggle("is-chosen", Boolean(chosen));
  };

  const selectedTemplates = () => {
    const selected = new Map();
    for (const radio of templateRadios) {
      if (radio.checked) selected.set(radio.dataset.templateSelect, radio);
    }
    return selected;
  };

  const templateData = (radio, name, fallback) => {
    const value = radio.closest("label.choice")?.dataset[name];
    return value ? JSON.parse(value) : fallback;
  };

  const compatibilityGraph = new Map(
    [...templateChoicesByKind.keys()].map((kind) => [kind, new Set()]),
  );
  for (const radio of templateRadios) {
    const gates = compatibilityGraph.get(radio.dataset.templateSelect);
    for (const kind of Object.keys(templateData(radio, "compatibleWith", {}))) {
      if (compatibilityGraph.has(kind)) gates.add(kind);
    }
  }
  const reachableKinds = (start) => {
    const reached = new Set([start]);
    const pending = [start];
    while (pending.length > 0) {
      for (const kind of compatibilityGraph.get(pending.pop()) ?? []) {
        if (reached.has(kind)) continue;
        reached.add(kind);
        pending.push(kind);
      }
    }
    return reached;
  };
  const compatibilityReachability = new Map(
    [...compatibilityGraph.keys()].map((kind) => [kind, reachableKinds(kind)]),
  );
  const compatibilityComponent = (start) =>
    new Set(
      [...compatibilityGraph.keys()].filter(
        (kind) =>
          compatibilityReachability.get(start)?.has(kind) &&
          compatibilityReachability.get(kind)?.has(start),
      ),
    );

  const activeRequirements = (selected) =>
    [...selected.values()].flatMap((radio) =>
      templateData(radio, "templateRequirements", []),
    );

  const choiceKey = (radio) => radio.dataset.choiceKey || radio.value;

  const hasFeasibleCompletion = (initial, blockedKinds = new Set()) => {
    const memo = new Map();
    const search = (selected) => {
      const key = JSON.stringify(
        [...selected]
          .map(([kind, radio]) => [kind, radio.value])
          .sort(([left], [right]) => left.localeCompare(right)),
      );
      if (memo.has(key)) return memo.get(key);

      let pending = null;
      for (const radio of selected.values()) {
        const restrictions = templateData(radio, "compatibleWith", {});
        for (const [kind, allowed] of Object.entries(restrictions)) {
          const gate = selected.get(kind);
          if (gate) {
            if (!allowed.includes(gate.value)) {
              memo.set(key, false);
              return false;
            }
            continue;
          }
          if (blockedKinds.has(kind)) {
            memo.set(key, false);
            return false;
          }
          pending = {
            kind,
            choices: (templateChoicesByKind.get(kind) ?? []).filter((choice) =>
              allowed.includes(choice.value),
            ),
          };
          break;
        }
        if (pending) break;

        for (const requirement of templateData(radio, "templateRequirements", [])) {
          const chosen = requirement.kinds
            .map((kind) => selected.get(kind))
            .filter((choice) => choice !== undefined);
          const keys = chosen.map(choiceKey);
          if (requirement.distinct && new Set(keys).size !== keys.length) {
            memo.set(key, false);
            return false;
          }
          const kind = requirement.kinds.find((requiredKind) => !selected.has(requiredKind));
          if (!kind) continue;
          if (blockedKinds.has(kind)) {
            memo.set(key, false);
            return false;
          }
          const duplicateKeys = requirement.distinct ? new Set(keys) : null;
          pending = {
            kind,
            choices: (templateChoicesByKind.get(kind) ?? []).filter(
              (choice) => !duplicateKeys || !duplicateKeys.has(choiceKey(choice)),
            ),
          };
          break;
        }
        if (pending) break;
      }

      if (!pending) {
        memo.set(key, true);
        return true;
      }
      for (const choice of pending.choices) {
        const next = new Map(selected);
        next.set(pending.kind, choice);
        if (search(next)) {
          memo.set(key, true);
          return true;
        }
      }
      memo.set(key, false);
      return false;
    };
    return search(new Map(initial));
  };

  const compatible = (radio, selected) => {
    const assignment = new Map(selected);
    const kind = radio.dataset.templateSelect;
    for (const cycleKind of compatibilityComponent(kind)) assignment.delete(cycleKind);
    assignment.set(kind, radio);
    return hasFeasibleCompletion(assignment);
  };

  const clearTemplateChoice = (radio) => {
    radio.checked = false;
    refreshSummary(radio.name);
    if (radio.dataset.templateSelect) revealTables(radio.dataset.templateSelect);
  };

  const refreshTemplateChoices = (changedRadio = null) => {
    for (let pass = 0; pass <= templateRadios.length; pass++) {
      let cleared = false;
      const selectedAtStart = selectedTemplates();
      const changedKind = changedRadio?.dataset.templateSelect;
      const ordered =
        changedKind && selectedAtStart.get(changedKind) === changedRadio
          ? [
              changedRadio,
              ...[...selectedAtStart.values()].filter((radio) => radio !== changedRadio),
            ]
          : [...selectedAtStart.values()];
      const blockedKinds = new Set();
      if (changedRadio && !changedKind && changedRadio.value === "") {
        const panel = changedRadio.closest('[data-choice-panel^="template-"]');
        if (panel) {
          blockedKinds.add(panel.dataset.choicePanel.slice("template-".length));
        }
      }
      const feasible = new Map();
      for (const radio of ordered) {
        const assignment = new Map(feasible);
        assignment.set(radio.dataset.templateSelect, radio);
        if (hasFeasibleCompletion(assignment, blockedKinds)) {
          feasible.set(radio.dataset.templateSelect, radio);
        } else {
          clearTemplateChoice(radio);
          cleared = true;
        }
      }

      let selected = selectedTemplates();
      for (const requirement of activeRequirements(selected)) {
        if (!requirement.distinct) continue;
        const byKey = new Map();
        for (const kind of requirement.kinds) {
          const radio = selected.get(kind);
          if (!radio) continue;
          const key = choiceKey(radio);
          const matches = byKey.get(key) ?? [];
          matches.push(radio);
          byKey.set(key, matches);
        }
        for (const matches of byKey.values()) {
          if (matches.length < 2) continue;
          const keep = matches.includes(changedRadio) ? changedRadio : matches[0];
          for (const radio of matches) {
            if (radio === keep) continue;
            clearTemplateChoice(radio);
            cleared = true;
          }
        }
        if (cleared) selected = selectedTemplates();
      }

      if (!cleared) break;
    }

    const selected = selectedTemplates();
    const requirements = activeRequirements(selected);
    const requiredKinds = new Set(requirements.flatMap((requirement) => requirement.kinds));
    for (const radio of selected.values()) {
      for (const kind of Object.keys(templateData(radio, "compatibleWith", {}))) {
        requiredKinds.add(kind);
      }
    }
    const panels = [...document.querySelectorAll('[data-choice-panel^="template-"]')];

    for (const panel of panels) {
      const kind = panel.dataset.choicePanel.slice("template-".length);
      const required = requiredKinds.has(kind);
      panel.dataset.required = String(required);

      const none = panel.querySelector('input[type="radio"][value=""]');
      if (none) {
        none.closest("label.choice").hidden = required;
        if (required && none.checked) clearTemplateChoice(none);
      }

      let offered = Boolean(none && !required);
      for (const radio of templateRadios) {
        if (radio.dataset.templateSelect !== kind) continue;
        const duplicate = requirements.some(
          (requirement) =>
            requirement.distinct &&
            requirement.kinds.includes(kind) &&
            requirement.kinds.some((otherKind) => {
              const other = selected.get(otherKind);
              return otherKind !== kind && other && choiceKey(other) === choiceKey(radio);
            }),
        );
        const allowed = compatible(radio, selected) && !duplicate;
        radio.closest("label.choice").hidden = !allowed;
        if (!allowed && radio.checked) clearTemplateChoice(radio);
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

  // Tagged feature picks offer only the group matching the selected field.
  const refreshTaggedPicks = () => {
    for (const fieldset of document.querySelectorAll("[data-tag-field]")) {
      const selectedTag = document.querySelector(`input[type="radio"][name="${fieldset.dataset.tagField}"]:checked`);
      for (const group of fieldset.querySelectorAll("optgroup")) {
        const offered = selectedTag !== null && group.dataset.tag === selectedTag.value;
        group.hidden = !offered;
        for (const option of group.children) option.disabled = !offered;
      }
      for (const select of fieldset.querySelectorAll("select")) {
        if (select.selectedOptions[0]?.disabled) select.value = "";
      }
    }
  };
  const refreshSkillPickOptions = () => {
    if (!form?.hasAttribute("data-skill-first-level")) return;
    const firstLevel = Number(form.dataset.skillFirstLevel);
    const creationMaximum = Number(form.dataset.skillCreationMaximum);
    if (!Number.isFinite(firstLevel) || !Number.isFinite(creationMaximum)) return;

    const increments = new Map();
    for (const radio of selectedTemplates().values()) {
      const tables = [...document.querySelectorAll("[data-template-group]")].find(
        (group) =>
          group.dataset.templateGroup === radio.dataset.templateSelect &&
          group.dataset.template === radio.value,
      );
      if (!tables) continue;
      const grants = JSON.parse(tables.dataset.skillGrants || "{}");
      for (const [skill, amount] of Object.entries(grants)) {
        increments.set(skill, (increments.get(skill) ?? 0) + Number(amount));
      }
    }

    for (const fieldset of document.querySelectorAll('[data-pick-kind="skill"]')) {
      if (fieldset.closest("[hidden]")) {
        for (const select of fieldset.querySelectorAll("select")) select.value = "";
        continue;
      }
      const maximum = Number(fieldset.dataset.pickMaxLevel);
      for (const select of fieldset.querySelectorAll("select")) {
        for (const option of select.options) {
          if (!option.value) {
            option.disabled = false;
            continue;
          }
          const count = increments.get(option.value) ?? 0;
          const level = Math.min(creationMaximum, firstLevel + count - 1);
          option.disabled = count > 0 && level >= maximum;
        }
        if (select.selectedOptions[0]?.disabled) select.value = "";
        if (select.value) {
          increments.set(select.value, (increments.get(select.value) ?? 0) + 1);
        }
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
  refreshTaggedPicks();
  refreshSkillPickOptions();

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (target instanceof HTMLSelectElement) {
      if (target.closest('[data-pick-kind="skill"]')) refreshSkillPickOptions();
      return;
    }
    const radio = target;
    if (!(radio instanceof HTMLInputElement) || radio.type !== "radio") return;
    refreshSummary(radio.name);
    refreshTemplateChoices(radio);
    refreshRestrictions();
    refreshTaggedPicks();
    if (radio.dataset.templateSelect) revealTables(radio.dataset.templateSelect);
    refreshSkillPickOptions();
    radio.closest("[data-choice-panel]")?.classList.remove("is-missing");
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
    const options = [...document.querySelectorAll('input[type="radio"]')].filter(
      (radio) =>
        radio.name === name &&
        !radio.closest("[hidden]") &&
        !radio.disabled &&
        !radio.hasAttribute("data-freeform"),
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

  const templateKindOrder = () => {
    const kinds = [...new Set(templateRadios.map((radio) => radio.dataset.templateSelect))];
    const dependencies = new Map(kinds.map((kind) => [kind, new Set()]));
    for (const radio of templateRadios) {
      const kind = radio.dataset.templateSelect;
      const dependents = dependencies.get(kind);
      for (const gate of Object.keys(templateData(radio, "compatibleWith", {}))) {
        if (gate !== kind && dependencies.has(gate)) dependents.add(gate);
      }
      for (const requirement of templateData(radio, "templateRequirements", [])) {
        for (const requiredKind of requirement.kinds) {
          if (requiredKind !== kind && dependencies.has(requiredKind)) {
            dependencies.get(requiredKind).add(kind);
          }
        }
      }
    }

    const remaining = new Set(kinds);
    const ordered = [];
    while (remaining.size > 0) {
      const ready = [...remaining].filter((kind) =>
        [...dependencies.get(kind)].every((gate) => !remaining.has(gate)),
      );
      const next = ready.length > 0 ? ready : [remaining.values().next().value];
      for (const kind of next) {
        ordered.push(kind);
        remaining.delete(kind);
      }
    }
    return new Map(ordered.map((kind, index) => [kind, index]));
  };

  // Rolls the visible choices. A template comes before its tables, so rolling it first reveals the
  // tables it needs, and dependent template kinds follow their gates. A restricted choice is rolled
  // after unrestricted ones, so its roll uses its gate's final value.
  const randomizeChoices = (onlyUnchosen) => {
    const rolled = new Set();
    const order = templateKindOrder();
    for (;;) {
      const pending = [...document.querySelectorAll("[data-dice-for]")].filter((dice) => {
        if (rolled.has(dice) || dice.closest("[hidden]")) return false;
        const panel = dice.closest("[data-choice-panel]");
        if (panel?.dataset.optional === "true" && panel.dataset.required !== "true") return false;
        const name = dice.dataset.diceFor;
        const chosen = [...document.querySelectorAll('input[type="radio"]')].some(
          (radio) => radio.name === name && radio.checked,
        );
        return !onlyUnchosen || !chosen;
      });
      pending.sort((left, right) => {
        const rank = (dice) => {
          const name = dice.dataset.diceFor;
          if (!name.startsWith("template-")) return -1;
          return order.get(name.slice("template-".length)) ?? 0;
        };
        return rank(left) - rank(right);
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

  // Do not select the same tagged feature twice.
  const randomizeTaggedPicks = (fieldset, onlyUnchosen) => {
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
    const skillPicks = fieldset.dataset.pickKind === "skill";
    if (skillPicks) {
      if (!onlyUnchosen) {
        for (const select of selects) select.value = "";
      }
      refreshSkillPickOptions();
    }
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
      if (skillPicks) refreshSkillPickOptions();
    }
  };

  // Tagged feature picks follow their fields; the name is rolled after all choices.
  const randomizeAll = (onlyUnchosen) => {
    randomizeChoices(onlyUnchosen);
    if (!onlyUnchosen) randomizeStats();
    for (const fieldset of document.querySelectorAll("[data-tagged-picks]")) {
      if (!fieldset.closest("[hidden]")) randomizeTaggedPicks(fieldset, onlyUnchosen);
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

    const taggedPicks = target.closest("[data-randomize-tagged-picks]")?.closest("[data-tagged-picks]");
    if (taggedPicks) {
      randomizeTaggedPicks(taggedPicks, false);
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
      (panel) =>
        !panel.closest("[hidden]") &&
        panel.querySelector('input[type="radio"]:checked') === null &&
        !(panel.dataset.optional === "true" && panel.dataset.required !== "true"),
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
      const missingKinds = new Set(
        missing
          .map((panel) => panel.dataset.choicePanel)
          .filter((name) => name.startsWith("template-"))
          .map((name) => name.slice("template-".length)),
      );
      const requirementMessage = activeRequirements(selectedTemplates()).find((requirement) =>
        requirement.kinds.some((kind) => missingKinds.has(kind)),
      )?.missing_message;
      if (requirementMessage) {
        error.textContent = requirementMessage;
      } else {
        const labels = missing.map((panel) => panel.dataset.choiceLabel).join(", ");
        error.textContent = `Choose an option for: ${labels}.`;
      }
      error.hidden = false;
    }
    const first = missing[0];
    first.querySelector("[data-open-choice]")?.focus();
    first.scrollIntoView({ block: "center" });
  });
})();
