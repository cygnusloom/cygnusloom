/* Large tabs for the desktop main menu. The choice survives app restarts.
 *
 * The local server gets a new port each launch, so localStorage/sessionStorage cannot
 * carry a choice across launches: storage is scoped to scheme, host, and port. Cookies
 * ignore the port but are shared by the Qt profile, which makes them the right home for
 * this preference.
 */
(() => {
  const list = document.querySelector(".play-tabs");
  const tabs = Array.from(document.querySelectorAll("[data-play-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-play-panel]"));
  if (!list || !tabs.length || !panels.length) return;
  const COOKIE = "cygnus_play_tab";
  const MAX_AGE = 60 * 60 * 24 * 365;

  function remembered() {
    try {
      const match = document.cookie.match(/(?:^|;\s*)cygnus_play_tab=([^;]*)/);
      return match ? decodeURIComponent(match[1]) : "";
    } catch (error) {
      return "";
    }
  }

  function remember(name) {
    try {
      document.cookie = `${COOKIE}=${encodeURIComponent(name)}; Path=/; Max-Age=${MAX_AGE}; SameSite=Lax`;
    } catch (error) {
      // A browser that blocks cookies can still switch tabs for this page.
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
  const initial = validHash
    ? hash
    : remembered() || list.dataset.playInitial || "device";
  select(initial, false);
})();
