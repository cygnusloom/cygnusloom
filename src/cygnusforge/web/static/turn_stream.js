(() => {
  // Forms marked data-confirm (rollback) ask before submitting. This runs even without streaming.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  const transcript = document.querySelector("[data-transcript]");
  // The "Show hidden events" checkbox toggles a class; the CSS hides .hidden-event lines otherwise.
  const showHidden = document.querySelector("[data-show-hidden]");
  const syncShowHidden = () => {
    if (transcript && showHidden) transcript.classList.toggle("show-hidden", showHidden.checked);
  };
  if (showHidden) {
    showHidden.checked = false;
    showHidden.addEventListener("change", syncShowHidden);
    syncShowHidden();
  }

  const composer = document.getElementById("composer");
  if (!composer || !composer.dataset.streamAction || !window.ReadableStream) return;

  const errorBox = document.querySelector(".turn-error");
  const textarea = composer.querySelector("textarea");
  const submit = composer.querySelector('button[type="submit"]');
  const NOT_SAVED = "The reply was not saved. Your message is still in the box.";
  const FLASH_KEY = "cygnus-turn-error";
  const FOCUS_KEY = "cygnus-focus-composer";

  class TurnFailure extends Error {}

  const addBubble = (role, label) => {
    for (const placeholder of transcript.querySelectorAll(":scope > p.muted")) {
      placeholder.remove();
    }
    const bubble = document.createElement("div");
    bubble.className = `message ${role}`;
    const header = document.createElement("div");
    header.className = "message-header";
    const name = document.createElement("span");
    name.className = "role";
    name.textContent = label;
    header.append(name);
    const content = document.createElement("div");
    content.className = "content";
    bubble.append(header, content);
    let status = null;
    let statusText = null;
    if (role === "assistant") {
      bubble.classList.add("streaming");
      status = document.createElement("div");
      status.className = "turn-status";
      status.setAttribute("role", "status");
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      spinner.setAttribute("aria-hidden", "true");
      statusText = document.createElement("span");
      status.append(spinner, statusText);
      bubble.append(status);
    }
    transcript.append(bubble);
    return { bubble, content, status, statusText };
  };

  const scrollDown = () => {
    transcript.scrollTop = transcript.scrollHeight;
  };

  // Log entries sit in the chat just before the reply they belong to.
  const appendLog = (text, hidden, before) => {
    const line = document.createElement("div");
    line.className = hidden ? "log-line hidden-event" : "log-line";
    line.textContent = text;
    transcript.insertBefore(line, before);
  };

  // "busy" while the GM is working on a turn; "waiting" once the page is ready for player input.
  const setGmState = (state) => {
    transcript.dataset.gmState = state;
  };

  const setBusy = (busy) => {
    submit.disabled = busy;
    textarea.readOnly = busy;
    setGmState(busy ? "busy" : "waiting");
  };

  const showError = (message) => {
    errorBox.textContent = message;
    errorBox.hidden = false;
  };

  const readLines = async (body, onLine) => {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      pending += decoder.decode(value, { stream: true });
      let newline = pending.indexOf("\n");
      while (newline !== -1) {
        const line = pending.slice(0, newline).trim();
        pending = pending.slice(newline + 1);
        if (line) onLine(JSON.parse(line));
        newline = pending.indexOf("\n");
      }
    }
    pending += decoder.decode();
    if (pending.trim()) onLine(JSON.parse(pending.trim()));
  };

  // Streams one turn into `reply`. Resolves once the server has saved the reply or paused for
  // the player; the page then reloads so it shows the server's state. `attempt.started` is set
  // once the server accepts the message, after which a failure must reload rather than restore
  // the draft, because the message is already saved.
  const playTurn = async (url, data, reply, attempt) => {
    const response = await fetch(url, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { Accept: "application/x-ndjson" },
    });
    const isStream = (response.headers.get("content-type") || "").includes("ndjson");
    if (!response.ok || !isStream) {
      throw new TurnFailure("Could not send your message. Reload the page and try again.");
    }
    attempt.started = true;
    let finished = false;
    // The bubble the reply streams into. An out-of-character reply can be followed by the story.
    let current = reply;
    await readLines(response.body, (item) => {
      switch (item.type) {
        case "status":
          current.statusText.textContent = item.text;
          current.status.hidden = false;
          scrollDown();
          break;
        case "log":
          appendLog(item.text, item.hidden, current.bubble);
          refreshMap();
          break;
        case "delta":
          current.status.hidden = true;
          current.content.textContent += item.text;
          scrollDown();
          break;
        case "next_message":
          current = addBubble("assistant", "Cygnus");
          break;
        case "saved":
        case "prompt":
          finished = true;
          break;
        case "error":
          throw new TurnFailure(item.message);
        default:
          break;
      }
    });
    if (!finished) throw new Error("connection ended early");
  };

  const reload = () => {
    window.location.assign(window.location.pathname);
  };

  // Reloading drops focus, so the reloaded page focuses the message box again.
  const reloadKeepingComposerFocus = () => {
    sessionStorage.setItem(FOCUS_KEY, "1");
    reload();
  };

  // While a reply streams, ask the server whether the turn is still running. A stream can die
  // without ending, or fall silent behind a stalling provider, and the page would then sit on a
  // disabled composer while the turn is long finished. The poll notices and reloads, which is
  // how every other turn ends. The first check waits, so a turn that has not been recorded yet
  // is never mistaken for a finished one.
  const STATUS_POLL_MS = 5000;
  const STATUS_FIRST_MS = 10000;
  const statusUrl = document.querySelector("[data-status-url]")?.dataset.statusUrl;
  const watchTurn = () => {
    if (!statusUrl) return;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (response.ok) {
          const { status } = await response.json();
          if (status !== "busy") {
            stopped = true;
            reload();
            return;
          }
        }
      } catch {
        // A failed poll is not fatal: the stream may still be alive, so try again.
      }
      setTimeout(tick, STATUS_POLL_MS);
    };
    setTimeout(tick, STATUS_FIRST_MS);
  };

  // Redraw the map while the GM works, so an encounter being built appears as it is built: the
  // board, the joining sides, initiative, and every placement. Redraws are throttled, and the
  // reload when the turn ends replaces the panel with the server's final state anyway.
  const MAP_REFRESH_MS = 2500;
  const mapUrl = document.querySelector("[data-map-url]")?.dataset.mapUrl;
  let mapTimer = null;
  const refreshMap = () => {
    if (!mapUrl || mapTimer) return;
    mapTimer = setTimeout(async () => {
      mapTimer = null;
      try {
        const response = await fetch(mapUrl, {
          credentials: "same-origin",
          headers: { Accept: "text/html" },
        });
        if (!response.ok) return;
        const template = document.createElement("template");
        template.innerHTML = (await response.text()).trim();
        const fresh = template.content.querySelector(".map-panel");
        const current = document.querySelector(".map-panel");
        if (!fresh || !current) return;
        const selected = current.querySelector('[role="tab"][aria-selected="true"]')?.id || "";
        current.replaceWith(fresh);
        fresh
          .closest(".side")
          ?.classList.toggle("in-encounter", Boolean(fresh.querySelector("#panel-encounter")));
        window.initWorkspaceTabs?.();
        if (selected) window.workspaceSelectTab?.(selected);
      } catch {
        // A failed redraw is harmless: the next one, or the reload at the end, catches up.
      }
    }, MAP_REFRESH_MS);
  };

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text) return;

    const draft = textarea.value;
    const data = new FormData(composer);
    errorBox.hidden = true;
    textarea.value = "";
    setBusy(true);
    watchTurn();
    // Disabling the Send button drops focus, so move it back to the message box.
    textarea.focus();

    const player = addBubble("user", `${composer.dataset.playerName} (you)`);
    player.content.textContent = text;
    const reply = addBubble("assistant", "Cygnus");
    const attempt = { started: false };

    try {
      await playTurn(composer.dataset.streamAction, data, reply, attempt);
      reloadKeepingComposerFocus();
    } catch (error) {
      if (attempt.started) {
        reloadKeepingComposerFocus();
        return;
      }
      player.bubble.remove();
      reply.bubble.remove();
      textarea.value = draft;
      showError(error instanceof TurnFailure ? error.message : NOT_SAVED);
      setBusy(false);
      textarea.focus();
    }
  });

  // Answers to the GM's prompt. The answer is stored before the reply continues, so a failure
  // leaves the server's state correct; reloading shows that state with a Continue button.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.hasAttribute("data-pending-action") && !form.hasAttribute("data-encounter-turn")) return;
    event.preventDefault();
    const data = new FormData(form);
    const kind = data.get("kind");
    const answer = String(data.get("content") || "").trim();
    if (kind === "text" && !answer) return;

    // Encounter turn forms have no pending block, so their own controls are disabled instead.
    const controls = (form.closest("[data-pending]") ?? form).querySelectorAll("button, textarea");
    for (const control of controls) control.disabled = true;
    setGmState("busy");
    watchTurn();
    const reply = addBubble("assistant", "Cygnus");
    const attempt = { started: false };
    try {
      await playTurn(form.action, data, reply, attempt);
      reload();
    } catch (error) {
      // An opening that could not start stays on the page with its Continue button, rather than
      // reloading into another automatic start.
      if (form.hasAttribute("data-auto-start") && !attempt.started) {
        reply.bubble.remove();
        showError(error instanceof TurnFailure ? error.message : "The GM's reply did not finish. Try again.");
        for (const control of controls) control.disabled = false;
        setGmState("waiting");
        return;
      }
      // Once the stream started, the server has recorded the failure and the reloaded page shows
      // it, so only a failure before the stream needs a flash message.
      if (!attempt.started) {
        sessionStorage.setItem(
          FLASH_KEY,
          error instanceof TurnFailure ? error.message : "The GM's reply did not finish. Try again.",
        );
      }
      reload();
    }
  });

  if (sessionStorage.getItem(FOCUS_KEY)) {
    sessionStorage.removeItem(FOCUS_KEY);
    // A pending prompt disables the message box, so there is nothing to focus.
    if (!textarea.disabled) textarea.focus();
  }

  const flashed = sessionStorage.getItem(FLASH_KEY);
  if (flashed) {
    sessionStorage.removeItem(FLASH_KEY);
    showError(flashed);
  }

  // The campaign's opening starts on its own once the character exists: the player waits for it.
  const autoStart = document.querySelector("form[data-auto-start]");
  if (autoStart) autoStart.requestSubmit();
})();
