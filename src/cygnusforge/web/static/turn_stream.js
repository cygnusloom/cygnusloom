(() => {
  const composer = document.getElementById("composer");
  if (!composer || !composer.dataset.streamAction || !window.ReadableStream) return;

  const transcript = document.querySelector("[data-transcript]");
  const errorBox = document.querySelector(".turn-error");
  const textarea = composer.querySelector("textarea");
  const submit = composer.querySelector('button[type="submit"]');
  const NOT_SAVED = "The reply was not saved. Your message is still in the box.";

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

  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = textarea.value.trim();
    if (!text) return;

    const draft = textarea.value;
    const data = new FormData(composer);
    const model = data.get("model") || "";
    errorBox.hidden = true;
    textarea.value = "";
    setBusy(true);

    const player = addBubble("user", "user");
    player.content.textContent = text;
    const reply = addBubble("assistant", "Cygnus");
    const showStatus = (message) => {
      reply.statusText.textContent = message;
      reply.status.hidden = false;
      scrollDown();
    };

    let saved = false;
    const handle = (item) => {
      switch (item.type) {
        case "status":
          showStatus(item.text);
          break;
        case "delta":
          reply.status.hidden = true;
          reply.content.textContent += item.text;
          scrollDown();
          break;
        case "saved":
          saved = true;
          break;
        case "error":
          throw new TurnFailure(item.message);
        default:
          break;
      }
    };

    try {
      const response = await fetch(composer.dataset.streamAction, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: { Accept: "application/x-ndjson" },
      });
      const isStream = (response.headers.get("content-type") || "").includes("ndjson");
      if (!response.ok || !isStream) {
        throw new TurnFailure("Could not send your message. Reload the page and try again.");
      }
      await readLines(response.body, handle);
      if (!saved) throw new Error("connection ended early");
      window.location.assign(`${window.location.pathname}?${new URLSearchParams({ model })}`);
    } catch (error) {
      player.bubble.remove();
      reply.bubble.remove();
      textarea.value = draft;
      showError(error instanceof TurnFailure ? error.message : NOT_SAVED);
      setBusy(false);
      textarea.focus();
    }
  });
})();
