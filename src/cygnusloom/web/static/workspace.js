(() => {
  // The turn stream can replace parts of the page when a reply finishes, then call
  // initWorkspace on the fresh region. Binding once per element keeps the original listeners
  // from stacking up and keeps the controls that survive a turn working.
  const bindings = new WeakMap();
  const boundTabLists = new WeakSet();
  const boundCompendiums = new WeakSet();
  const dialogOpeners = new WeakMap();
  const on = (element, key, type, handler, options) => {
    if (!element) return;
    let keys = bindings.get(element);
    if (!keys) {
      keys = new Set();
      bindings.set(element, keys);
    }
    if (keys.has(key)) return;
    keys.add(key);
    element.addEventListener(type, handler, options);
  };

  // A sideways-scrolling bar hides its scrollbar, and a mouse has no way to drag it, so the
  // wheel scrolls the bar while some content is out of view. At the ends the page scrolls as
  // usual.
  const bindWheelScroll = (element) => {
    on(
      element,
      "wheel-scroll",
      "wheel",
      (event) => {
        if (event.ctrlKey || event.shiftKey || event.altKey || event.metaKey) return;
        if (element.scrollWidth <= element.clientWidth) return;
        const raw =
          Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
        if (!raw) return;
        const atStart = element.scrollLeft <= 0;
        const atEnd = element.scrollLeft + element.clientWidth >= element.scrollWidth - 1;
        if ((raw < 0 && atStart) || (raw > 0 && atEnd)) return;
        const scale =
          event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientWidth : 1;
        event.preventDefault();
        element.scrollLeft += raw * scale;
      },
      { passive: false },
    );
  };

  // On phones the details panel is a drawer opened from the bottom bar.
  const shell = document.querySelector(".shell");
  const sideToggle = document.querySelector("[data-side-toggle]");
  const setSide = (open, focus = true) => {
    // The side panel is replaced in place after a turn, so look it up instead of holding the
    // old element.
    const side = document.querySelector(".side");
    if (!shell || !side || !sideToggle) return;
    shell.classList.toggle("show-side", open);
    sideToggle.setAttribute("aria-pressed", String(open));
    if (open) setMenu(false, false);
    if (!focus) return;
    if (open) side.querySelector('[role="tab"]')?.focus();
    else sideToggle.focus();
  };
  if (shell && sideToggle) {
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

  // The campaign workspace folds the site menu away; its first sidebar button expands it.
  const menuToggle = document.querySelector("[data-menu-toggle]");
  const setMenu = (open, focus = true) => {
    document.body.classList.toggle("menu-open", open);
    menuToggle?.setAttribute("aria-expanded", String(open));
    if (!focus) return;
    if (open) document.querySelector("#site-menu a, #site-menu button")?.focus();
    else menuToggle?.focus();
  };
  if (menuToggle) {
    menuToggle.addEventListener("click", () => {
      const open = !document.body.classList.contains("menu-open");
      // The details drawer sits above the page, so it would hide the menu; close it first.
      if (open) setSide(false, false);
      setMenu(open);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (document.body.classList.contains("menu-open")) setMenu(false, false);
    });
  }

  // A turn stream can redraw the map panel while the GM works, so tab wiring lives in a function
  // the page calls again after a redraw, and the select helper is exposed for restoring the tab
  // the player was reading.
  const initTabs = (root = document) => {
    root.querySelectorAll('[role="tablist"]').forEach((list) => {
      if (boundTabLists.has(list)) return;
      boundTabLists.add(list);
      const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
      const select = (tab, focus = true) => {
        for (const other of tabs) {
          const selected = other === tab;
          other.setAttribute("aria-selected", String(selected));
          other.tabIndex = selected ? 0 : -1;
          const panel = document.getElementById(other.getAttribute("aria-controls"));
          if (panel) panel.hidden = !selected;
        }
        if (focus) tab.focus();
        else tab.scrollIntoView({ block: "nearest", inline: "nearest" });
      };
      list.selectTab = select;

      tabs.forEach((tab, index) => {
        on(tab, "tab-click", "click", () => select(tab));
        on(tab, "tab-key", "keydown", (event) => {
          const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
          if (step === undefined) return;
          event.preventDefault();
          let next = (index + step + tabs.length) % tabs.length;
          // A compendium search hides tabs with no matches; skip them when arrowing.
          while (tabs[next].hidden && next !== index) {
            next = (next + step + tabs.length) % tabs.length;
          }
          select(tabs[next]);
        });
      });

      bindWheelScroll(list);
    });
  };
  window.initWorkspaceTabs = initTabs;
  window.workspaceSelectTab = (id) => {
    const tab = document.getElementById(id);
    const list = tab && tab.closest('[role="tablist"]');
    if (tab && list && list.selectTab) list.selectTab(tab, false);
  };

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
  window.showWorkspaceView = showView;

  // A compendium entry lives inside a tab panel, so opening one also selects its tab.
  const revealCompendiumTarget = (target) => {
    const panel = target.closest("[data-compendium-panel]");
    if (!panel) return;
    const tab = document.querySelector(
      `[data-compendium-tab="${panel.dataset.compendiumPanel}"]`,
    );
    const list = tab && tab.closest('[role="tablist"]');
    if (tab && list && list.selectTab) list.selectTab(tab, false);
  };

  // The compendium toolbar sticks to the top of its view, so an entry scrolled to the top would
  // sit underneath it. The entries take the toolbar's measured height as their scroll margin;
  // remeasure when it wraps as the viewport or the tab list changes size.
  const measureCompendiumToolbars = (root = document) => {
    for (const compendium of root.querySelectorAll("[data-compendium]")) {
      const toolbar = compendium.querySelector(".compendium-toolbar");
      if (toolbar) {
        compendium.style.setProperty("--compendium-toolbar-height", `${toolbar.offsetHeight}px`);
      }
    }
  };
  window.addEventListener("load", () => measureCompendiumToolbars());
  window.addEventListener("resize", () => measureCompendiumToolbars());

  // A link can open a compendium entry directly, such as a bookmarked anchor. Show the view
  // that holds the target before scrolling to it, since a hidden view cannot scroll.
  const openHash = () => {
    const anchored = location.hash && document.querySelector(location.hash);
    if (!anchored) return;
    const view = anchored.closest("[data-view]");
    if (view) showView(view.dataset.view);
    revealCompendiumTarget(anchored);
    measureCompendiumToolbars();
    anchored.scrollIntoView({ block: "start" });
  };
  // Some browsers restore the fragment after the first script pass, so retry once at load.

  // A link inside one view can point at an entry in another, such as an inventory item in the
  // compendium: switch views first, then scroll to the anchor the link names.
  const bindViewLinks = (root) => {
    root.querySelectorAll("[data-view-link]").forEach((link) => {
      on(link, "view-link", "click", (event) => {
        const target = document.querySelector(link.getAttribute("href"));
        if (!target) return;
        event.preventDefault();
        showView(link.dataset.viewLink);
        revealCompendiumTarget(target);
        measureCompendiumToolbars();
        target.scrollIntoView({ block: "start" });
        history.replaceState(null, "", link.getAttribute("href"));
      });
    });
  };

  // A search box narrows the cards in the region its data-filter names. A group left with no
  // matching card is hidden too, and the page says so when nothing matches at all.
  const bindFilters = (root) => {
    root.querySelectorAll("[data-filter]").forEach((input) => {
      const target = document.querySelector(input.dataset.filter);
      if (!target) return;
      const items = Array.from(target.querySelectorAll("[data-filter-item]"));
      const groups = Array.from(target.querySelectorAll("[data-filter-group]"));
      // The empty message sits beside the filtered region, so each search box reports its own
      // result instead of whichever message appears first on a page with two searches.
      const empty =
        target.parentElement?.querySelector("[data-filter-empty]") ??
        document.querySelector("[data-filter-empty]");
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
      on(input, "filter", "input", apply);
      apply();
    });
  };

  // The compendium search filters every entry, including entries in other tabs, and moves to the
  // first tab with a match when the current one has none.
  const bindCompendiumSearch = (root) => {
    root.querySelectorAll("[data-compendium]").forEach((compendium) => {
      if (boundCompendiums.has(compendium)) return;
      boundCompendiums.add(compendium);
      const input = compendium.querySelector("[data-compendium-search]");
      if (!input) return;
      const entries = [...compendium.querySelectorAll("[data-compendium-entry]")];
      const sections = [...compendium.querySelectorAll("[data-compendium-section]")];
      const panels = [...compendium.querySelectorAll("[data-compendium-panel]")];
      const tabs = [...compendium.querySelectorAll("[data-compendium-tab]")];
      const tabList = compendium.querySelector('.compendium-tab-list[role="tablist"]');
      const count = compendium.querySelector("[data-compendium-count]");
      const empty = compendium.querySelector("[data-compendium-empty]");
    const total = entries.length;
    const tabCounts = new Map(
      tabs.map((tab) => [tab, tab.querySelector(".compendium-count")?.textContent ?? ""]),
    );
    let previousTab = "";

    const selectTab = (panelId) => {
      const tab = tabs.find((candidate) => candidate.dataset.compendiumTab === panelId);
      if (tab && tabList && tabList.selectTab) tabList.selectTab(tab, false);
    };

    const apply = () => {
      const query = input.value.trim().toLocaleLowerCase();
      if (!query) {
        for (const entry of entries) entry.hidden = false;
        for (const section of sections) section.hidden = false;
        for (const tab of tabs) {
          tab.hidden = false;
          const badge = tab.querySelector(".compendium-count");
          if (badge) badge.textContent = tabCounts.get(tab) ?? "";
        }
        if (previousTab) {
          selectTab(previousTab);
          previousTab = "";
        }
        if (count) count.textContent = `${total} ${total === 1 ? "entry" : "entries"}`;
        if (empty) empty.hidden = true;
        return;
      }
      if (!previousTab) {
        const selected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
        previousTab = selected?.dataset.compendiumTab ?? "";
      }
      const matching = new Set();
      for (const entry of entries) {
        const text = (entry.dataset.compendiumText ?? "").toLocaleLowerCase();
        const match = text.includes(query);
        entry.hidden = !match;
        if (match) matching.add(entry);
      }
      for (const section of sections) {
        section.hidden = !section.querySelector("[data-compendium-entry]:not([hidden])");
      }
      for (const panel of panels) {
        const matches = panel.querySelectorAll("[data-compendium-entry]:not([hidden])").length;
        const tab = tabs.find(
          (candidate) => candidate.dataset.compendiumTab === panel.dataset.compendiumPanel,
        );
        if (tab) {
          tab.hidden = matches === 0;
          const badge = tab.querySelector(".compendium-count");
          if (badge) badge.textContent = String(matches);
        }
      }
      const selected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true");
      if (!selected || selected.hidden) {
        const first = panels.find((panel) =>
          panel.querySelector("[data-compendium-entry]:not([hidden])"),
        );
        if (first) selectTab(first.dataset.compendiumPanel);
      }
      if (count) count.textContent = `${matching.size} of ${total} entries`;
      if (empty) empty.hidden = matching.size > 0;
    };

      on(input, "compendium-search", "input", apply);
      apply();
    });
  };

  const bindDialogButtons = (root) => {
    root.querySelectorAll("[data-dialog-target]").forEach((button) => {
      const dialog = document.getElementById(button.dataset.dialogTarget);
      if (!dialog) return;
      on(button, "dialog-open", "click", () => {
        dialogOpeners.set(dialog, button);
        dialog.showModal();
        // The browser focuses the first control, which can be at the bottom of a long dialog.
        const title = dialog.querySelector("h2");
        if (title) {
          title.tabIndex = -1;
          title.focus();
        }
        dialog.scrollTop = 0;
      });
    });
  };
  // Dialogs sit outside the regions a turn replaces, so bind their close handlers only once.
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("close", () => dialogOpeners.get(dialog)?.focus());
    closeOnBackdropClick(dialog);
  });

  const turnDialog = document.getElementById("dialog-turn-context");
  let turnOpener = null;
  const bindTurnContextButtons = (root) => {
    if (!turnDialog) return;
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
    root.querySelectorAll("[data-turn-context-url]").forEach((button) => {
      on(button, "turn-context", "click", async () => {
        turnOpener = button;
        bodies.context.textContent = "Loading…";
        bodies.transcript.textContent = "Loading…";
        turnDialog.showModal();
        const [context, transcript] = await Promise.all([
          loadFragment(button.dataset.turnContextUrl),
          loadFragment(button.dataset.turnTranscriptUrl),
        ]);
        // Ignore replies for a gear that has since been replaced by another click.
        if (turnOpener !== button) return;
        bodies.context.innerHTML = context ?? unavailable;
        bodies.transcript.innerHTML = transcript ?? unavailable;
      });
    });
  };
  if (turnDialog) turnDialog.addEventListener("close", () => turnOpener?.focus());

  // On phones the message box starts at one row and grows with what is typed, up to a point.
  // Desktop keeps its three-row box.
  const autosizeComposer = (input) => {
    if (!window.matchMedia("(max-width: 700px)").matches) {
      input.style.height = "";
      return;
    }
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 128)}px`;
  };
  const bindComposer = (root) => {
    root.querySelectorAll(".composer textarea").forEach((input) => {
      on(input, "composer-enter", "keydown", (event) => {
        if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        input.form.requestSubmit();
      });
      on(input, "composer-autosize", "input", () => autosizeComposer(input));
      // The submit handler clears the box after this listener runs, so measure on the next frame.
      on(input.form, "composer-autosize-reset", "submit", () => {
        requestAnimationFrame(() => autosizeComposer(input));
      });
    });
  };

  const observedToolbars = new WeakSet();
  const initWorkspace = (root = document) => {
    initTabs(root);
    bindViewLinks(root);
    bindFilters(root);
    bindCompendiumSearch(root);
    bindDialogButtons(root);
    bindTurnContextButtons(root);
    bindComposer(root);
    measureCompendiumToolbars(root);
    if ("ResizeObserver" in window) {
      root.querySelectorAll("[data-compendium] .compendium-toolbar").forEach((toolbar) => {
        if (observedToolbars.has(toolbar)) return;
        observedToolbars.add(toolbar);
        new ResizeObserver(() => measureCompendiumToolbars()).observe(toolbar);
      });
    }
  };
  window.initWorkspace = initWorkspace;
  initWorkspace();
  openHash();
  window.addEventListener("hashchange", openHash);
  window.addEventListener("load", openHash);

  const transcript = document.querySelector("[data-transcript]");
  if (transcript) transcript.scrollTop = transcript.scrollHeight;
})();
