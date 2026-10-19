(() => {
  const transcript = document.querySelector("[data-transcript]");
  if (transcript) {
    transcript.scrollTop = transcript.scrollHeight;
  }

  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
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
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const outside =
        event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) dialog.close();
    });
  });

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
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".drawer.open").forEach((drawer) => setDrawer(drawer.id, false));
  });
})();
