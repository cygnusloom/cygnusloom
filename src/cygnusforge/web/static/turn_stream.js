(() => {
  // Forms marked data-confirm (rollback) ask before submitting. This runs even without streaming.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });

  const composer = document.getElementById("composer");
  if (!composer || !composer.dataset.streamAction || !window.ReadableStream) return;

  const transcript = document.querySelector("[data-transcript]");
  const logList = document.querySelector("[data-log]");
  const errorBox = document.querySelector(".turn-error");
  const textarea = composer.querySelector("textarea");
  const submit = composer.querySelector('button[type="submit"]');
  const NOT_SAVED = "The reply was not saved. Your message is still in the box.";
  const FLASH_KEY = "cygnus-turn-error";

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

  const appendLog = (text) => {
    document.querySelector("[data-log-empty]")?.remove();
    const entry = document.createElement("li");
    entry.textContent = text;
    logList.append(entry);
  };

  const setBusy = (busy) => {
    submit.disabled = busy;
    textarea.readOnly = busy;
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
  // the player; the page then reloads so it shows the server's state.
  const playTurn = async (url, data, reply) => {
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
    let finished = false;
    await readLines(response.body, (item) => {
      switch (item.type) {
        case "status":
          reply.statusText.textContent = item.text;
          reply.status.hidden = false;
          scrollDown();
          break;
        case "log":
          appendLog(item.text);
          break;
        case "delta":
          reply.status.hidden = true;
          reply.content.textContent += item.text;
          scrollDown();
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

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text) return;

    const draft = textarea.value;
    const data = new FormData(composer);
    errorBox.hidden = true;
    textarea.value = "";
    setBusy(true);

    const player = addBubble("user", "user");
    player.content.textContent = text;
    const reply = addBubble("assistant", "Cygnus");

    try {
      await playTurn(composer.dataset.streamAction, data, reply);
      reload();
    } catch (error) {
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
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-pending-action")) return;
    event.preventDefault();
    const data = new FormData(form);
    const kind = data.get("kind");
    const answer = String(data.get("content") || "").trim();
    if (kind === "text" && !answer) return;

    for (const control of form.closest("[data-pending]")?.querySelectorAll("button, textarea") ?? []) {
      control.disabled = true;
    }
    const reply = addBubble("assistant", "Cygnus");
    try {
      await playTurn(form.action, data, reply);
      reload();
    } catch (error) {
      sessionStorage.setItem(
        FLASH_KEY,
        error instanceof TurnFailure ? error.message : "The GM's reply did not finish. Try again.",
      );
      reload();
    }
  });

  const flashed = sessionStorage.getItem(FLASH_KEY);
  if (flashed) {
    sessionStorage.removeItem(FLASH_KEY);
    showError(flashed);
  }
})();
