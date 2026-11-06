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
    frame.title = "Scroll to zoom, drag to pan, double-click to reset";

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
    frame.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest?.("button")) return;
      lastPointer = { x: event.clientX, y: event.clientY };
      frame.setPointerCapture(event.pointerId);
      frame.classList.add("panning");
    });
    frame.addEventListener("pointermove", (event) => {
      if (!lastPointer) return;
      const state = stateOf(board);
      state.left += event.clientX - lastPointer.x;
      state.top += event.clientY - lastPointer.y;
      lastPointer = { x: event.clientX, y: event.clientY };
      fit(frame, board);
    });
    const endPan = () => {
      lastPointer = null;
      frame.classList.remove("panning");
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
