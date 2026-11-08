(() => {
  const fields = [...document.querySelectorAll("[data-user-search]")];
  if (!fields.length) return;

  const nameFor = (user) => {
    const name = (user.display_name || "").trim();
    const email = (user.email || "").trim();
    const subject = (user.subject || "").trim();
    return name || email || subject || `Account ${user.id}`;
  };

  const metaFor = (user) => {
    const parts = [`#${user.id}`];
    const name = (user.display_name || "").trim();
    const email = (user.email || "").trim();
    const subject = (user.subject || "").trim();
    if (email && email !== name) parts.push(email);
    if (subject && subject !== email && subject !== name) parts.push(subject);
    if (Number(user.campaigns) === 1) parts.push("1 campaign");
    else if (Number(user.campaigns) > 1) parts.push(`${user.campaigns} campaigns`);
    if (user.disabled) parts.push("disabled");
    return parts.join(" · ");
  };

  for (const field of fields) {
    const list = document.getElementById(field.getAttribute("aria-controls") || "");
    if (!list) continue;
    const wrapper = field.closest(".user-search") || field;
    const target = field.dataset.userSearchTarget
      ? document.getElementById(field.dataset.userSearchTarget)
      : null;
    const selectOnEnter = field.dataset.userSearchEnter === "select";
    const url = field.dataset.userSearchUrl || "/admin/users/search";
    let users = [];
    let options = [];
    let active = -1;
    let controller = null;
    let sequence = 0;
    let emptyMessage = "No matching accounts.";

    const close = () => {
      users = [];
      options = [];
      active = -1;
      list.replaceChildren();
      list.hidden = true;
      field.setAttribute("aria-expanded", "false");
      field.removeAttribute("aria-activedescendant");
    };

    const setActive = (next) => {
      if (!options.length) return;
      active = (next + options.length) % options.length;
      options.forEach((option, optionIndex) => {
        const selected = optionIndex === active;
        option.classList.toggle("is-active", selected);
        option.setAttribute("aria-selected", selected ? "true" : "false");
      });
      field.setAttribute("aria-activedescendant", options[active].id);
      options[active].scrollIntoView?.({ block: "nearest" });
    };

    const choose = (userIndex) => {
      const user = users[userIndex];
      if (!user) return;
      if (target) {
        target.value = String(user.id);
        target.dispatchEvent(new Event("input", { bubbles: true }));
        field.value = nameFor(user);
      } else {
        window.location.assign(`/admin/users/${encodeURIComponent(user.id)}`);
      }
      close();
    };

    const render = () => {
      list.replaceChildren();
      options = [];
      active = -1;
      field.removeAttribute("aria-activedescendant");
      if (!users.length) {
        const empty = document.createElement("li");
        empty.className = "user-search-empty";
        empty.textContent = emptyMessage;
        list.append(empty);
      } else {
        users.forEach((user, userIndex) => {
          const option = document.createElement("li");
          option.className = "user-search-option";
          option.id = `${list.id}-option-${userIndex}`;
          option.setAttribute("role", "option");
          option.setAttribute("aria-selected", "false");

          const name = document.createElement("span");
          name.className = "user-search-name";
          name.textContent = nameFor(user);
          const meta = document.createElement("span");
          meta.className = "user-search-meta";
          meta.textContent = metaFor(user);
          option.append(name, meta);

          // Keep focus in the search field so the suggestion list stays open until chosen.
          option.addEventListener("pointerdown", (event) => {
            event.preventDefault();
            choose(userIndex);
          });
          list.append(option);
          options.push(option);
        });
      }
      list.hidden = false;
      field.setAttribute("aria-expanded", "true");
    };

    const search = async () => {
      const query = field.value.trim();
      if (!query) {
        if (controller) controller.abort();
        close();
        return;
      }
      if (controller) controller.abort();
      controller = new AbortController();
      const current = ++sequence;
      emptyMessage = "No matching accounts.";
      try {
        const response = await fetch(`${url}?q=${encodeURIComponent(query)}`, {
          headers: { Accept: "application/json" },
          signal: controller.signal,
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`search failed: ${response.status}`);
        const data = await response.json();
        if (current !== sequence) return;
        users = Array.isArray(data.users) ? data.users : [];
        render();
      } catch (error) {
        if (error.name === "AbortError" || current !== sequence) return;
        users = [];
        emptyMessage = "Account search is unavailable.";
        render();
      }
    };

    field.addEventListener("input", search);
    field.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!users.length) search();
        else setActive(active + 1);
      } else if (event.key === "ArrowUp") {
        if (!users.length) return;
        event.preventDefault();
        setActive(active - 1);
      } else if (event.key === "Enter") {
        if (active >= 0) {
          event.preventDefault();
          choose(active);
        } else if (selectOnEnter) {
          // This field is a lookup for another input, so Enter must never submit the form on
          // its own. Pick the first result when there is one.
          event.preventDefault();
          if (users.length) choose(0);
        }
      } else if (event.key === "Escape") {
        close();
      }
    });
    field.addEventListener("blur", () => close());
    document.addEventListener("pointerdown", (event) => {
      if (!wrapper.contains(event.target)) close();
    });
  }
})();
