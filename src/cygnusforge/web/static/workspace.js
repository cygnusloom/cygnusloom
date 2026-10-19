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
    const body = turnDialog.querySelector("[data-turn-context-body]");
    let opener = null;
    document.querySelectorAll("[data-turn-context-url]").forEach((button) => {
      button.addEventListener("click", async () => {
        opener = button;
        body.textContent = "Loading…";
        turnDialog.showModal();
        let html = null;
        try {
          const response = await fetch(button.dataset.turnContextUrl, {
            headers: { Accept: "text/html" },
            credentials: "same-origin",
          });
          if (response.ok && !response.redirected) html = await response.text();
        } catch (error) {
          html = null;
        }
        // Ignore a reply for a gear that has since been replaced by another click.
        if (opener !== button) return;
        body.innerHTML = html ?? "<p class=\"muted\">Could not load this reply's details.</p>";
      });
    });
    turnDialog.addEventListener("close", () => opener?.focus());
    closeOnBackdropClick(turnDialog);
  }

  const drawerButtons = Array.from(document.querySelectorAll("[data-drawer-target]"));
  const setDrawer = (id, open) => {
    const drawer = document.getElementById(id);
    if (!open && drawer.contains(document.activeElement)) {
      drawerButtons.find((button) => button.dataset.drawerTarget === id).focus();
    }
    drawer.classList.toggle("open", open);
    for (const button of drawerButtons) {
      if (button.dataset.drawerTarget === id) {
        button.setAttribute("aria-expanded", String(open));
      }
    }
  };
  drawerButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.dataset.drawerTarget;
      setDrawer(id, !document.getElementById(id).classList.contains("open"));
    });
  });
  document.querySelectorAll("[data-drawer-close]").forEach((button) => {
    button.addEventListener("click", () => setDrawer(button.closest(".drawer").id, false));
  });
  const composerInput = document.querySelector(".composer textarea");
  if (composerInput) {
    composerInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      composerInput.form.requestSubmit();
    });
  }

  document.addEventListener("click", (event) => {
    for (const drawer of document.querySelectorAll(".drawer.open")) {
      if (drawer.contains(event.target)) continue;
      if (event.target.closest(`[data-drawer-target="${drawer.id}"]`)) continue;
      setDrawer(drawer.id, false);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".drawer.open").forEach((drawer) => setDrawer(drawer.id, false));
  });
})();
