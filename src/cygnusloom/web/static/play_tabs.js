/* Large tabs for the desktop main menu. The selected tab survives form redirects. */
(() => {
  const tabs = Array.from(document.querySelectorAll("[data-play-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-play-panel]"));
  if (!tabs.length || !panels.length) return;
  const STORAGE_KEY = "cygnus.playTab";

  function remembered() {
    try {
      return sessionStorage.getItem(STORAGE_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function remember(name) {
    try {
      sessionStorage.setItem(STORAGE_KEY, name);
    } catch (error) {
      // A restricted browser can still switch tabs for this page.
    }
  }

  function select(name, updateHash = true) {
    if (!tabs.some((tab) => tab.dataset.playTab === name)) name = "device";
    tabs.forEach((tab) => {
      const active = tab.dataset.playTab === name;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.playPanel !== name;
    });
    remember(name);
    if (updateHash) history.replaceState(null, "", `#${name}`);
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab.dataset.playTab));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const step = event.key === "ArrowRight" ? 1 : -1;
      const next = (index + step + tabs.length) % tabs.length;
      tabs[next].focus();
      select(tabs[next].dataset.playTab);
    });
  });

  const hash = location.hash.replace(/^#/, "");
  const validHash = tabs.some((tab) => tab.dataset.playTab === hash);
  select(validHash ? hash : remembered() || "device", false);
})();
