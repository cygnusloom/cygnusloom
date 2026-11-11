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

  let userEnabled = readBool(ENABLED_KEY, true);
  let volume = readNumber(VOLUME_KEY, DEFAULT_VOLUME);
  let mood = root.dataset.musicMood || "silence";
  let intensity = Number(root.dataset.musicIntensity || 1);
  let source = root.dataset.musicSource || "auto";
  let manifest = null;
  let manifestPromise = null;
  let current = null;
  let nowPlaying = null;
  let pendingGesture = false;
  let fadeToken = 0;
  let recent = [];
  const audios = new Set();

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const normalise = (value) => {
    if (typeof value !== "string") return "auto";
    return value === "gm" || value === "auto" ? value : "auto";
  };
  const trackUrl = (track) => {
    const file = String(track.file || "");
    return file.startsWith("/") ? file : `/static/music/${encodeURIComponent(file)}`;
  };
  const trackFits = (track, nextMood, nextIntensity) =>
    Array.isArray(track.moods) &&
    track.moods.includes(nextMood) &&
    Array.isArray(track.intensity) &&
    nextIntensity >= track.intensity[0] &&
    nextIntensity <= track.intensity[1];

  const syncControls = () => {
    const playing = userEnabled && !pendingGesture;
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
    for (const input of document.querySelectorAll("[data-music-volume]")) {
      input.value = String(Math.round(volume * 100));
    }
    for (const checkbox of document.querySelectorAll("[data-music-enabled]")) {
      checkbox.checked = userEnabled;
    }
  };

  const cleanup = (audio) => {
    if (!audio) return;
    try {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    } catch {
      // The element may already be gone.
    }
    audios.delete(audio);
  };

  const stop = () => {
    fadeToken += 1;
    for (const audio of audios) cleanup(audio);
    audios.clear();
    current = null;
    nowPlaying = null;
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

  const pickTrack = (nextMood, nextIntensity) => {
    if (!manifest) return null;
    const byMood = (wanted) => manifest.tracks.filter((track) => track.moods.includes(wanted));
    let pool = byMood(nextMood).filter(
      (track) => nextIntensity >= track.intensity[0] && nextIntensity <= track.intensity[1],
    );
    if (!pool.length) pool = byMood(nextMood);
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
    const previous = current;
    const previousLevel = previous ? previous.audio.volume : 0;
    const started = performance.now();

    Promise.resolve(next.play())
      .then(() => {
        if (token !== fadeToken) {
          cleanup(next);
          return;
        }
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
        cleanup(next);
        if (previous && !previous.audio.paused) {
          current = previous;
          pendingGesture = false;
        } else {
          pendingGesture = true;
        }
        syncControls();
      });

    next.addEventListener("ended", () => {
      if (current?.audio !== next || next.loop) return;
      const following = pickTrack(mood, intensity);
      if (following) fadeTo(following);
    });
  };

  const start = () => {
    if (!userEnabled || mood === "silence") {
      stop();
      return;
    }
    if (current && trackFits(current.track, mood, intensity) && !current.audio.paused) {
      syncControls();
      return;
    }
    loadManifest().then((data) => {
      if (!data || !userEnabled || mood === "silence") return;
      const track = pickTrack(mood, intensity);
      if (track) fadeTo(track);
    });
  };

  const applyState = (nextMood, nextIntensity, nextSource) => {
    const moodChanged = nextMood !== mood;
    const intensityChanged = nextIntensity !== intensity;
    mood = typeof nextMood === "string" && nextMood ? nextMood : "exploration";
    intensity = clamp(Number(nextIntensity) || 1, 1, 3);
    source = normalise(nextSource);
    if (!userEnabled || mood === "silence") {
      stop();
      return;
    }
    if (
      current &&
      !moodChanged &&
      !intensityChanged &&
      trackFits(current.track, mood, intensity) &&
      !current.audio.paused
    ) {
      syncControls();
      return;
    }
    start();
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

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-music-toggle]");
    if (!button) return;
    event.preventDefault();
    toggle();
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
  window.initMusic = () => {
    syncControls();
    start();
  };

  syncControls();
  start();
})();
