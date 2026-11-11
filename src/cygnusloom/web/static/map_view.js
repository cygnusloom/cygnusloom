(() => {
  const MAX_SCALE = 6;
  const MIN_SCALE = 1;
  const WHEEL_SENSITIVITY = 0.0015;
  // `--zoom` is inherited by labels, markers and tokens, so rewrite it only on a visible step.
  const ZOOM_STEP = 0.01;
  const WHEEL_GESTURE_MS = 140;

  // Each board keeps its own zoom and position; hidden tabs keep their state.
  // `left` and `top` are where the board's top-left corner sits inside its frame.
  const states = new WeakMap();
  const stateOf = (board) => {
    if (!states.has(board)) {
      states.set(board, {
        scale: 1, left: null, top: null,
        frameWidth: 0, frameHeight: 0,
        boardWidth: 0, boardHeight: 0,
        boardLeft: 0, boardTop: 0,
        measured: false,
        renderedScale: null,
        raf: 0,
      });
    }
    return states.get(board);
  };

  // Keep the board covering the frame, with no gap at the edges. A board smaller than the frame is centred.
  const clampAxis = (position, size, frameSize) => {
    if (size <= frameSize) return (frameSize - size) / 2;
    return Math.min(0, Math.max(frameSize - size, position ?? (frameSize - size) / 2));
  };

  // Layout metrics only change with layout, never with the transform, so they are read on attach,
  // pointerdown, ResizeObserver and window resize rather than on every zoom or pan event.
  const measure = (frame, board) => {
    const state = stateOf(board);
    state.frameWidth = frame.clientWidth;
    state.frameHeight = frame.clientHeight;
    if (!state.frameWidth || !state.frameHeight) {
      // Hidden tabs and closed side sheets have no size; measure again when they are shown.
      state.measured = false;
      return;
    }
    state.boardWidth = board.offsetWidth;
    state.boardHeight = board.offsetHeight;
    state.boardLeft = board.offsetLeft;
    state.boardTop = board.offsetTop;
    state.measured = true;
  };

  const attachedFrames = new WeakSet();
  // One shared resize listener: live turns replace map panels, so per-frame window listeners would pile up.
  const refreshers = new WeakMap();
  window.addEventListener("resize", () => {
    document.querySelectorAll(".map-frame").forEach((frame) => refreshers.get(frame)?.());
  });

  const attach = (frame) => {
    if (attachedFrames.has(frame)) return;
    attachedFrames.add(frame);
    const board = frame.firstElementChild;
    if (!board) return;
    frame.title = "Pinch or scroll to zoom, drag to pan, double-tap to reset";

    let frameRect = null;
    let wheelActive = false;
    let wheelTimer = 0;
    let lastPointer = null;
    const pointed = new Map();
    let pinch = null;
    let moved = false;
    let tapTarget = null;
    let lastTap = null;

    const apply = () => {
      const state = stateOf(board);
      if (!state.measured) return;
      state.left = clampAxis(state.left, state.boardWidth * state.scale, state.frameWidth);
      state.top = clampAxis(state.top, state.boardHeight * state.scale, state.frameHeight);
      // The transform is always exact; only the inherited `--zoom` is quantised on screen.
      board.style.transform = `translate(${state.left - state.boardLeft}px, ${state.top - state.boardTop}px) scale(${state.scale})`;
      if (state.renderedScale === null || Math.abs(state.scale - state.renderedScale) >= ZOOM_STEP) {
        board.style.setProperty("--zoom", state.scale);
        state.renderedScale = state.scale;
      }
    };

    // Handlers mutate the numbers, then ask for one frame; several events in a frame coalesce.
    const schedule = () => {
      const state = stateOf(board);
      if (state.raf) return;
      state.raf = requestAnimationFrame(() => {
        state.raf = 0;
        apply();
      });
    };
    const flush = () => {
      const state = stateOf(board);
      if (state.raf) cancelAnimationFrame(state.raf);
      state.raf = 0;
      apply();
    };
    // Gesture end writes the exact final transform and `--zoom`, even if a stepped write skipped it.
    const settle = () => {
      stateOf(board).renderedScale = null;
      flush();
    };
    const refresh = () => {
      measure(frame, board);
      frameRect = null;
      flush();
    };
    // Clamp against the cached metrics: no layout reads while a gesture is running.
    const clampState = (state) => {
      if (!state.measured) return;
      state.left = clampAxis(state.left, state.boardWidth * state.scale, state.frameWidth);
      state.top = clampAxis(state.top, state.boardHeight * state.scale, state.frameHeight);
    };
    // Zoom about a frame-local point, keeping the board point under that point where it was.
    const zoomState = (factor, x, y) => {
      const state = stateOf(board);
      const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, state.scale * factor));
      if (scale === state.scale) return;
      const boardX = (x - state.left) / state.scale;
      const boardY = (y - state.top) / state.scale;
      state.left = x - boardX * scale;
      state.top = y - boardY * scale;
      state.scale = scale;
      clampState(state);
    };
    const reset = () => {
      Object.assign(stateOf(board), { scale: 1, left: null, top: null });
      settle();
    };
    const endWheel = () => {
      wheelTimer = 0;
      wheelActive = false;
      if (pointed.size) return;
      frame.classList.remove("gesturing");
      frameRect = null;
      settle();
    };

    frame.addEventListener("wheel", (event) => {
      event.preventDefault();
      if (!frameRect) frameRect = frame.getBoundingClientRect();
      const deltaY = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      zoomState(
        Math.exp(-deltaY * WHEEL_SENSITIVITY),
        event.clientX - frameRect.left,
        event.clientY - frameRect.top,
      );
      frame.classList.add("gesturing");
      wheelActive = true;
      clearTimeout(wheelTimer);
      wheelTimer = setTimeout(endWheel, WHEEL_GESTURE_MS);
      schedule();
    }, { passive: false });

    const pinchAt = () => {
      const [a, b] = [...pointed.values()];
      return {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        x: (a.x + b.x) / 2,
        y: (a.y + b.y) / 2,
      };
    };

    // Buttons, links and form controls keep their own pointer defaults.
    const INTERACTIVE = "button, a, input, select, textarea, summary, label, [role='button'], [role='link'], [contenteditable='true']";
    frame.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest?.(INTERACTIVE)) return;
      // Stop text selection and native image drags without swallowing control clicks.
      event.preventDefault();
      // Layout can have changed since attach (side sheet opening, tab switch), so refresh here.
      measure(frame, board);
      frameRect = frame.getBoundingClientRect();
      flush();
      pointed.set(event.pointerId, { x: event.clientX, y: event.clientY });
      // Synthetic pointers and a few embedded browsers refuse capture; the pointer map alone
      // is enough to keep dragging and pinching inside the frame.
      try {
        frame.setPointerCapture(event.pointerId);
      } catch {
        // Keep handling the pointer without capture.
      }
      if (pointed.size === 1) {
        lastPointer = { x: event.clientX, y: event.clientY };
        moved = false;
        tapTarget = event.target.closest?.(".map-frame [data-tip]") ?? null;
      } else if (pointed.size === 2) {
        pinch = pinchAt();
        lastPointer = null;
      } else {
        pinch = null;
      }
      frame.classList.add("panning");
      frame.classList.add("gesturing");
    });
    frame.addEventListener("pointermove", (event) => {
      if (!pointed.has(event.pointerId)) return;
      pointed.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointed.size >= 2) {
        const next = pinchAt();
        if (pinch && next.distance > 0 && pinch.distance > 0) {
          if (!frameRect) frameRect = frame.getBoundingClientRect();
          const state = stateOf(board);
          // Scale about the new midpoint, then move that midpoint with the fingers: one update.
          zoomState(
            next.distance / pinch.distance,
            next.x - frameRect.left,
            next.y - frameRect.top,
          );
          state.left += next.x - pinch.x;
          state.top += next.y - pinch.y;
          clampState(state);
          schedule();
        }
        pinch = next;
        moved = true;
        return;
      }
      if (!lastPointer) return;
      const dx = event.clientX - lastPointer.x;
      const dy = event.clientY - lastPointer.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      const state = stateOf(board);
      state.left += dx;
      state.top += dy;
      lastPointer = { x: event.clientX, y: event.clientY };
      clampState(state);
      schedule();
    });
    const endPan = (event) => {
      pointed.delete(event.pointerId);
      pinch = pointed.size === 2 ? pinchAt() : null;
      if (pointed.size === 1) {
        const [remaining] = pointed.values();
        lastPointer = { x: remaining.x, y: remaining.y };
      }
      if (pointed.size === 0) {
        lastPointer = null;
        frame.classList.remove("panning");
        frame.classList.remove("gesturing");
        if (!wheelActive) frameRect = null;
        settle();
      }
      if (event.type !== "pointerup" || pointed.size !== 0) return;
      // A tap, not a drag: show the token or region tip and detect a double tap.
      if (!moved && tapTarget) showTip(tapTarget, event);
      const now = Date.now();
      const double =
        !moved &&
        lastTap &&
        now - lastTap.time < 350 &&
        Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) < 30;
      if (double) {
        reset();
        lastTap = null;
      } else {
        lastTap = { time: now, x: event.clientX, y: event.clientY };
      }
    };
    frame.addEventListener("pointerup", endPan);
    frame.addEventListener("pointercancel", endPan);

    frame.addEventListener("dblclick", reset);
    frame.querySelector(".map-recenter")?.addEventListener("click", reset);

    refreshers.set(frame, refresh);
    new ResizeObserver(refresh).observe(frame);
    // Hidden frames stay unmeasured until ResizeObserver reports a size.
    refresh();
  };

  // Token names pop up in the same pill as the token. The popup sits on the page, not in the board,
  // so zooming does not scale it.
  const tip = document.createElement("div");
  tip.className = "map-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.append(tip);
  let current = null;

  const hideTip = () => {
    current = null;
    tip.hidden = true;
  };

  // Places the pill above the anchor box, or below it when there is no room above. A pointer
  // anchor is a box with no size at the pointer.
  const placeTip = (rect) => {
    const size = tip.getBoundingClientRect();
    const margin = 4;
    const left = Math.min(
      Math.max(margin, rect.left + rect.width / 2 - size.width / 2),
      window.innerWidth - size.width - margin,
    );
    // Above the token, or below it when there is no room above.
    const above = rect.top - size.height - margin * 2;
    const top = above >= margin ? above : rect.bottom + margin;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };

  // Regions have no point of their own, so their tooltip sits at the pointer and follows it.
  const pointerBox = (event) => ({
    left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0,
  });

  const showTip = (target, event) => {
    current = target;
    tip.textContent = target.dataset.tip;
    tip.classList.toggle("here", target.classList.contains("here"));
    tip.hidden = false;
    placeTip(target.hasAttribute("data-follow") ? pointerBox(event) : target.getBoundingClientRect());
  };

  document.addEventListener("pointerover", (event) => {
    const target = event.target.closest?.(".map-frame [data-tip]");
    if (target && target !== current) showTip(target, event);
  });
  document.addEventListener("pointermove", (event) => {
    if (current?.hasAttribute("data-follow")) placeTip(pointerBox(event));
  });
  document.addEventListener("pointerout", (event) => {
    if (current && !current.contains(event.relatedTarget)) hideTip();
  });
  document.addEventListener("pointerdown", hideTip);
  document.addEventListener("wheel", hideTip, { passive: true });
  window.addEventListener("scroll", hideTip, true);

  // A live turn replaces the map panels in place, so this is callable again for the fresh
  // frames. A WeakSet keeps a frame from being wired twice.
  const initMapViews = (root = document) => {
    root.querySelectorAll(".map-frame").forEach(attach);
  };
  window.initMapViews = initMapViews;
  initMapViews();
})();
