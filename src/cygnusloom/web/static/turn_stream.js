(() => {
  // Forms marked data-confirm (rollback) ask before submitting. This runs even without streaming.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    // Ending a turn can start the next player turn at once, so a second click must not end that
    // new turn. Let the submission start, then disable the button.
    const submitter = event.submitter;
    if (form.hasAttribute("data-once") && submitter) {
      window.setTimeout(() => {
        submitter.disabled = true;
      }, 0);
    }
  });

  const transcript = document.querySelector("[data-transcript]");

  // The transcript follows new content only while the reader is at the bottom. Scrolling up
  // leaves their place alone and reveals the jump button.
  const jumpButton = document.querySelector("[data-jump-to-bottom]");
  const NEAR_BOTTOM_PX = 32;
  let followBottom = true;
  const atBottom = () =>
    !transcript ||
    transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight <= NEAR_BOTTOM_PX;
  const updateJump = () => {
    if (jumpButton) jumpButton.hidden = atBottom();
  };
  const syncFollow = () => {
    followBottom = atBottom();
    updateJump();
  };
  transcript?.addEventListener("scroll", syncFollow, { passive: true });
  window.addEventListener("resize", updateJump);
  jumpButton?.addEventListener("click", () => {
    followBottom = true;
    if (transcript) transcript.scrollTop = transcript.scrollHeight;
    updateJump();
  });
  syncFollow();

  const composer = document.getElementById("composer");
  if (!composer || !composer.dataset.streamAction || !window.ReadableStream) return;

  const errorBox = document.querySelector(".turn-error");
  const textarea = composer.querySelector("textarea");
  const submit = composer.querySelector('button[type="submit"]');
  const liveUrl = document.querySelector("[data-live-url]")?.dataset.liveUrl;
  const NOT_SAVED = "The reply was not saved. Your message is still in the box.";
  const FLASH_KEY = "cygnus-turn-error";
  const FOCUS_KEY = "cygnus-focus-composer";
  // True while this page is streaming its own turn, so the background live check leaves it alone.
  let localTurn = false;

  class TurnFailure extends Error {}

  // A reply streams as plain deltas but is shown as the markdown the server saves. Paints are
  // batched to one per frame: a fast model sends many deltas, and rendering each one would
  // reflow the transcript just as often. The fallback still escapes the text it shows.
  const renderMarkdown = (text) =>
    window.renderMarkdown
      ? window.renderMarkdown(text)
      : String(text)
          .replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`)
          .replace(/\n/g, "<br>");
  const paint = (target) => {
    if (target.frame) return;
    target.frame = requestAnimationFrame(() => {
      target.frame = 0;
      if (target.content.dataset.raw === target.text) return;
      target.content.dataset.raw = target.text;
      target.content.innerHTML = renderMarkdown(target.text);
      scrollDown();
    });
  };

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
    return { bubble, content, status, statusText, text: "", frame: 0 };
  };

  // Keep the reader at the bottom, unless they scrolled up to read something earlier.
  const scrollDown = () => {
    if (followBottom) transcript.scrollTop = transcript.scrollHeight;
    updateJump();
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

  // "X is typing" hints. A hint is sent at most every couple of seconds while the box holds
  // text, and expires on the server when the player stops. Other players' hints arrive in the
  // live snapshots, so this page never polls for them.
  const typingNote = document.querySelector("[data-typing-note]");
  const viewerId = Number(composer.dataset.viewerId || 0);
  const TYPING_EVERY_MS = 2000;
  let typingSentAt = 0;
  let typingShown = false;
  const sendTyping = (active) => {
    const url = composer.dataset.typingUrl;
    if (!url) return;
    const token = composer.querySelector('input[name="csrf_token"]');
    const data = new FormData();
    data.set("csrf_token", token ? token.value : "");
    data.set("active", active ? "1" : "0");
    data.set("name", composer.dataset.playerName || "");
    fetch(url, { method: "POST", body: data, credentials: "same-origin", keepalive: true }).catch(
      () => {
        // A missed hint is harmless; the next keystroke sends another one.
      },
    );
  };
  const stopTyping = () => {
    typingSentAt = 0;
    if (!typingShown) return;
    typingShown = false;
    sendTyping(false);
  };
  const noteTyping = () => {
    if (textarea.disabled || textarea.readOnly) return;
    if (!textarea.value.trim()) {
      stopTyping();
      return;
    }
    const now = Date.now();
    if (typingShown && now - typingSentAt < TYPING_EVERY_MS) return;
    typingSentAt = now;
    typingShown = true;
    sendTyping(true);
  };
  const applyTyping = (typers) => {
    if (!typingNote) return;
    const selfId = Number(composer.dataset.viewerId || viewerId);
    const names = (Array.isArray(typers) ? typers : [])
      .filter((typer) => Number(typer?.id) !== selfId && typer?.name)
      .map((typer) => String(typer.name));
    if (!names.length) {
      typingNote.hidden = true;
      typingNote.textContent = "";
      return;
    }
    const text =
      names.length === 1
        ? `${names[0]} is typing…`
        : names.length === 2
          ? `${names[0]} and ${names[1]} are typing…`
          : `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]} are typing…`;
    typingNote.textContent = text;
    typingNote.hidden = false;
  };
  textarea.addEventListener("input", noteTyping);
  window.addEventListener("pagehide", stopTyping);

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
          scrollDown();
          break;
        case "delta":
          current.status.hidden = true;
          current.text += item.text;
          paint(current);
          break;
        case "retract":
          // A retracted narration is written again in the same message, so clear both the
          // raw text and any paint still waiting for the next frame.
          if (current.frame) {
            cancelAnimationFrame(current.frame);
            current.frame = 0;
          }
          current.text = "";
          delete current.content.dataset.raw;
          current.content.textContent = "";
          current.status.hidden = false;
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
    // Keep the hash, such as #view-settings, so the reader stays on the view they were using.
    window.location.assign(window.location.href);
  };

  const rememberFocus = () => sessionStorage.setItem(FOCUS_KEY, "1");

  // Replace the views with the server's current state instead of navigating. The composer and
  // transcript elements stay in place, so their handlers and the streaming helpers keep working
  // and the player keeps their scroll position and focus.
  const applyLive = (html) => {
    const template = document.createElement("template");
    template.innerHTML = html.trim();
    const freshPlay = template.content.querySelector('[data-view="play"]');
    const currentPlay = document.querySelector('[data-view="play"]');
    const freshTranscript = freshPlay?.querySelector("[data-transcript]");
    if (!freshPlay || !currentPlay || !freshTranscript) return false;

    const selectedTabs = Array.from(
      document.querySelectorAll('[role="tab"][aria-selected="true"]'),
      (tab) => tab.id,
    );
    const scrolls = new Map();
    for (const element of document.querySelectorAll(".views [id]")) {
      if (element.scrollTop) scrolls.set(element.id, element.scrollTop);
    }
    const wasAtBottom = atBottom();
    const transcriptTop = transcript.scrollTop;

    transcript.replaceChildren(
      ...Array.from(freshTranscript.childNodes, (node) => node.cloneNode(true)),
    );
    transcript.dataset.gmState = freshTranscript.dataset.gmState;
    transcript.dataset.lastEvent = freshTranscript.dataset.lastEvent;
    transcript.dataset.turnStatus = freshTranscript.dataset.turnStatus;
    transcript.dataset.showThinking = freshTranscript.dataset.showThinking;
    // The server renders the viewer's thinking preference; keep the class across the swap.
    transcript.classList.toggle(
      "show-hidden",
      freshTranscript.classList.contains("show-hidden") ||
        transcript.dataset.showThinking === "1",
    );

    // The encounter prompt, the advancement pause, and the waiting note sit between the chat
    // and the composer.
    for (const selector of [".encounter-turn", ".advancement-pause", ".turn-note"]) {
      currentPlay.querySelector(selector)?.remove();
      const fresh = freshPlay.querySelector(selector);
      // The typing note stays in place, so the fresh waiting note goes above it.
      if (fresh) (selector === ".turn-note" && typingNote ? typingNote : composer).before(fresh);
    }

    const currentSide = currentPlay.querySelector(".side");
    const freshSide = freshPlay.querySelector(".side");
    if (currentSide && freshSide) {
      currentSide.innerHTML = freshSide.innerHTML;
      currentSide.classList.toggle(
        "in-encounter",
        freshSide.classList.contains("in-encounter"),
      );
    }

    const freshComposer = freshPlay.querySelector("#composer");
    if (freshComposer) {
      composer.action = freshComposer.action;
      composer.dataset.streamAction = freshComposer.dataset.streamAction;
      composer.dataset.playerName = freshComposer.dataset.playerName;
      composer.dataset.blocked = freshComposer.dataset.blocked;
      if (freshComposer.dataset.typingUrl) {
        composer.dataset.typingUrl = freshComposer.dataset.typingUrl;
        composer.dataset.viewerId = freshComposer.dataset.viewerId;
      }
      const freshTextarea = freshComposer.querySelector("textarea");
      if (freshTextarea) {
        textarea.placeholder = freshTextarea.placeholder;
        textarea.disabled = freshTextarea.disabled;
        textarea.readOnly = false;
      }
      const freshSubmit = freshComposer.querySelector('button[type="submit"]');
      if (freshSubmit) submit.disabled = freshSubmit.disabled;
      const tools = composer.querySelector(".composer-tools");
      const freshTools = freshComposer.querySelector(".composer-tools");
      if (tools && freshTools) tools.innerHTML = freshTools.innerHTML;
    }

    // The views outside Play are part of the same campaign state: the journal, roster, and
    // compendium can all change during a turn. Rebuild their contents in place so switching to
    // them after a reply does not show the state from before it. A hidden Settings view is
    // refreshed too, so a setting the GM changed mid-turn is current when the player opens it;
    // a visible one is left alone so unsaved form input is not wiped.
    for (const current of document.querySelectorAll(
      '.views > [data-view]:not([data-view="play"])',
    )) {
      if (current.dataset.view === "settings" && !current.hidden) continue;
      const fresh = template.content.querySelector(`[data-view="${current.dataset.view}"]`);
      if (!fresh) continue;
      current.innerHTML = fresh.innerHTML;
      window.initWorkspace?.(current);
    }
    window.initWorkspace?.(currentPlay);
    window.initMapViews?.(document);
    // The GM's music cue lives on the play view, which applyLive does not replace. Keep the
    // existing workspace's data current, then hand the fresh mood to the player, which keeps
    // the current track when it still fits.
    if (freshPlay.dataset.musicMood) {
      currentPlay.dataset.musicMood = freshPlay.dataset.musicMood;
      currentPlay.dataset.musicSource = freshPlay.dataset.musicSource;
      window.applyMusicState?.(
        freshPlay.dataset.musicMood,
        freshPlay.dataset.musicSource || "auto",
      );
    }
    window.syncMusicControls?.();

    for (const id of selectedTabs) window.workspaceSelectTab?.(id);
    for (const [id, top] of scrolls) {
      const element = document.getElementById(id);
      if (element && element !== transcript) element.scrollTop = top;
    }
    transcript.scrollTop = wasAtBottom ? transcript.scrollHeight : transcriptTop;
    followBottom = wasAtBottom;
    updateJump();
    // BACKWARDS-COMPATIBILITY: 3 - start a player turn saved before turns auto-started.
    window.setTimeout(startPendingPlayerTurn, 0);
    return true;
  };

  let liveRefresh = null;
  let mapTimer = null;
  const refreshLive = async ({ focusComposer = false, message = "" } = {}) => {
    if (!liveUrl) return false;
    try {
      const response = await fetch(liveUrl, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (response.status === 409) {
        // The campaign moved to the setup or death screen, which has no live region. A normal
        // reload shows that screen and stops the stream from asking for a fragment again.
        window.location.reload();
        return false;
      }
      if (!response.ok || response.redirected) return false;
      if (!applyLive(await response.text())) return false;
    } catch {
      return false;
    }
    errorBox.hidden = true;
    if (message) showError(message);
    if (focusComposer && !textarea.disabled) textarea.focus();
    return true;
  };

  // One update at a time: the stream and the status poll can both decide a turn is over.
  const settle = async (options = {}) => {
    if (liveRefresh) return liveRefresh;
    liveRefresh = (async () => {
      if (mapTimer) {
        clearTimeout(mapTimer);
        mapTimer = null;
      }
      if (await refreshLive(options)) return;
      // A page that cannot update in place falls back to the old reload path.
      if (options.focusComposer) rememberFocus();
      if (options.message) sessionStorage.setItem(FLASH_KEY, options.message);
      reload();
    })();
    try {
      await liveRefresh;
    } finally {
      liveRefresh = null;
    }
  };

  // BACKWARDS-COMPATIBILITY: 3 - an old page may still render the Begin form for a player
  // turn. Submit it without a page navigation and then take the fresh state.
  async function startPlayerTurn(form) {
    if (form.dataset.starting) return;
    form.dataset.starting = "1";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
      });
      if (!response.ok && !response.redirected) {
        form.hidden = false;
        return;
      }
    } catch {
      form.hidden = false;
      return;
    } finally {
      delete form.dataset.starting;
    }
    await settle({ focusComposer: true });
  }

  function startPendingPlayerTurn() {
    const form = document.querySelector("form[data-auto-start-turn]");
    if (form) void startPlayerTurn(form);
  }

  // While a reply streams, ask the server whether the turn is still running. A stream can die
  // without ending, or fall silent behind a stalling provider, and the page would then sit on a
  // disabled composer while the turn is long finished. The poll notices and updates the page in
  // place, the same way every other turn ends. The first check waits, so a turn that has not
  // been recorded yet is never mistaken for a finished one.
  const STATUS_POLL_MS = 5000;
  const STATUS_FIRST_MS = 10000;
  const statusUrl = document.querySelector("[data-status-url]")?.dataset.statusUrl;
  let stopWatching = null;
  const watchTurn = () => {
    if (!statusUrl) return;
    stopWatching?.();
    let stopped = false;
    let timer = null;
    stopWatching = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      timer = null;
    };
    const tick = async () => {
      timer = null;
      if (stopped) return;
      try {
        const response = await fetch(statusUrl, {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (response.ok) {
          const { status } = await response.json();
          if (stopped) return;
          if (status !== "busy") {
            stopped = true;
            stopWatching = null;
            settle({ focusComposer: true });
            return;
          }
        }
      } catch {
        // A failed poll is not fatal: the stream may still be alive, so try again.
      }
      if (!stopped) timer = setTimeout(tick, STATUS_POLL_MS);
    };
    timer = setTimeout(tick, STATUS_FIRST_MS);
  };
  const stopWatch = () => {
    stopWatching?.();
    stopWatching = null;
  };

  // Redraw the map while the GM works, so an encounter being built appears as it is built: the
  // board, the joining sides, initiative, and every placement. Redraws are throttled, and the
  // live update when the turn ends replaces the panel with the server's final state anyway.
  const MAP_REFRESH_MS = 2500;
  const mapUrl = document.querySelector("[data-map-url]")?.dataset.mapUrl;
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
        // The roster lives in the Location tab; the map response carries its fresh copy.
        const freshRoster = template.content.querySelector("[data-map-roster]");
        const locationPanel = document.querySelector("#panel-location");
        const currentRoster = locationPanel?.querySelector("[data-map-roster]");
        if (freshRoster) {
          if (currentRoster) currentRoster.replaceWith(freshRoster);
          else locationPanel?.append(freshRoster);
        } else {
          currentRoster?.remove();
        }
        locationPanel?.classList.toggle("with-roster", Boolean(freshRoster));
        window.initWorkspaceTabs?.(fresh);
        if (selected) window.workspaceSelectTab?.(selected);
        window.initMapViews?.(fresh);
      } catch {
        // A failed redraw is harmless: the next one, or the live update at the end, catches up.
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
    stopTyping();
    localTurn = true;
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
      stopWatch();
      await settle({ focusComposer: true });
    } catch (error) {
      stopWatch();
      if (attempt.started) {
        await settle({ focusComposer: true });
        return;
      }
      player.bubble.remove();
      reply.bubble.remove();
      textarea.value = draft;
      showError(error instanceof TurnFailure ? error.message : NOT_SAVED);
      setBusy(false);
      textarea.focus();
    } finally {
      localTurn = false;
    }
  });

  // Answers to the GM's prompt. The answer is stored before the reply continues, so a failure
  // leaves the server's state correct; the live update shows that state with a Continue button.
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (!form.hasAttribute("data-pending-action") && !form.hasAttribute("data-encounter-turn")) return;
    event.preventDefault();
    const data = new FormData(form);
    const kind = data.get("kind");
    const answer = String(data.get("content") || "").trim();
    if ((kind === "text" || kind === "choice") && !answer) return;

    // Encounter turn forms have no pending block, so their own controls are disabled instead.
    const controls = (form.closest("[data-pending]") ?? form).querySelectorAll("button, textarea");
    for (const control of controls) control.disabled = true;
    localTurn = true;
    setGmState("busy");
    watchTurn();
    const reply = addBubble("assistant", "Cygnus");
    const attempt = { started: false };
    try {
      await playTurn(form.action, data, reply, attempt);
      stopWatch();
      await settle({ focusComposer: true });
    } catch (error) {
      stopWatch();
      // An opening that could not start stays on the page with its Continue button, rather than
      // updating into another automatic start.
      if (form.hasAttribute("data-auto-start") && !attempt.started) {
        reply.bubble.remove();
        showError(error instanceof TurnFailure ? error.message : "The GM's reply did not finish. Try again.");
        for (const control of controls) control.disabled = false;
        setGmState("waiting");
        return;
      }
      // Once the stream started, the server has recorded the failure and the live update shows
      // it, so only a failure before the stream needs the message beside the restored state.
      const message =
        attempt.started
          ? ""
          : error instanceof TurnFailure
            ? error.message
            : "The GM's reply did not finish. Try again.";
      await settle({ focusComposer: true, message });
    } finally {
      localTurn = false;
    }
  });

  // Every player's page watches the campaign, not just the page that started the turn. When the
  // newest event or whose turn it is changes, the page reloads its views in place, so a turn by
  // another player appears without a manual refresh. A server-sent event stream carries the
  // change as soon as it is written; a browser without EventSource falls back to checking.
  const GM_WORKING = "The GM is working...";
  const setGmWorking = (text) => {
    const label = document.querySelector("[data-gm-working-text]");
    if (label) label.textContent = text || GM_WORKING;
  };
  const setGmReply = (text) => {
    let content = document.querySelector("[data-gm-reply]");
    if (!text) {
      content?.remove();
      return;
    }
    if (!content) {
      const bubble = document.querySelector("[data-gm-working]");
      if (!bubble) return;
      content = document.createElement("div");
      content.className = "content";
      content.setAttribute("data-gm-reply", "");
      bubble.querySelector(".turn-status")?.before(content);
    }
    if (content.dataset.raw !== text) {
      content.dataset.raw = text;
      content.innerHTML = renderMarkdown(text);
      scrollDown();
    }
  };

  let pendingSnapshot = null;
  const applySnapshot = (latest) => {
    applyTyping(latest.typing);
    if (localTurn) return;
    const status = String(latest.status || "");
    const running = Boolean(latest.running);
    if (running) {
      setGmWorking(String(latest.activity || ""));
      setGmReply(String(latest.reply || ""));
    }
    if (liveRefresh) {
      // Keep the newest state for once the update in flight finishes.
      pendingSnapshot = latest;
      return;
    }
    const eventChanged = Number(latest.event || 0) !== Number(transcript.dataset.lastEvent || 0);
    const statusChanged = status !== (transcript.dataset.turnStatus || "");
    // A running turn must carry its working line, and a finished one must replace it with the
    // stored message or the pending prompt in a fresh view.
    const working = document.querySelector("[data-gm-working]");
    const workingChanged = running ? !working : Boolean(working);
    if (!eventChanged && !statusChanged && !workingChanged) return;
    refreshLive().finally(() => {
      const next = pendingSnapshot;
      pendingSnapshot = null;
      if (next) applySnapshot(next);
    });
  };

  const eventsUrl = document.querySelector("[data-events-url]")?.dataset.eventsUrl;
  if (eventsUrl && window.EventSource) {
    const source = new EventSource(eventsUrl);
    source.addEventListener("message", (event) => {
      try {
        applySnapshot(JSON.parse(event.data));
      } catch {
        // A malformed event is ignored; the next snapshot is a full one.
      }
    });
  } else if (statusUrl) {
    const LIVE_POLL_MS = 2500;
    const pollCampaign = async () => {
      if (!localTurn && !liveRefresh) {
        try {
          const response = await fetch(statusUrl, {
            credentials: "same-origin",
            headers: { Accept: "application/json" },
          });
          if (response.ok) applySnapshot(await response.json());
        } catch {
          // A failed check is harmless; the next one catches up.
        }
      }
      window.setTimeout(pollCampaign, LIVE_POLL_MS);
    };
    window.setTimeout(pollCampaign, LIVE_POLL_MS);
  }

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
  if (autoStart) {
    autoStart.requestSubmit();
  } else {
    // BACKWARDS-COMPATIBILITY: 3 - start an old page's waiting player turn.
    startPendingPlayerTurn();
  }
})();
