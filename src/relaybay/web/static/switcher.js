// Quick switcher: Ctrl+K (Cmd+K on macOS) or the button in the top bar. Results come from
// GET /switcher as JSON and are written with textContent, so nothing in them is parsed as HTML.
(function () {
  "use strict";

  var dialog = document.getElementById("switcher");
  if (!dialog) return;
  var input = dialog.querySelector(".switcher-input");
  var list = dialog.querySelector(".switcher-results");
  var status = dialog.querySelector(".switcher-status");
  var DEBOUNCE_MS = 120;
  var timer = null;
  var running = null;
  var active = -1;

  function rows() { return list.querySelectorAll(".switcher-row"); }

  function select(index) {
    var all = rows();
    if (!all.length) { active = -1; return; }
    active = (index + all.length) % all.length;
    all.forEach(function (row, i) {
      var on = i === active;
      row.classList.toggle("is-active", on);
      row.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        input.setAttribute("aria-activedescendant", row.id);
        row.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function text(tag, className, value) {
    var el = document.createElement(tag);
    el.className = className;
    el.textContent = value;
    return el;
  }

  function show(items) {
    list.textContent = "";
    items.forEach(function (item, i) {
      var li = document.createElement("li");
      li.setAttribute("role", "presentation");
      var link = document.createElement("a");
      link.className = "switcher-row";
      link.id = "switcher-row-" + i;
      link.href = item.url;
      link.setAttribute("role", "option");
      link.appendChild(text("span", "switcher-kind", item.kind));
      if (item.key) link.appendChild(text("span", "card-key", item.key));
      link.appendChild(text("span", "switcher-title", item.title));
      link.appendChild(text("span", "switcher-detail muted", item.detail));
      li.appendChild(link);
      list.appendChild(li);
    });
    status.textContent = items.length ? "" : "Nothing found.";
    input.removeAttribute("aria-activedescendant");
    select(0);
  }

  function search() {
    if (running) running.abort();
    var controller = new AbortController();
    running = controller;
    fetch("/switcher?q=" + encodeURIComponent(input.value), {
      headers: { Accept: "application/json" },
      signal: controller.signal
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    }).then(function (data) {
      if (running === controller) show(data.items);
    }).catch(function (error) {
      if (error.name === "AbortError") return;
      list.textContent = "";
      active = -1;
      status.textContent = "Search failed (" + error.message + "). Reload the page and sign in again.";
    });
  }

  function open() {
    if (dialog.open) return;
    input.value = "";
    status.textContent = "";
    dialog.showModal();
    input.focus();
    search();
  }

  input.addEventListener("input", function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(search, DEBOUNCE_MS);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      select(active + (event.key === "ArrowDown" ? 1 : -1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      var row = rows()[active];
      if (row) window.location.assign(row.href);
    }
  });

  // A click on the backdrop targets the dialog itself, outside its box.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });

  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest(".switcher-open")) open();
  });

  document.addEventListener("keydown", function (event) {
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
    if (event.key !== "k" && event.key !== "K") return;
    // Typing stays typing: inside a field the browser and the field keep the shortcut.
    var el = event.target;
    if (el && el.closest && el.closest("input, textarea, select, [contenteditable]")) return;
    event.preventDefault();
    open();
  });

  if (/Mac|iPhone|iPad/.test(navigator.platform)) {
    document.querySelectorAll(".switcher-open kbd").forEach(function (kbd) {
      kbd.textContent = "⌘K";
    });
  }
})();
