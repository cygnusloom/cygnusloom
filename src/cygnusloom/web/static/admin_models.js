(() => {
  const number = (value) => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  };

  const costFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });

  const updateCalculator = (dialog) => {
    const field = (name) => {
      const input = dialog.querySelector(`[data-calc="${name}"]`);
      return input ? number(input.value) : 0;
    };
    const peakShare = Math.min(100, field("peak-share")) / 100;
    const hit = field("hit-tokens");
    const miss = field("miss-tokens");
    const output = field("output-tokens");

    const peak =
      (hit * field("hit-peak") +
        miss * field("miss-peak") +
        output * field("output-peak")) /
      1_000_000;
    const offPeak =
      (hit * field("hit-off-peak") +
        miss * field("miss-off-peak") +
        output * field("output-off-peak")) /
      1_000_000;
    const blended = peak * peakShare + offPeak * (1 - peakShare);

    const result = (name, value) => {
      const outputElement = dialog.querySelector(`[data-calc-result="${name}"]`);
      if (outputElement) outputElement.textContent = costFormat.format(value);
    };
    result("peak", peak);
    result("off-peak", offPeak);
    result("blended", blended);
    result("thousand", blended * 1000);
  };

  document.querySelectorAll("[data-calculator]").forEach((dialog) => {
    dialog.addEventListener("input", () => updateCalculator(dialog));
    updateCalculator(dialog);
  });

  document.querySelectorAll("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.dialogOpen);
      if (dialog) dialog.showModal();
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });

  document.querySelectorAll("dialog.app-dialog").forEach((dialog) => {
    // Clicking the backdrop closes the dialog, matching the native Escape behavior.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
  });

  const filter = document.querySelector("[data-usage-filter]");
  if (filter) {
    const period = filter.querySelector("[data-usage-period]");
    const start = filter.querySelector("[data-usage-start]");
    const end = filter.querySelector("[data-usage-end]");
    const sync = () => {
      if (!period) return;
      if (start) start.disabled = period.value === "all";
      if (end) end.disabled = period.value !== "between";
    };
    period?.addEventListener("change", sync);
    sync();
  }
})();
