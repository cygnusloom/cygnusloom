(() => {
  // Background music is a player preference, not campaign state. The GM only supplies a mood;
  // this file picks a licensed track, crossfades it, and remembers the player's mute and volume.
  const root = document.querySelector('[data-view="play"]');
  if (!root) return;

  const manifestUrl = root.dataset.musicManifest || "/static/music/manifest.json";
  const ENABLED_KEY = "cygnus.music.enabled";
  const VOLUME_KEY = "cygnus.music.volume";
  const DEFAULT_VOLUME = 0.35;
  const FADE_MS = 2500;
  const RECENT_LIMIT = 3;
  const FALLBACKS = {
    calm: ["exploration", "social"],
    exploration: ["calm", "social"],
    tension: ["combat", "mystery"],
    combat: ["tension", "horror"],
    victory: ["exploration", "calm"],
    sorrow: ["calm", "mystery"],
    mystery: ["horror", "tension"],
    horror: ["mystery", "tension"],
    social: ["calm", "exploration"],
    silence: [],
  };

  const readBool = (key, fallback) => {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : value === "1";
    } catch {
      return fallback;
    }
  };
  const readNumber = (key, fallback) => {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null || raw === "") return fallback;
      const value = Number(raw);
      return Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : fallback;
    } catch {
      return fallback;
    }
  };
  const store = (key, value) => {
    try {
      localStorage.setItem(key, String(value));
    } catch {
      // A private window may refuse storage; the preference still works for this page.
    }
  };

  // An automated test server mutes music for a fresh profile; an explicit player choice wins.
  let userEnabled = readBool(ENABLED_KEY, root.dataset.musicDefaultEnabled !== "0");
  let volume = readNumber(VOLUME_KEY, DEFAULT_VOLUME);
  let mood = root.dataset.musicMood || "silence";
  let source = root.dataset.musicSource || "auto";
  let manifest = null;
  let manifestPromise = null;
  let current = null;
  let nowPlaying = null;
  let pendingGesture = false;
  let gestureSeen = false;
  let starting = false;
  let refusals = 0;
  let fadeToken = 0;
  let recent = [];
  const audios = new Set();

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const normalise = (value) => {
    if (typeof value !== "string") return "auto";
    return value === "gm" || value === "auto" ? value : "auto";
  };
  const moodLabel = (value) => {
    const text = typeof value === "string" && value ? value : "exploration";
    return text.charAt(0).toUpperCase() + text.slice(1);
  };
  const trackUrl = (track) => {
    const file = String(track.file || "");
    return file.startsWith("/") ? file : `/static/music/${encodeURIComponent(file)}`;
  };
  const trackFits = (track, nextMood) =>
    Array.isArray(track.moods) && track.moods.includes(nextMood);

  // The widget shows what is playing under its controls. The text scrolls when it does not fit;
  // the second copy in the markup makes the loop seamless.
  const measureTicker = (ticker) => {
    const track = ticker.querySelector(".music-ticker-track");
    const item = track?.firstElementChild;
    const width = item ? item.getBoundingClientRect().width : 0;
    const overflows = width > ticker.clientWidth + 1;
    ticker.dataset.scrolling = overflows ? "true" : "false";
    if (overflows) {
      const seconds = Math.max(8, Math.round((width / 25) * 10) / 10);
      ticker.style.setProperty("--music-ticker-duration", `${seconds}s`);
    }
  };
  const syncTicker = () => {
    const text = userEnabled && nowPlaying ? `${nowPlaying.title} · ${nowPlaying.artist}` : "";
    for (const ticker of document.querySelectorAll("[data-music-ticker]")) {
      if (ticker.dataset.text !== text) {
        ticker.dataset.text = text;
        for (const item of ticker.querySelectorAll("[data-music-ticker-text]")) {
          item.textContent = text;
        }
        ticker.hidden = !text;
      }
      if (text) measureTicker(ticker);
    }
  };
  // Widths change with the window and the panel, so re-measure instead of trusting the first pass.
  let tickerFrame = 0;
  const remeasureTicker = () => {
    cancelAnimationFrame(tickerFrame);
    tickerFrame = requestAnimationFrame(syncTicker);
  };
  window.addEventListener("resize", remeasureTicker);
  document.fonts?.ready.then(() => syncTicker());

  const syncControls = () => {
    const playing = userEnabled && !pendingGesture;
    const moodText = moodLabel(mood);
    for (const label of document.querySelectorAll("[data-music-mood-label]")) {
      label.textContent = moodText;
    }
    for (const button of document.querySelectorAll("[data-music-toggle]")) {
      button.setAttribute("aria-pressed", String(playing));
      const label = playing
        ? nowPlaying
          ? `Mute music - ${nowPlaying.title} by ${nowPlaying.artist}`
          : "Mute music"
        : "Play music";
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    for (const button of document.querySelectorAll("[data-music-menu-toggle]")) {
      const label = nowPlaying
        ? `Music - ${moodText} - ${nowPlaying.title} by ${nowPlaying.artist}`
        : `Music - ${moodText}`;
      button.setAttribute("aria-label", label);
      button.title = label;
    }
    for (const button of document.querySelectorAll("[data-music-skip]")) {
      button.disabled = !userEnabled || mood === "silence";
    }
    for (const input of document.querySelectorAll("[data-music-volume]")) {
      input.value = String(Math.round(volume * 100));
    }
    for (const checkbox of document.querySelectorAll("[data-music-enabled]")) {
      checkbox.checked = userEnabled;
    }
    syncTicker();
  };

  const cleanup = (audio) => {
    if (!audio) return;
    // Stopping is ours, so the autoplay watchdog must ignore the pause that follows.
    audio.cygnusPausing = true;
    try {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    } catch {
      // The element may already be gone.
    }
    audios.delete(audio);
  };

  // Music belongs to the player in front of the window, so it drops to silence while the window
  // is in the background and comes back when they return. The desktop edition reports its own
  // window activation through setWindowActive, because QtWebEngine keeps the page focused when
  // its window is not.
  let pageVisible = !document.hidden;
  let windowActive = true;
  const syncAudible = () => {
    const audible = pageVisible && windowActive && !pendingGesture;
    for (const audio of audios) audio.muted = !audible;
  };
  const syncPageVisible = () => {
    pageVisible = !document.hidden;
    syncAudible();
  };
  document.addEventListener("visibilitychange", syncPageVisible);
  window.addEventListener("focus", syncPageVisible);
  window.addEventListener("blur", () => {
    pageVisible = false;
    syncAudible();
  });

  // Some browsers turn autoplay down by refusing play(), and some start the track and pause it
  // again. Either way the chosen track is kept running silently, so the player's next gesture
  // only has to unmute it.
  const keepSilent = (audio, track) => {
    if (!userEnabled || mood === "silence") {
      cleanup(audio);
      syncControls();
      return;
    }
    pendingGesture = true;
    audio.volume = volume;
    audio.muted = true;
    Promise.resolve(audio.play())
      .then(() => {
        if (!audios.has(audio)) return;
        if (current && current.audio !== audio && !current.audio.paused) {
          // A newer attempt is already playing, so this one is only a silent spare.
          cleanup(audio);
          return;
        }
        current = { audio, track };
        nowPlaying = track;
        syncControls();
      })
      .catch(() => {
        cleanup(audio);
        if (current?.audio === audio) {
          current = null;
          nowPlaying = null;
        }
        syncControls();
      });
  };

  const stop = () => {
    fadeToken += 1;
    for (const audio of audios) cleanup(audio);
    audios.clear();
    current = null;
    nowPlaying = null;
    pendingGesture = false;
    starting = false;
    refusals = 0;
    syncControls();
  };

  const loadManifest = () => {
    if (manifest) return Promise.resolve(manifest);
    if (!manifestPromise) {
      manifestPromise = fetch(manifestUrl, { credentials: "same-origin" })
        .then((response) => {
          if (!response.ok) throw new Error(`music manifest ${response.status}`);
          return response.json();
        })
        .then((data) => {
          if (!data || !Array.isArray(data.tracks) || !data.tracks.length) {
            throw new Error("music manifest has no tracks");
          }
          manifest = data;
          return data;
        })
        .catch((error) => {
          console.warn("Cygnus Loom could not load the music pack.", error);
          manifestPromise = null;
          return null;
        });
    }
    return manifestPromise;
  };

  const pickTrack = (nextMood) => {
    if (!manifest) return null;
    const byMood = (wanted) => manifest.tracks.filter((track) => track.moods.includes(wanted));
    let pool = byMood(nextMood);
    for (const fallback of FALLBACKS[nextMood] || []) {
      if (pool.length) break;
      pool = byMood(fallback);
    }
    if (!pool.length) return null;
    const fresh = pool.filter(
      (track) => track.id !== current?.track.id && !recent.includes(track.id),
    );
    const choices = fresh.length ? fresh : pool;
    const chosen = choices[Math.floor(Math.random() * choices.length)] || choices[0];
    if (chosen) {
      recent = [...recent.filter((id) => id !== chosen.id), chosen.id].slice(-RECENT_LIMIT);
    }
    return chosen;
  };

  const fadeTo = (track) => {
    const token = ++fadeToken;
    const next = new Audio(trackUrl(track));
    next.preload = "auto";
    next.loop = Boolean(track.loop);
    next.volume = 0;
    audios.add(next);
    syncAudible();
    const previous = current;
    const previousLevel = previous ? previous.audio.volume : 0;
    const started = performance.now();

    // A browser that refuses audible playback may accept play() and then pause the track a
    // moment later without rejecting anything, so an immediate pause means "not allowed".
    next.addEventListener("pause", () => {
      if (next.cygnusPausing || next.ended || next.error) return;
      if (current?.audio !== next || next.currentTime > 0.5) return;
      if (!userEnabled || mood === "silence" || pendingGesture) return;
      keepSilent(next, track);
      syncControls();
    });

    Promise.resolve(next.play())
      .then(() => {
        if (token !== fadeToken) {
          cleanup(next);
          return;
        }
        if (next.paused) {
          keepSilent(next, track);
          syncControls();
          return;
        }
        refusals = 0;
        pendingGesture = false;
        current = { audio: next, track };
        nowPlaying = track;
        syncControls();
        const step = (now) => {
          if (token !== fadeToken) return;
          const progress = clamp((now - started) / FADE_MS, 0, 1);
          next.volume = volume * progress;
          if (previous) previous.audio.volume = previousLevel * (1 - progress);
          if (progress < 1) {
            requestAnimationFrame(step);
          } else if (previous) {
            cleanup(previous.audio);
          }
        };
        requestAnimationFrame(step);
      })
      .catch(() => {
        if (token !== fadeToken) return;
        if (previous && !previous.audio.paused) {
          cleanup(next);
          current = previous;
          pendingGesture = false;
          syncControls();
          syncAudible();
          return;
        }
        refusals += 1;
        if (gestureSeen && refusals === 1) {
          // The page has been interacted with since this attempt began, so try audibly again.
          unlock();
          return;
        }
        keepSilent(next, track);
        syncControls();
      });

    next.addEventListener("ended", () => {
      if (current?.audio !== next || next.loop) return;
      const following = pickTrack(mood);
      if (following) fadeTo(following);
    });
  };

  const start = () => {
    if (pendingGesture || starting) return;
    if (!userEnabled || mood === "silence") {
      stop();
      return;
    }
    if (current && trackFits(current.track, mood) && !current.audio.paused) {
      syncAudible();
      syncControls();
      return;
    }
    // Start in the caller's gesture when the pack is already loaded: some browsers only allow
    // audible playback from the event handler itself.
    if (manifest) {
      const track = pickTrack(mood);
      if (track) fadeTo(track);
      return;
    }
    starting = true;
    loadManifest().then((data) => {
      starting = false;
      if (!data || !userEnabled || mood === "silence") return;
      const track = pickTrack(mood);
      if (track) fadeTo(track);
    });
  };

  const skip = () => {
    if (!userEnabled || mood === "silence") return;
    const advance = () => {
      if (!userEnabled || mood === "silence") return;
      const following = pickTrack(mood);
      if (following) fadeTo(following);
    };
    if (manifest) advance();
    else loadManifest().then((data) => {
      if (data) advance();
    });
  };

  const applyState = (nextMood, nextSource) => {
    const moodChanged = nextMood !== mood;
    mood = typeof nextMood === "string" && nextMood ? nextMood : "exploration";
    source = normalise(nextSource);
    syncControls();
    if (!userEnabled || mood === "silence") {
      stop();
      return;
    }
    if (current && !moodChanged && trackFits(current.track, mood) && !current.audio.paused) {
      syncControls();
      return;
    }
    start();
  };

  // Browsers turn down audible autoplay until the page has seen a real user gesture, which a
  // fresh origin - a new worktree port, a private window - has not had yet. Every click, tap,
  // and key press retries, whether or not the browser reported the refusal.
  const unlock = () => {
    // A real gesture proves the player is in front of the window even if focus events lie.
    if (!document.hidden) pageVisible = true;
    pendingGesture = false;
    start();
    syncAudible();
  };

  const toggle = () => {
    if (pendingGesture) {
      pendingGesture = false;
      userEnabled = true;
      store(ENABLED_KEY, "1");
      start();
      return;
    }
    userEnabled = !userEnabled;
    store(ENABLED_KEY, userEnabled ? "1" : "0");
    if (userEnabled) start();
    else stop();
  };

  const setEnabled = (enabled) => {
    userEnabled = Boolean(enabled);
    store(ENABLED_KEY, userEnabled ? "1" : "0");
    if (userEnabled) {
      pendingGesture = false;
      start();
    } else {
      stop();
    }
  };

  const setVolume = (value) => {
    volume = clamp(Number(value) / 100, 0, 1);
    store(VOLUME_KEY, volume);
    if (current) current.audio.volume = volume;
    syncControls();
  };

  const onGesture = (event) => {
    // The music controls handle their own gestures; anything else on the page just unlocks sound.
    if (event.target.closest?.("[data-music-toggle], [data-music-enabled], [data-music-skip]")) {
      return;
    }
    gestureSeen = true;
    unlock();
  };
  for (const type of ["pointerdown", "keydown", "touchstart"]) {
    document.addEventListener(type, onGesture, { capture: true, passive: true });
  }

  // Phones fold the widget into one small button that opens the whole panel.
  const musicWidget = document.querySelector("[data-music-widget]");
  const setMusicMenu = (open) => {
    if (!musicWidget) return;
    musicWidget.dataset.musicMenuOpen = open ? "true" : "false";
    for (const button of document.querySelectorAll("[data-music-menu-toggle]")) {
      button.setAttribute("aria-expanded", String(open));
    }
    // The panel has no width while it is closed, so measure the ticker again once it opens.
    if (open) syncTicker();
  };
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-music-menu-toggle]")) return;
    event.preventDefault();
    setMusicMenu(musicWidget?.dataset.musicMenuOpen !== "true");
  });
  document.addEventListener("pointerdown", (event) => {
    if (musicWidget?.dataset.musicMenuOpen !== "true") return;
    if (event.target.closest?.("[data-music-widget]")) return;
    setMusicMenu(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setMusicMenu(false);
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-music-toggle]");
    if (!button) return;
    event.preventDefault();
    toggle();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-music-skip]");
    if (!button || button.disabled) return;
    event.preventDefault();
    skip();
  });
  document.addEventListener("input", (event) => {
    const input = event.target.closest("[data-music-volume]");
    if (input) setVolume(input.value);
  });
  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest("[data-music-enabled]");
    if (checkbox) setEnabled(checkbox.checked);
  });

  window.applyMusicState = applyState;
  window.syncMusicControls = syncControls;
  window.setWindowActive = (active) => {
    windowActive = Boolean(active);
    syncAudible();
  };
  window.initMusic = () => {
    syncControls();
    start();
  };

  syncControls();
  start();
})();
