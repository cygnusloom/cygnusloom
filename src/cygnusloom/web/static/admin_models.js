(() => {
  const number = (value) => {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  };

  const integer = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0;
  };

  const countFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const costFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 });
  const pending = new WeakMap();
  const pendingSaves = new WeakMap();
  const SAVED_FIELDS = [
    "hit-peak",
    "miss-peak",
    "output-peak",
    "hit-off-peak",
    "miss-off-peak",
    "output-off-peak",
    "peak-start",
    "peak-end",
  ];

  const field = (dialog, name) => {
    const input = dialog.querySelector(`[data-calc="${name}"]`);
    return input ? input.value.trim() : "";
  };

  const setText = (dialog, selector, value) => {
    dialog.querySelectorAll(selector).forEach((element) => {
      element.textContent = value;
    });
  };

  const setHidden = (dialog, selector, hidden) => {
    dialog.querySelectorAll(selector).forEach((element) => {
      element.hidden = hidden;
    });
  };

  const setResult = (dialog, name, value) => {
    const element = dialog.querySelector(`[data-calc-result="${name}"]`);
    if (element) element.textContent = value == null ? "—" : costFormat.format(value);
  };

  const clearResults = (dialog) => {
    for (const name of ["peak", "off-peak", "total", "per-reply", "thousand"]) {
      setResult(dialog, name, null);
    }
  };

  const saveStatus = (dialog, message, isError = false) => {
    const status = dialog.querySelector("[data-calc-save-status]");
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
    status.classList.toggle("form-error", isError);
  };

  const persistSettings = async (dialog) => {
    const url = dialog.dataset.saveUrl;
    const token = dialog.dataset.csrfToken;
    if (!url || !token) return;
    const body = new URLSearchParams();
    body.set("csrf_token", token);
    for (const name of SAVED_FIELDS) {
      const input = dialog.querySelector(`[data-calc="${name}"]`);
      if (input) body.set(name, input.value.trim());
    }
    saveStatus(dialog, "Saving…");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { Accept: "application/json" },
        body,
        credentials: "same-origin",
      });
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) {
        throw new Error("Could not save calculator values. Reload the page and try again.");
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not save calculator values.");
      saveStatus(dialog, "Saved.");
    } catch (exc) {
      saveStatus(dialog, exc.message || "Could not save calculator values.", true);
    }
  };

  const saveSettings = (dialog) => {
    // Serialize saves so rapid edits cannot finish out of order and restore an older value.
    const previous = pendingSaves.get(dialog) || Promise.resolve();
    const next = previous.then(() => persistSettings(dialog)).catch(() => {});
    pendingSaves.set(dialog, next);
    return next;
  };

  const renderCosts = (dialog, data) => {
    const totals = data.totals || {};
    if (integer(totals.calls) === 0) {
      clearResults(dialog);
      return;
    }
    const price = (name) => number(field(dialog, name));
    const cost = (slice, hit, miss, output) => {
      if (!slice) return 0;
      return (
        (integer(slice.cache_hit_tokens) * price(hit) +
          integer(slice.cache_miss_tokens) * price(miss) +
          integer(slice.output_tokens) * price(output)) /
        1_000_000
      );
    };

    const hasPeak = Boolean(data.peak_hours);
    let total = 0;
    if (hasPeak) {
      const peak = cost(data.peak, "hit-peak", "miss-peak", "output-peak");
      const offPeak = cost(data.off_peak, "hit-off-peak", "miss-off-peak", "output-off-peak");
      setResult(dialog, "peak", peak);
      setResult(dialog, "off-peak", offPeak);
      total = peak + offPeak;
    } else {
      total = cost(totals, "hit-peak", "miss-peak", "output-peak");
    }

    const replies = integer(totals.replies);
    const perReply = replies > 0 ? total / replies : null;
    setResult(dialog, "total", total);
    setResult(dialog, "per-reply", perReply);
    setResult(dialog, "thousand", perReply == null ? null : perReply * 1000);
  };

  const renderUsage = (dialog, data) => {
    const totals = data.totals || {};
    const hasPeak = Boolean(data.peak_hours);
    const show = integer(totals.calls) > 0;
    const usage = dialog.querySelector("[data-calc-usage]");
    const averages = dialog.querySelector("[data-calc-averages]");
    const status = dialog.querySelector("[data-calc-status]");

    setHidden(dialog, '[data-usage^="peak-"], [data-usage^="off-"], [data-usage-peak], [data-usage-off-peak]', !hasPeak);
    setHidden(dialog, "[data-price-heading='off-peak'], [data-price-cell='off-peak']", !hasPeak);
    setText(dialog, "[data-price-heading='primary']", hasPeak ? "Peak" : "Price");
    setHidden(dialog, "[data-calc-peak-result]", !hasPeak);
    setHidden(dialog, "[data-calc-result='peak'], [data-calc-result='off-peak']", !hasPeak);

    const peak = data.peak || {};
    const offPeak = data.off_peak || {};
    const cells = [
      ["total-hit", totals.cache_hit_tokens],
      ["total-miss", totals.cache_miss_tokens],
      ["total-output", totals.output_tokens],
      ["peak-hit", peak.cache_hit_tokens],
      ["peak-miss", peak.cache_miss_tokens],
      ["peak-output", peak.output_tokens],
      ["off-hit", offPeak.cache_hit_tokens],
      ["off-miss", offPeak.cache_miss_tokens],
      ["off-output", offPeak.output_tokens],
    ];
    for (const [name, value] of cells) {
      setText(dialog, `[data-usage="${name}"]`, countFormat.format(integer(value)));
    }

    if (usage) usage.hidden = !show;
    if (averages) {
      const replies = integer(totals.replies);
      averages.hidden = !show || replies === 0;
      if (show && replies > 0) {
        const average = (value) => Math.ceil(integer(value) / replies);
        averages.textContent =
          `Average per reported reply: ${countFormat.format(average(totals.cache_hit_tokens))} cache hit input, ` +
          `${countFormat.format(average(totals.cache_miss_tokens))} cache miss input, ` +
          `${countFormat.format(average(totals.output_tokens))} output tokens.`;
      }
    }
    if (status) {
      status.textContent = show
        ? `Based on ${countFormat.format(integer(totals.calls))} reported calls across ` +
          `${countFormat.format(integer(totals.replies))} reported ` +
          `${integer(totals.replies) === 1 ? "reply" : "replies"}.`
        : "No provider-reported token usage for the selected period.";
    }
    renderCosts(dialog, data);
  };

  const loadUsage = async (dialog) => {
    const error = dialog.querySelector("[data-calc-error]");
    const status = dialog.querySelector("[data-calc-status]");
    const start = field(dialog, "peak-start");
    const end = field(dialog, "peak-end");
    if ((start && !end) || (!start && end)) {
      if (error) {
        error.hidden = false;
        error.textContent = "Enter both peak times, or leave both blank.";
      }
      if (status) status.textContent = "";
      return;
    }
    if (error) error.hidden = true;

    let url;
    try {
      url = new URL(dialog.dataset.usageUrl, window.location.origin);
    } catch {
      if (error) {
        error.hidden = false;
        error.textContent = "This calculator has no usage source.";
      }
      return;
    }
    if (start && end) {
      url.searchParams.set("peak_start", start);
      url.searchParams.set("peak_end", end);
    } else {
      url.searchParams.delete("peak_start");
      url.searchParams.delete("peak_end");
    }

    const previous = pending.get(dialog);
    if (previous) previous.abort();
    const controller = new AbortController();
    pending.set(dialog, controller);
    if (status) status.textContent = "Loading reported usage…";
    try {
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
        credentials: "same-origin",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || "Could not load usage.");
      if (controller.signal.aborted) return;
      dialog._usage = data;
      renderUsage(dialog, data);
    } catch (exc) {
      if (exc.name === "AbortError") return;
      dialog._usage = null;
      if (status) status.textContent = "";
      if (error) {
        error.hidden = false;
        error.textContent = exc.message || "Could not load usage.";
      }
      const usage = dialog.querySelector("[data-calc-usage]");
      if (usage) usage.hidden = true;
      clearResults(dialog);
    }
  };

  document.querySelectorAll("[data-calculator]").forEach((dialog) => {
    dialog.querySelectorAll('[data-calc="peak-start"], [data-calc="peak-end"]').forEach((input) => {
      input.addEventListener("change", () => {
        saveSettings(dialog);
        loadUsage(dialog);
      });
    });
    dialog
      .querySelectorAll(
        '[data-calc="hit-peak"], [data-calc="miss-peak"], [data-calc="output-peak"], ' +
          '[data-calc="hit-off-peak"], [data-calc="miss-off-peak"], [data-calc="output-off-peak"]',
      )
      .forEach((input) => {
        input.addEventListener("input", () => {
          if (dialog._usage) renderCosts(dialog, dialog._usage);
        });
        input.addEventListener("change", () => saveSettings(dialog));
      });
  });

  document.querySelectorAll("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.dialogOpen);
      if (!dialog) return;
      if (!dialog.open) dialog.showModal();
      if (dialog.matches("[data-calculator]")) {
        saveStatus(dialog, "");
        loadUsage(dialog);
      }
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
