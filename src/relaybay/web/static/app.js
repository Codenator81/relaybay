// Live updates: the htmx SSE extension owns the EventSource; this file decides which fragment to
// refetch for each event (no HTML travels over SSE) and reconnects when a phone wakes up.
(function () {
  "use strict";

  var KINDS = [
    "project.created", "project.updated",
    "task.created", "task.updated", "task.stage_changed",
    "report.added", "decision.added", "task.session_bound", "task.session_unbound",
    "session.seen"
  ];
  var live = null;
  var lastId = 0;
  var source = null;
  var pending = new Set();
  var flushTimer = null;

  function setStatus(state) {
    var el = document.getElementById("live-status");
    if (!el) return;
    el.dataset.state = state;
    var label = el.querySelector(".live-label");
    if (label) label.textContent = state;
  }

  // Each new EventSource resumes from the newest id seen. The browser's own retry sends the
  // Last-Event-ID header; a fresh EventSource cannot, so the id goes into the query instead.
  function withLastId(url) {
    var u = new URL(url, window.location.origin);
    u.searchParams.delete("r");
    u.searchParams.set("last_event_id", String(lastId));
    return u.pathname + u.search;
  }

  function createEventSource(url) {
    var es = new EventSource(withLastId(url));
    KINDS.forEach(function (kind) { es.addEventListener(kind, onMessage); });
    es.addEventListener("resync", function () { window.location.reload(); });
    es.addEventListener("open", function () { setStatus("live"); });
    es.addEventListener("error", function () { setStatus("offline"); });
    source = es;
    return es;
  }

  function queue(el) {
    if (!el) return;
    pending.add(el);
    if (flushTimer === null) {
      flushTimer = window.setTimeout(function () {
        flushTimer = null;
        pending.forEach(function (target) {
          if (!document.body.contains(target)) return;
          // A card whose column is refetched anyway would be swapped twice.
          var col = target.closest(".col");
          if (col && col !== target && pending.has(col)) return;
          htmx.trigger(target, "rb:refresh");
        });
        pending.clear();
      }, 50);
    }
  }

  function column(stage) { return stage ? document.getElementById("col-" + stage) : null; }
  function card(key) { return key ? document.getElementById("card-" + key) : null; }

  function onMessage(event) {
    var id = parseInt(event.lastEventId, 10);
    if (!isNaN(id) && id > lastId) lastId = id;
    var data = {};
    try { data = JSON.parse(event.data); } catch (e) { return; }

    // The task page is read-only: reload it when its own task changes.
    if (live.dataset.task) {
      if (data.key === live.dataset.task) window.location.reload();
      return;
    }

    var projects = document.getElementById("projects");
    if (projects) { queue(projects); return; }
    if (!document.getElementById("board")) return;
    if (data.key && dialog && dialog.open && data.key === dialog.dataset.key) refreshDialog();

    switch (data.kind) {
      case "task.created":
        queue(column(data.stage));
        break;
      case "task.stage_changed":
        queue(column(data.from));
        queue(column(data.to));
        break;
      case "task.updated":
        // A done task can leave the done column (archived) when it is marked as logged.
        queue(data.stage === "done" ? column("done") : (card(data.key) || column(data.stage)));
        break;
      case "report.added":
      case "task.session_bound":
      case "task.session_unbound":
        queue(card(data.key) || column(data.stage));
        break;
    }
  }

  function reconnect() {
    if (!live) return;
    if (source) source.close();
    // htmx re-initialises an element whose attributes changed; the SSE extension then opens a
    // new EventSource through createEventSource above.
    var u = new URL(live.getAttribute("sse-connect"), window.location.origin);
    u.searchParams.set("r", String(Date.now()));
    live.setAttribute("sse-connect", u.pathname + u.search);
    htmx.process(live);
  }

  // "Copy link" next to "Open in desktop": the clipboard API needs a secure context, which
  // http://127.0.0.1 is; elsewhere (LAN host) fall back to showing the link selected.
  function copyLink(button) {
    var link = button.dataset.link;
    var field = button.parentNode.querySelector(".link-text");
    function fallback() {
      if (!field) return;
      field.hidden = false;
      field.focus();
      field.select();
      button.textContent = "Selected: copy it";
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(link).then(function () {
        button.textContent = "Copied";
        window.setTimeout(function () { button.textContent = "Copy link"; }, 1500);
      }, fallback);
    } else {
      fallback();
    }
  }

  // --- task dialog: /p/<slug>?task=<key> --------------------------------------------------
  // Opening pushes the ?task= URL; Back, Esc, the close button and a click on the backdrop close
  // it. The board under it keeps updating; the dialog refetches its read-only part on events for
  // its task, and its form only while the user has not typed into it.

  var dialog = null;
  var pushed = false;     // the open dialog added a history entry that closing must undo
  var fromHistory = false;

  function projectPath() { return "/p/" + encodeURIComponent(live.dataset.project); }

  // The board URL with the open task; search and archive filters in the URL are kept.
  function boardUrl(key) {
    var params = new URLSearchParams(window.location.search);
    params.delete("task");
    if (key) params.set("task", key);
    var query = params.toString();
    return projectPath() + (query ? "?" + query : "");
  }

  function editForm() { return dialog.querySelector("form.task-form"); }

  function openTask(key, push) {
    if (dialog.dataset.key !== key) {
      dialog.dataset.key = key;
      dialog.replaceChildren(loadingNote("Loading " + key + "…"));
      htmx.ajax("GET", projectPath() + "/t/" + encodeURIComponent(key) + "/dialog",
        { target: dialog, swap: "innerHTML" });
    }
    if (!dialog.open) dialog.showModal();
    if (!push) return;
    // The URL still names a task when this open raced the previous close: reuse that entry.
    if (new URLSearchParams(window.location.search).has("task")) {
      window.history.replaceState({ task: key }, "", boardUrl(key));
    } else {
      window.history.pushState({ task: key }, "", boardUrl(key));
      pushed = true;
    }
  }

  function loadingNote(text) {
    var p = document.createElement("p");
    p.className = "dialog-loading muted";
    p.textContent = text;
    return p;
  }

  function refreshDialog() {
    queue(dialog.querySelector(".task-view"));
    var form = editForm();
    if (!form) return;
    if (form.dataset.dirty) {
      var note = form.querySelector(".form-stale");
      if (note) note.hidden = false;
    } else {
      queue(form.closest(".task-form-box"));
    }
  }

  // Every way of closing the dialog asks before throwing away unsaved edits.
  function mayDiscard() {
    var form = editForm();
    return !(form && form.dataset.dirty) || window.confirm("Discard your edits?");
  }

  function requestClose() {
    if (mayDiscard()) dialog.close();
  }

  function setupDialog() {
    dialog = document.getElementById("task-dialog");
    if (!dialog) return;

    dialog.addEventListener("close", function () {
      // "close" is dispatched asynchronously: a task opened in the meantime stays open.
      if (dialog.open) return;
      delete dialog.dataset.key;
      dialog.replaceChildren();
      if (fromHistory) {
        fromHistory = false;
      } else if (pushed) {
        // Also async: a task opened before this back() lands is closed by its popstate (rare race).
        window.history.back();
      } else if (new URLSearchParams(window.location.search).has("task")) {
        window.history.replaceState(null, "", boardUrl(null));
      }
      pushed = false;
    });
    // Handle Esc ourselves: Chromium makes a repeated Esc "cancel" non-cancelable, which would
    // close the dialog even after the user chose to keep their edits.
    dialog.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      requestClose();
    });
    dialog.addEventListener("cancel", function (event) {
      if (!mayDiscard()) event.preventDefault();
    });
    dialog.addEventListener("click", function (event) {
      // A click on the backdrop targets the dialog itself, outside its box.
      if (event.target !== dialog) return;
      var r = dialog.getBoundingClientRect();
      var inside = event.clientX >= r.left && event.clientX <= r.right &&
        event.clientY >= r.top && event.clientY <= r.bottom;
      if (!inside) requestClose();
    });
    dialog.addEventListener("htmx:responseError", function (event) {
      if (event.detail.target !== dialog) return;
      dialog.replaceChildren(loadingNote("Could not load the task (HTTP " +
        event.detail.xhr.status + "). Close this and reload the board."));
    });
    dialog.addEventListener("input", function (event) {
      var form = event.target.closest("form.task-form");
      if (form) form.dataset.dirty = "1";
    });

    window.addEventListener("popstate", function () {
      var key = new URLSearchParams(window.location.search).get("task");
      if (key) {
        openTask(key, false);
      } else if (dialog.open) {
        if (!mayDiscard()) {
          // Back already left the ?task= entry: put it back so URL and dialog agree.
          window.history.pushState({ task: dialog.dataset.key }, "", boardUrl(dialog.dataset.key));
          pushed = true;
          return;
        }
        fromHistory = true;
        dialog.close();
      }
    });

    if (dialog.dataset.open) {
      dialog.dataset.key = dialog.dataset.open;
      delete dialog.dataset.open;
      dialog.showModal();
    }
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var button = event.target.closest(".copy-link");
    if (button) {
      event.preventDefault();
      copyLink(button);
      return;
    }
    var open = event.target.closest("a.card-open[data-task]");
    if (open && dialog && event.button === 0 &&
        !(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) {
      event.preventDefault();
      openTask(open.dataset.task, true);
      return;
    }
    if (dialog && event.target.closest(".dialog-close")) {
      requestClose();
      return;
    }
    var reload = event.target.closest(".form-reload");
    if (reload) queue(reload.closest(".task-form-box"));
  });

  // "Show archived" applies at once; the search applies on Enter or the Filter button.
  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches(".board-tools input[type=checkbox]")) {
      event.target.form.requestSubmit();
    }
  });

  function refreshAges() {
    document.querySelectorAll("time[data-age]").forEach(function (el) {
      var then = Date.parse(el.getAttribute("datetime"));
      if (isNaN(then)) return;
      var s = Math.max(0, Math.floor((Date.now() - then) / 1000));
      var text = "just now";
      if (s >= 86400) text = Math.floor(s / 86400) + "d ago";
      else if (s >= 3600) text = Math.floor(s / 3600) + "h ago";
      else if (s >= 60) text = Math.floor(s / 60) + "m ago";
      el.textContent = text;
    });
  }

  // A deferred script runs after parsing but before htmx processes the page on DOMContentLoaded,
  // so the starting id must be read here, not in a DOMContentLoaded handler.
  htmx.createEventSource = createEventSource;
  live = document.getElementById("live");
  if (!live) { setStatus("off"); return; }
  lastId = parseInt(live.dataset.lastEventId || "0", 10) || 0;
  setupDialog();
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") reconnect();
  });
  window.addEventListener("online", reconnect);
  window.setInterval(refreshAges, 60000);
})();
