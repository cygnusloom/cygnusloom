(() => {
  const transcript = document.querySelector("[data-transcript]");
  if (transcript) {
    transcript.scrollTop = transcript.scrollHeight;
  }

  // On phones the details panel is a drawer opened from the bottom bar.
  const shell = document.querySelector(".shell");
  const side = document.querySelector(".side");
  const sideToggle = document.querySelector("[data-side-toggle]");
  const setSide = (open, focus = true) => {
    if (!shell || !side || !sideToggle) return;
    shell.classList.toggle("show-side", open);
    sideToggle.setAttribute("aria-pressed", String(open));
    if (!focus) return;
    if (open) side.querySelector('[role="tab"]')?.focus();
    else sideToggle.focus();
  };
  if (side && sideToggle) {
    sideToggle.addEventListener("click", () => setSide(!shell.classList.contains("show-side")));
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (shell.classList.contains("show-side")) setSide(false);
    });
    const wide = window.matchMedia("(min-width: 901px)");
    wide.addEventListener("change", (event) => {
      if (event.matches) setSide(false, false);
    });
  }

  document.querySelectorAll('[role="tablist"]').forEach((list) => {
    const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
    const select = (tab) => {
      for (const other of tabs) {
        const selected = other === tab;
        other.setAttribute("aria-selected", String(selected));
        other.tabIndex = selected ? 0 : -1;
        document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
      }
      tab.focus();
    };

    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => select(tab));
      tab.addEventListener("keydown", (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (step === undefined) return;
        event.preventDefault();
        select(tabs[(index + step + tabs.length) % tabs.length]);
      });
    });
  });

  const closeOnBackdropClick = (dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const outside =
        event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) dialog.close();
    });
  };

  const showView = (name) => {
    for (const view of document.querySelectorAll("[data-view]")) {
      view.hidden = view.dataset.view !== name;
    }
    for (const button of document.querySelectorAll("[data-view-target]")) {
      button.setAttribute("aria-pressed", String(button.dataset.viewTarget === name));
    }
  };
  document.querySelectorAll("[data-view-target]").forEach((button) => {
    button.addEventListener("click", () => {
      showView(button.dataset.viewTarget);
      setSide(false, false);
    });
  });

  // A search box narrows the cards in the region its data-filter names. A group left with no
  // matching card is hidden too, and the page says so when nothing matches at all.
  document.querySelectorAll("[data-filter]").forEach((input) => {
    const target = document.querySelector(input.dataset.filter);
    if (!target) return;
    const items = Array.from(target.querySelectorAll("[data-filter-item]"));
    const groups = Array.from(target.querySelectorAll("[data-filter-group]"));
    const empty = document.querySelector("[data-filter-empty]");
    const apply = () => {
      const query = input.value.trim().toLocaleLowerCase();
      let shown = 0;
      for (const item of items) {
        const text = (item.dataset.filterText ?? item.textContent ?? "").toLocaleLowerCase();
        const match = !query || text.includes(query);
        item.hidden = !match;
        if (match) shown += 1;
      }
      for (const group of groups) {
        group.hidden = !group.querySelector("[data-filter-item]:not([hidden])");
      }
      if (empty) empty.hidden = shown > 0;
    };
    input.addEventListener("input", apply);
    apply();
  });

  document.querySelectorAll("[data-dialog-target]").forEach((button) => {
    const dialog = document.getElementById(button.dataset.dialogTarget);
    button.addEventListener("click", () => {
      dialog.showModal();
      // The browser focuses the first control, which can be at the bottom of a long dialog.
      const title = dialog.querySelector("h2");
      if (title) {
        title.tabIndex = -1;
        title.focus();
      }
      dialog.scrollTop = 0;
    });
    dialog.addEventListener("close", () => button.focus());
    closeOnBackdropClick(dialog);
  });

  const turnDialog = document.getElementById("dialog-turn-context");
  if (turnDialog) {
    const bodies = {
      context: turnDialog.querySelector("[data-turn-context-body]"),
      transcript: turnDialog.querySelector("[data-turn-transcript-body]"),
    };
    const loadFragment = async (url) => {
      try {
        const response = await fetch(url, {
          headers: { Accept: "text/html" },
          credentials: "same-origin",
        });
        if (response.ok && !response.redirected) return await response.text();
      } catch (error) {
        return null;
      }
      return null;
    };
    const unavailable = "<p class=\"muted\">Could not load this reply's details.</p>";
    let opener = null;
    document.querySelectorAll("[data-turn-context-url]").forEach((button) => {
      button.addEventListener("click", async () => {
        opener = button;
        bodies.context.textContent = "Loading…";
        bodies.transcript.textContent = "Loading…";
        turnDialog.showModal();
        const [context, transcript] = await Promise.all([
          loadFragment(button.dataset.turnContextUrl),
          loadFragment(button.dataset.turnTranscriptUrl),
        ]);
        // Ignore replies for a gear that has since been replaced by another click.
        if (opener !== button) return;
        bodies.context.innerHTML = context ?? unavailable;
        bodies.transcript.innerHTML = transcript ?? unavailable;
      });
    });
    turnDialog.addEventListener("close", () => opener?.focus());
    closeOnBackdropClick(turnDialog);
  }

  const composerInput = document.querySelector(".composer textarea");
  if (composerInput) {
    composerInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      composerInput.form.requestSubmit();
    });
  }
})();
