(() => {
  // A level-up pool such as skill points is shared by skills and attribute boosts. The server
  // refuses to overspend it; this script keeps the page from offering a choice the server would
  // refuse, and shows how much of the pool is left.
  const optionCost = (option) => Number(option?.dataset.cost) || 0;
  const selectCost = (select) => optionCost(select.selectedOptions[0]);

  const stepsOf = (fieldset) => {
    try {
      return JSON.parse(fieldset.dataset.boostSteps || "[]");
    } catch {
      return [];
    }
  };

  const skillSelects = (pool) =>
    pool.skills.flatMap((fieldset) => [...fieldset.querySelectorAll("select")]);

  // The server spends the remaining attribute-boost steps in stat order, so the page does too.
  const statPlan = (pool) => {
    const plans = [];
    let total = 0;
    for (const fieldset of pool.stats) {
      const steps = stepsOf(fieldset);
      const level = Number(fieldset.dataset.boostLevel) || 0;
      let offset = 0;
      for (const input of fieldset.querySelectorAll("input[data-stat-boost]")) {
        const count = Math.max(0, Number(input.value) || 0);
        let cost = 0;
        for (let index = 0; index < count; index++) {
          const step = steps[offset + index];
          if (!step || (step.minimum_level && step.minimum_level > level)) {
            cost = Infinity;
            break;
          }
          cost += Number(step.cost) || 0;
        }
        plans.push({ input, fieldset, count, offset, cost });
        total += cost;
        offset += count;
      }
    }
    return { plans, total };
  };

  const maxCount = (plan, budget) => {
    if (!Number.isFinite(budget) || budget < 0) return 0;
    const steps = stepsOf(plan.fieldset);
    const level = Number(plan.fieldset.dataset.boostLevel) || 0;
    let cost = 0;
    let count = 0;
    for (;;) {
      const step = steps[plan.offset + count];
      if (!step || (step.minimum_level && step.minimum_level > level)) break;
      const next = cost + (Number(step.cost) || 0);
      if (next > budget) break;
      cost = next;
      count += 1;
    }
    return count;
  };

  const disableInvalidSkills = (pool) => {
    let changed = false;
    for (let guard = 0; guard < 20; guard++) {
      const plan = statPlan(pool);
      const selects = skillSelects(pool);
      const total = selects.reduce((sum, select) => sum + selectCost(select), 0);
      let cleared = false;
      for (const select of selects) {
        for (const option of select.options) {
          if (!option.value) continue;
          const without = total - selectCost(select);
          option.disabled = without + optionCost(option) + plan.total > pool.available;
        }
        if (select.selectedOptions[0]?.disabled) {
          select.value = "";
          cleared = true;
          changed = true;
          break;
        }
      }
      if (!cleared) break;
    }
    return changed;
  };

  const clampStats = (pool) => {
    let changed = false;
    for (let guard = 0; guard < 100; guard++) {
      const plan = statPlan(pool);
      const skillTotal = skillSelects(pool).reduce((sum, select) => sum + selectCost(select), 0);
      if (plan.total <= pool.available - skillTotal) break;
      const chosen = plan.plans.filter((entry) => entry.count > 0);
      if (chosen.length === 0) break;
      const last = chosen[chosen.length - 1];
      last.input.value = String(Math.max(0, last.count - 1));
      changed = true;
    }
    return changed;
  };

  const poolSpent = (pool) => {
    const skillTotal = skillSelects(pool).reduce((sum, select) => sum + selectCost(select), 0);
    const plan = statPlan(pool);
    return skillTotal + (Number.isFinite(plan.total) ? plan.total : Infinity);
  };

  const updateBudget = (form, pools) => {
    const budget = form.querySelector("[data-pick-budget]");
    if (!budget) return;
    const lines = [];
    for (const pool of pools.values()) {
      const remaining = Math.max(0, pool.available - poolSpent(pool));
      const points = pool.available === 1 ? "point" : "points";
      lines.push(`${pool.label}: ${remaining} of ${pool.available} ${points} left.`);
    }
    budget.textContent = lines.join(" ");
    budget.hidden = lines.length === 0;
  };

  const refresh = (form, pools) => {
    for (const pool of pools.values()) {
      for (let pass = 0; pass < 3; pass++) {
        const statsChanged = clampStats(pool);
        const skillsChanged = disableInvalidSkills(pool);
        if (!statsChanged && !skillsChanged) break;
      }
      disableInvalidSkills(pool);

      // Once the current choices are settled, each boost can only grow until the pool runs out.
      const plan = statPlan(pool);
      const skillTotal = skillSelects(pool).reduce((sum, select) => sum + selectCost(select), 0);
      for (const entry of plan.plans) {
        const others = Number.isFinite(entry.cost) ? plan.total - entry.cost : 0;
        const allowed = maxCount(entry, pool.available - skillTotal - others);
        entry.input.max = String(allowed);
        if (entry.count > allowed) entry.input.value = String(allowed);
      }
    }
    updateBudget(form, pools);
  };

  for (const form of document.querySelectorAll("form.advancement-form")) {
    const pools = new Map();
    for (const fieldset of form.querySelectorAll("[data-pick-kind][data-pick-counter]")) {
      const kind = fieldset.dataset.pickKind;
      if (kind !== "skill" && kind !== "stat") continue;
      const counter = fieldset.dataset.pickCounter;
      let pool = pools.get(counter);
      if (!pool) {
        pool = {
          available: Number(fieldset.dataset.pickAvailable) || 0,
          label: fieldset.dataset.pickLabel || "Points",
          skills: [],
          stats: [],
        };
        pools.set(counter, pool);
      }
      pool.available = Math.min(pool.available, Number(fieldset.dataset.pickAvailable) || 0);
      (kind === "skill" ? pool.skills : pool.stats).push(fieldset);
    }

    if (pools.size > 0) {
      refresh(form, pools);
      form.addEventListener("change", () => refresh(form, pools));
      form.addEventListener("input", (event) => {
        if (event.target.matches("[data-stat-boost]")) refresh(form, pools);
      });
      form.addEventListener("submit", (event) => {
        const error = form.querySelector("[data-budget-error]");
        for (const pool of pools.values()) {
          if (poolSpent(pool) > pool.available) {
            event.preventDefault();
            if (error) {
              error.textContent = `${pool.label} cost more than the ${pool.available} available. Lower a choice and try again.`;
              error.hidden = false;
            }
            return;
          }
        }
        if (error) {
          error.hidden = true;
          error.textContent = "";
        }
      });
    }

    // An Advancement that teaches a feature or raises a stat needs that choice before it can be
    // submitted. The server checks the same thing, but the page can point at the missing field.
    const optionError = form.querySelector("[data-option-error]");
    if (optionError) {
      form.addEventListener("change", () => {
        optionError.hidden = true;
        optionError.textContent = "";
      });
      form.addEventListener("submit", (event) => {
        const chosen = form.querySelector('input[name="option"]:checked');
        if (!chosen) return;
        const label = chosen.closest(".advancement-option")?.querySelector("strong")?.textContent.trim();
        const feature = form.querySelector(`select[name="feature-${chosen.value}"]`);
        const stat = form.querySelector(`select[name="stat-${chosen.value}"]`);
        let message = "";
        if (feature && !feature.value) {
          message = `Choose a feature for ${label || "this option"}.`;
        } else if (stat && !stat.value) {
          message = `Choose an attribute for ${label || "this option"}.`;
        }
        if (!message) return;
        event.preventDefault();
        optionError.textContent = message;
        optionError.hidden = false;
        (feature || stat).closest("[data-pick-control]")?.querySelector("[data-open-pick]")?.focus();
      });
    }
  }
})();
