(() => {
  const ruleSetSelect = document.querySelector('select[name="rule_set"]');
  const worldSelect = document.querySelector('select[name="world_template"]');
  if (!ruleSetSelect || !worldSelect) return;

  const worldOptions = [...worldSelect.options];
  const recommendedGroup = worldSelect.querySelector('[data-world-group="recommended"]');
  const otherGroup = worldSelect.querySelector('[data-world-group="other"]');
  const labelInput = document.querySelector('input[name="label"]');
  const defaultLabels = new Set(
    worldOptions.map((option) => option.dataset.label).filter(Boolean),
  );

  const chosenRuleSet = () => ruleSetSelect.selectedOptions[0];

  // Third-party sources link to their credits beside the picker.
  const showCredits = (select, name) => {
    const credits = document.querySelector(`[data-blurb-credits="${name}"]`);
    if (credits) credits.hidden = select.selectedOptions[0]?.dataset.thirdParty !== "1";
  };

  // The label follows the world until the player writes their own.
  const refreshLabel = () => {
    if (!labelInput) return;
    const label = worldSelect.selectedOptions[0]?.dataset.label ?? "";
    if (!label) return;
    if (!labelInput.value || defaultLabels.has(labelInput.value)) labelInput.value = label;
  };

  // A world is recommended for the chosen rule set when it is built for it or names it in
  // `recommended-for`. The server renders the first split; changing the rule set regroups the
  // same options here.
  const recommended = (option) => {
    if (option.dataset.ruleSet === ruleSetSelect.value) return true;
    const declared = JSON.parse(option.dataset.recommendedFor || "[]");
    return declared.includes(ruleSetSelect.value);
  };

  // The rule set is chosen first. It offers the worlds built for it, plus the worlds that are
  // not tied to a rule set, with the recommended worlds in their own group above the rest.
  const refreshWorlds = () => {
    const name = chosenRuleSet()?.dataset.title || chosenRuleSet()?.textContent || "";
    if (recommendedGroup) recommendedGroup.label = `Recommended for ${name}`;
    if (otherGroup) otherGroup.label = "Other worlds";
    for (const option of worldOptions) {
      const required = option.dataset.ruleSet ?? "";
      const offered = required === "" || required === ruleSetSelect.value;
      option.hidden = !offered;
      option.disabled = !offered;
      (offered && recommended(option) ? recommendedGroup : otherGroup)?.append(option);
    }
    if (worldSelect.selectedOptions[0]?.disabled) {
      const offered = worldOptions.filter((option) => !option.disabled);
      const first = offered.find((option) => recommended(option)) ?? offered[0];
      if (first) worldSelect.value = first.value;
    }
    window.cygnusPicks?.rebuild();
    showCredits(worldSelect, "world_template");
    refreshLabel();
  };

  ruleSetSelect.addEventListener("change", () => {
    refreshWorlds();
    showCredits(ruleSetSelect, "rule_set");
  });
  worldSelect.addEventListener("change", () => {
    showCredits(worldSelect, "world_template");
    refreshLabel();
  });
  refreshWorlds();
  showCredits(ruleSetSelect, "rule_set");
})();
