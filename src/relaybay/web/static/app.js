// Live updates: the htmx SSE extension owns the EventSource; this file decides which fragment to
// refetch for each event (no HTML travels over SSE) and reconnects when a phone wakes up.
(function () {
  "use strict";

  var KINDS = [
    "project.created", "project.updated",
    "task.created", "task.updated", "task.stage_changed",
    "report.added", "decision.added", "task.session_bound", "session.seen"
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

  function isEditing(el) {
    return el.matches("form") || el.querySelector("form.card-form") !== null;
  }

  function queue(el) {
    if (!el || isEditing(el)) return;
    pending.add(el);
    if (flushTimer === null) {
      flushTimer = window.setTimeout(function () {
        flushTimer = null;
        pending.forEach(function (target) {
          if (!document.body.contains(target)) return;
          // A card whose column is refetched anyway would be swapped twice.
          var col = target.closest(".col");
          if (col !== target && pending.has(col)) return;
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

    switch (data.kind) {
      case "task.created":
        queue(column(data.stage));
        break;
      case "task.stage_changed":
        queue(column(data.from));
        queue(column(data.to));
        break;
      case "task.updated":
      case "report.added":
      case "task.session_bound":
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

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest(".copy-link") : null;
    if (button) {
      event.preventDefault();
      copyLink(button);
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
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") reconnect();
  });
  window.addEventListener("online", reconnect);
  window.setInterval(refreshAges, 60000);
})();
