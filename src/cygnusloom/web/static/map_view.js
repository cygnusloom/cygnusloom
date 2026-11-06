(() => {
  const MAX_SCALE = 6;
  const WHEEL_SENSITIVITY = 0.0015;

  // Each board keeps its own zoom and position; hidden tabs keep their state.
  // `left` and `top` are where the board's top-left corner sits inside its frame.
  const states = new WeakMap();
  const stateOf = (board) => {
    if (!states.has(board)) states.set(board, { scale: 1, left: null, top: null });
    return states.get(board);
  };

  // Keep the board covering the frame, with no gap at the edges. A board smaller than the frame is centred.
  const clampAxis = (position, size, frameSize) => {
    if (size <= frameSize) return (frameSize - size) / 2;
    return Math.min(0, Math.max(frameSize - size, position ?? (frameSize - size) / 2));
  };

  const fit = (frame, board) => {
    // Hidden tabs have no size; the board is fitted again when it is shown.
    if (!frame.clientWidth || !frame.clientHeight) return;
    const state = stateOf(board);
    const width = board.offsetWidth * state.scale;
    const height = board.offsetHeight * state.scale;
    state.left = clampAxis(state.left, width, frame.clientWidth);
    state.top = clampAxis(state.top, height, frame.clientHeight);
    board.style.transform = `translate(${state.left - board.offsetLeft}px, ${state.top - board.offsetTop}px) scale(${state.scale})`;
    board.style.setProperty("--zoom", state.scale);
  };

  const zoomAt = (frame, board, factor, pointX, pointY) => {
    const state = stateOf(board);
    const scale = Math.min(MAX_SCALE, Math.max(1, state.scale * factor));
    if (scale === state.scale) return;
    // Keep the board point under the pointer where it was.
    const boardX = (pointX - state.left) / state.scale;
    const boardY = (pointY - state.top) / state.scale;
    state.left = pointX - boardX * scale;
    state.top = pointY - boardY * scale;
    state.scale = scale;
    fit(frame, board);
  };

  const attachedFrames = new WeakSet();
  const attach = (frame) => {
    if (attachedFrames.has(frame)) return;
    attachedFrames.add(frame);
    const board = frame.firstElementChild;
    if (!board) return;
    frame.title = "Pinch or scroll to zoom, drag to pan, double-tap to reset";

    const reset = () => {
      Object.assign(stateOf(board), { scale: 1, left: null, top: null });
      fit(frame, board);
    };

    frame.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = frame.getBoundingClientRect();
      const deltaY = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
      zoomAt(frame, board, Math.exp(-deltaY * WHEEL_SENSITIVITY), event.clientX - rect.left, event.clientY - rect.top);
    }, { passive: false });

    let lastPointer = null;
    const pointed = new Map();
    let pinch = null;
    let moved = false;
    let tapTarget = null;
    let lastTap = null;
    const pinchAt = () => {
      const [a, b] = [...pointed.values()];
      return {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        x: (a.x + b.x) / 2,
        y: (a.y + b.y) / 2,
      };
    };
    frame.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest?.("button")) return;
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
    });
    frame.addEventListener("pointermove", (event) => {
      if (!pointed.has(event.pointerId)) return;
      pointed.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointed.size >= 2) {
        const next = pinchAt();
        if (pinch && next.distance > 0 && pinch.distance > 0) {
          const rect = frame.getBoundingClientRect();
          zoomAt(
            frame,
            board,
            next.distance / pinch.distance,
            next.x - rect.left,
            next.y - rect.top,
          );
          const state = stateOf(board);
          state.left += next.x - pinch.x;
          state.top += next.y - pinch.y;
          fit(frame, board);
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
      fit(frame, board);
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

    new ResizeObserver(() => fit(frame, board)).observe(frame);
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
