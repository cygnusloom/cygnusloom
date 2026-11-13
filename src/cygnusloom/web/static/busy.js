(() => {
  // Every form that posts shows what its submit button is doing until the server's page
  // arrives. The pressed button turns into a spinner and a label, and cannot be pressed
  // again. A form that builds its own progress display opts out with data-no-busy.
  //
  // The browser leaves a disabled button out of the request, so a chosen action such as
  // Allow or Deny is carried across in a hidden field. Other buttons are disabled too,
  // so a second action cannot be started while the first is in flight.
  const saved = new WeakMap();

  const spinner = () => {
    const element = document.createElement("span");
    element.className = "spinner";
    element.setAttribute("aria-hidden", "true");
    return element;
  };

  // A label can be set on the form (the usual place) or on one button. Without one, the
  // button's own text says what is happening; an icon-only button just shows the spinner.
  const labelFor = (form, button) => {
    const explicit = (button?.dataset.busyLabel || form.dataset.busyLabel || "").trim();
    if (explicit) return explicit;
    const text = button ? button.textContent.trim() : "";
    return text ? `${text}…` : "";
  };

  const mark = (form, submitter) => {
    if (!form || form.dataset.busy) return;
    form.dataset.busy = "1";
    form.setAttribute("aria-busy", "true");
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    const pressed = submitter instanceof HTMLButtonElement ? submitter : buttons[0];
    if (!pressed) return;
    const label = labelFor(form, pressed);
    for (const button of buttons) {
      if (saved.has(button)) continue;
      saved.set(button, { html: button.innerHTML, disabled: button.disabled });
      button.disabled = true;
    }
    if (pressed.name) {
      const chosen = document.createElement("input");
      chosen.type = "hidden";
      chosen.name = pressed.name;
      chosen.value = pressed.value;
      chosen.dataset.busyValue = "1";
      form.append(chosen);
    }
    pressed.textContent = "";
    pressed.append(spinner());
    if (label) {
      const text = document.createElement("span");
      text.textContent = label;
      pressed.append(text);
    }
    pressed.classList.add("busy-button");
  };

  const clear = (form) => {
    if (!form) return;
    delete form.dataset.busy;
    form.removeAttribute("aria-busy");
    for (const chosen of form.querySelectorAll("input[data-busy-value]")) chosen.remove();
    for (const button of form.querySelectorAll('button[type="submit"]')) {
      const idle = saved.get(button);
      if (!idle) continue;
      button.innerHTML = idle.html;
      button.disabled = idle.disabled;
      button.classList.remove("busy-button");
      saved.delete(button);
    }
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    // Another handler already refused this submit, such as the character form's choice check.
    if (event.defaultPrevented) return;
    // Only a post reaches the server; a GET is a navigation the browser already reports,
    // and a dialog form only closes.
    if (form.method !== "post" || form.hasAttribute("data-no-busy")) return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    mark(form, event.submitter);
  });

  // A back-forward cache restore brings the disabled button back with the page; put it
  // back the way the server rendered it.
  window.addEventListener("pageshow", () => {
    for (const form of document.querySelectorAll("form[data-busy]")) clear(form);
  });

  // A page script that posts by itself uses these to show and drop the same state.
  window.cygnusBusy = { mark, clear };
})();
