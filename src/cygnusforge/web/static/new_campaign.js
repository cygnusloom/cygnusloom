(() => {
  const ruleSetSelect = document.querySelector('select[name="rule_set"]');
  const worldSelect = document.querySelector('select[name="world_template"]');
  if (!ruleSetSelect || !worldSelect) return;

  const worldOptions = [...worldSelect.options];

  const showBlurb = (select, name) => {
    const output = document.querySelector(`[data-blurb-for="${name}"]`);
    if (output) output.textContent = select.selectedOptions[0]?.dataset.blurb ?? "";
  };

  // The rule set is chosen first. It offers the worlds built for it, plus the worlds that are
  // not tied to a rule set.
  const refreshWorlds = () => {
    for (const option of worldOptions) {
      const required = option.dataset.ruleSet ?? "";
      const offered = required === "" || required === ruleSetSelect.value;
      option.hidden = !offered;
      option.disabled = !offered;
    }
    if (worldSelect.selectedOptions[0]?.disabled) {
      const first = worldOptions.find((option) => !option.disabled);
      if (first) worldSelect.value = first.value;
    }
    showBlurb(worldSelect, "world_template");
  };

  ruleSetSelect.addEventListener("change", () => {
    refreshWorlds();
    showBlurb(ruleSetSelect, "rule_set");
  });
  worldSelect.addEventListener("change", () => showBlurb(worldSelect, "world_template"));
  refreshWorlds();
  showBlurb(ruleSetSelect, "rule_set");
})();
