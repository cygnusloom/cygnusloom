/* The desktop main menu. The Qt window injects window.__cygnusDesktop on its own pages and reads
   the prefixed console messages below. A page that loses the injected flag asks for the bridge
   with a hello action, and a regular browser sees the note instead. */
(() => {
  const PREFIX = "cygnus-desktop:";
  const STATUS_ID = "desktop-invite-status";
  const note = document.getElementById("desktop-note");
  let started = false;

  function post(action, data) {
    console.log(PREFIX + JSON.stringify(Object.assign({ action }, data || {})));
  }

  function send(action, data) {
    if (!window.__cygnusDesktop) return;
    post(action, data);
  }

  function openExternal(event) {
    const anchor = event.target && event.target.closest
      ? event.target.closest("a[href]")
      : null;
    if (!anchor) return;
    let target;
    try {
      target = new URL(anchor.href, location.href);
    } catch (error) {
      return;
    }
    if (target.origin === location.origin) return;
    if (!/^https?:$/.test(target.protocol) && target.protocol !== "mailto:") return;
    event.preventDefault();
    event.stopPropagation();
    post("external", { url: target.href });
  }

  function start() {
    if (started) return;
    started = true;
    window.__cygnusDesktop = true;
    if (note) note.hidden = true;
    if (!window.__cygnusExternalGuard) {
      window.__cygnusExternalGuard = true;
      document.addEventListener("click", openExternal, true);
    }

    document.querySelectorAll("[data-desktop-connect]").forEach((button) => {
      button.addEventListener("click", () =>
        send("connect", { server: button.dataset.desktopConnect })
      );
    });
    document.querySelectorAll("[data-desktop-login]").forEach((button) => {
      button.addEventListener("click", () =>
        send("login", { server: button.dataset.desktopLogin })
      );
    });
    document.querySelectorAll("[data-desktop-browser]").forEach((button) => {
      button.addEventListener("click", () =>
        send("browser", { server: button.dataset.desktopBrowser })
      );
    });

    const addForm = document.querySelector("form[data-desktop-add]");
    if (addForm) {
      addForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const data = new FormData(addForm);
        send("add", { url: data.get("url"), name: data.get("name") });
      });
    }

    const invite = document.querySelector("[data-desktop-invite]");
    if (invite) {
      const status = document.getElementById(STATUS_ID);
      if (status) status.textContent = "Opening the invitation in the app…";
      send("invite", { url: invite.dataset.desktopInvite });
    }
  }

  if (window.__cygnusDesktop) {
    start();
    return;
  }

  // A regular browser has no bridge; show the note after giving the app a moment to answer.
  setTimeout(() => {
    if (note && !window.__cygnusDesktop) note.hidden = false;
  }, 300);
  window.addEventListener("cygnus-desktop-ready", start, { once: true });
  post("hello");
})();
