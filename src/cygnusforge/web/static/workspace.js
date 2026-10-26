(() => {
  const transcript = document.querySelector("[data-transcript]");
  if (transcript) {
    transcript.scrollTop = transcript.scrollHeight;
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
    button.addEventListener("click", () => showView(button.dataset.viewTarget));
  });

  document.querySelectorAll("[data-dialog-target]").forEach((button) => {
    const dialog = document.getElementById(button.dataset.dialogTarget);
    button.addEventListener("click", () => dialog.showModal());
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
