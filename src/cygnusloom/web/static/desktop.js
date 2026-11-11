/* The desktop main menu. The Qt window injects window.__cygnusDesktop on its own pages and reads
   the prefixed console messages below. A regular browser sees the note instead. */
(() => {
  const PREFIX = "cygnus-desktop:";
  const STATUS_ID = "desktop-invite-status";

  function send(action, data) {
    if (!window.__cygnusDesktop) return;
    console.log(PREFIX + JSON.stringify(Object.assign({ action }, data || {})));
  }

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  if (!window.__cygnusDesktop) {
    const note = document.getElementById("desktop-note");
    if (note) note.hidden = false;
    return;
  }

  document.querySelectorAll("[data-desktop-connect]").forEach((button) => {
    button.addEventListener("click", () => send("connect", { server: button.dataset.desktopConnect }));
  });
  document.querySelectorAll("[data-desktop-login]").forEach((button) => {
    button.addEventListener("click", () => send("login", { server: button.dataset.desktopLogin, saved: true }));
  });
  document.querySelectorAll("[data-desktop-browser]").forEach((button) => {
    button.addEventListener("click", () => send("browser", { server: button.dataset.desktopBrowser }));
  });

  const addForm = document.querySelector("form[data-desktop-add]");
  if (addForm) {
    addForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(addForm);
      send("add", { url: data.get("url"), name: data.get("name") });
    });
  }

  const params = new URLSearchParams(window.location.search);
  const loginServer = params.get("desktop_login");
  if (loginServer) {
    const saved = params.get("desktop_remember") !== "0";
    params.delete("desktop_login");
    params.delete("desktop_remember");
    const query = params.toString();
    history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}`);
    send("login", { server: loginServer, saved });
  }

  const invite = document.querySelector("[data-desktop-invite]");
  if (invite) {
    const status = document.getElementById(STATUS_ID);
    if (status) status.textContent = "Opening the invitation in the app…";
    send("invite", { url: invite.dataset.desktopInvite });
  }
})();
