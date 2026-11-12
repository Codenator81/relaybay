// Live updates: the htmx SSE extension owns the EventSource; this file decides which fragment to
// refetch for each event (no HTML travels over SSE) and reconnects when a phone wakes up.
(function () {
  "use strict";

  var KINDS = [
    "project.created", "project.updated", "project.pulse_set",
    "task.created", "task.updated", "task.stage_changed", "task.reordered",
    "report.added", "decision.added", "task.session_bound", "task.session_unbound",
    "task.tracker_unlogged", "session.seen", "session.status_changed", "session.alias_added",
    "session.no_live_on", "session.no_live_off",
    "comment.added", "comment.delivered", "comment.read",
    "message.sent", "message.delivered", "message.read", "message.loop_stopped",
    "message.loop_dismissed",
    "environment.created", "environment.updated", "environment.deleted",
    "environment.access_added", "environment.access_updated", "environment.access_removed",
    "environment.proposed", "environment.proposal_accepted", "environment.proposal_rejected",
    "policy.updated", "policy.proposed", "policy.proposal_accepted", "policy.proposal_rejected",
    "policy.proposal_reopened",
    "rules.updated", "rules.proposed", "rules.proposal_accepted", "rules.proposal_rejected",
    "rules.notice_read", "filter.saved", "filter.deleted",
    "live.action",
    "review.step_added", "review.step_updated", "review.step_removed", "review.step_accepted",
    "review.step_rejected", "attachment.added", "attachment.updated", "attachment.removed",
    "plan.set", "plan.step_added", "plan.step_updated", "plan.accepted", "session.todo_updated",
    "session.message", "session.launched",
    "document.updated", "memory.added", "memory.updated", "block.updated",
    "annotation.added", "annotation.updated", "annotation.removed", "annotation.replied",
    "intake.received", "intake.tied", "intake.answer_received", "intake.answer_cleared",
    "intake.wake", "intake.forwarded", "intake.proposed", "intake.dismissed",
    "intake.source_trusted", "intake.source_untrusted",
    "backlog.added", "backlog.updated", "backlog.planned",
    "brake.set", "brake.released", "budget.set", "budget.removed", "budget.warned",
    "budget.exceeded", "concurrency.over",
    "run.started", "run.turn_started", "run.turn_queued", "run.turn_ended", "run.stopped",
    "run.ended", "run.lost"
  ];
  // Events that can change what the Inbox lists (and its count in the sidebar).
  var INBOX_KINDS = [
    "task.created", "task.updated", "task.stage_changed", "task.tracker_unlogged",
    "session.seen", "session.status_changed", "comment.added", "comment.read",
    "message.loop_stopped", "message.loop_dismissed",
    "environment.proposed", "environment.proposal_accepted", "environment.proposal_rejected",
    "policy.proposed", "policy.proposal_accepted", "policy.proposal_rejected",
    "policy.proposal_reopened",
    "rules.updated", "rules.notice_read", "rules.proposed", "rules.proposal_accepted",
    "rules.proposal_rejected",
    "review.step_added", "review.step_updated", "review.step_removed", "review.step_accepted",
    "review.step_rejected",
    "intake.received", "intake.tied", "intake.answer_received", "intake.answer_cleared",
    "intake.proposed", "intake.dismissed", "intake.source_trusted", "intake.source_untrusted",
    "backlog.added", "backlog.updated", "backlog.planned",
    "brake.set", "brake.released", "budget.set", "budget.removed", "budget.warned",
    "budget.exceeded", "concurrency.over"
  ];
  var SESSION_KINDS = [
    "session.seen", "session.status_changed", "session.alias_added",
    "session.no_live_on", "session.no_live_off",
    "message.sent", "message.read",
    "task.session_bound", "task.session_unbound"
  ];
  var DOCS_KINDS = [
    "document.updated", "decision.added", "memory.added", "memory.updated", "project.pulse_set"
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

  // A reload would take the open viewer, and a send under way, with it: it waits for the close.
  var staleBehindViewer = false;
  function reloadPage() {
    if (viewer && viewer.open) {
      staleBehindViewer = true;
      return;
    }
    window.location.reload();
  }

  function createEventSource(url) {
    var es = new EventSource(withLastId(url));
    KINDS.forEach(function (kind) { es.addEventListener(kind, onMessage); });
    es.addEventListener("resync", reloadPage);
    es.addEventListener("open", function () { setStatus("live"); });
    es.addEventListener("error", function () { setStatus("offline"); });
    source = es;
    return es;
  }

  function queue(el) {
    if (!el) return;
    pending.add(el);
    if (flushTimer === null) {
      flushTimer = window.setTimeout(function flush() {
        // A refetch under a card being dragged would take the card away mid-drag.
        if (window.relaybayBoard && window.relaybayBoard.busy()) {
          flushTimer = window.setTimeout(flush, 250);
          return;
        }
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

  // Hook status events come every few seconds per working session: lists that react to them
  // are refetched at most once per THROTTLE_MS (the last event in the window still counts).
  var THROTTLE_MS = 2000;
  var throttled = new Map();
  function queueThrottled(el) {
    if (!el || throttled.has(el.id)) return;
    throttled.set(el.id, window.setTimeout(function () {
      throttled.delete(el.id);
      queue(document.getElementById(el.id));
    }, THROTTLE_MS));
  }

  // Activity and Audit: prepend the events newer than the newest row shown, with the page's
  // filters.
  var activityBusy = false;
  var activityAgain = false;
  function prependActivity(list) {
    if (activityBusy) { activityAgain = true; return; }
    var first = list.querySelector("[data-id]");
    var newest = first ? first.dataset.id : list.dataset.newest;
    var filters = list.dataset.filters;
    activityBusy = true;
    htmx.ajax("GET", list.dataset.fragment + "?after=" + encodeURIComponent(newest || "0") +
      (filters ? "&" + filters : ""), { target: list, swap: "afterbegin" }).finally(function () {
      activityBusy = false;
      var empty = document.querySelector(".activity-empty");
      if (empty && list.querySelector("[data-id]")) empty.remove();
      if (activityAgain) { activityAgain = false; prependActivity(list); }
    });
  }

  function column(stage) { return stage ? document.getElementById("col-" + stage) : null; }
  function thread(key) { return key ? document.getElementById("comments-" + key) : null; }
  function isMessage(kind) { return kind.indexOf("message.") === 0; }
  function isComment(kind) { return kind.indexOf("comment.") === 0; }
  function isReview(kind) {
    return kind.indexOf("review.") === 0 || kind.indexOf("attachment.") === 0;
  }
  function reviewPanel(key) { return key ? document.getElementById("review-" + key) : null; }
  // Plan events and the bound agent's todo changes refetch the Plan panel.
  function isPlan(kind) { return kind.indexOf("plan.") === 0 || kind === "session.todo_updated"; }
  function planPanel(key) { return key ? document.getElementById("plan-" + key) : null; }
  // Run events refetch the card's Agent block, unless a text is being typed into it.
  function isRun(kind) { return kind.indexOf("run.") === 0; }
  function queueRun(key) {
    var panel = key ? document.getElementById("run-" + key) : null;
    if (!panel) return;
    var typing = Array.prototype.some.call(panel.querySelectorAll("textarea[name=text]"),
      function (f) { return f.value.trim(); });
    if (!typing) queue(panel);
  }
  // A review panel is refetched only while none of its comment boxes holds text.
  function queueReview(key) {
    var panel = reviewPanel(key);
    if (!panel) return;
    var typing = Array.prototype.some.call(panel.querySelectorAll("textarea"), function (f) {
      return f.value.trim();
    });
    if (typing) panel.dataset.stale = "1"; else queue(panel);
  }
  function card(key) { return key ? document.getElementById("card-" + key) : null; }
  // With a filter on the board a changed task may leave or enter the view: its column decides,
  // a refetched card would stay on the board whatever it now says.
  function changed(key, stage) {
    var board = document.getElementById("board");
    return (board && board.dataset.filtered) ? column(stage) : (card(key) || column(stage));
  }

  function onMessage(event) {
    var id = parseInt(event.lastEventId, 10);
    if (!isNaN(id) && id > lastId) lastId = id;
    var data = {};
    try { data = JSON.parse(event.data); } catch (e) { return; }

    if (INBOX_KINDS.indexOf(data.kind) !== -1) {
      queueThrottled(document.getElementById("inbox-badge"));
      queueThrottled(document.getElementById("inbox"));
    }
    if (data.kind.indexOf("brake.") === 0) queue(document.getElementById("brake-banner"));
    if (data.kind.indexOf("budget.") === 0 || data.kind.indexOf("brake.") === 0) {
      queue(document.getElementById("budgets"));
    }
    if (data.kind === "task.stage_changed") notifyAcceptance(data);
    if (data.kind === "plan.accepted" && window.relaybayBoard) {
      window.relaybayBoard.planAccepted(data.key);
    }
    if (data.kind === "intake.answer_received") notifyAnswer(data);
    // The backlog page: only its list is live; the add form stays as the user left it.
    var backlogList = document.getElementById("backlog");
    if (backlogList) {
      if (data.kind.indexOf("backlog.") === 0 &&
          (!live.dataset.project || data.project === live.dataset.project)) {
        queue(backlogList);
      }
      return;
    }
    // Markup changes only the open viewer; the comment that may follow has its own event.
    if (data.kind.indexOf("annotation.") === 0) {
      if (viewer && viewer.open) window.relaybayAnnotate.onEvent(data);
      return;
    }

    // The task page: comment events refetch the thread; other changes of its own task reload
    // the page (not every hook status of its session, which comes every few seconds while it
    // works), or only refetch the details while a comment is being written.
    if (live.dataset.task) {
      if (data.key !== live.dataset.task || data.kind === "session.status_changed") return;
      if (isComment(data.kind)) {
        queue(thread(data.key));
      } else if (isMessage(data.kind)) {
        queue(document.querySelector(".task-view"));
      } else if (isReview(data.kind)) {
        queueReview(data.key);
      } else if (isPlan(data.kind)) {
        queue(planPanel(data.key));
      } else if (isRun(data.kind)) {
        queueRun(data.key);
      } else if (commentDraft(document)) {
        queue(document.querySelector(".task-view"));
        queue(thread(data.key));
      } else {
        reloadPage();
      }
      return;
    }

    var projects = document.getElementById("projects");
    if (projects) { queue(projects); return; }
    var sessions = document.getElementById("sessions");
    if (sessions) {
      if (SESSION_KINDS.indexOf(data.kind) !== -1) queueThrottled(sessions);
      return;
    }
    var journal = document.getElementById("journal");
    if (journal && data.kind === "live.action") queueThrottled(journal);
    // Settings → Policy: only the proposals list is live, like Stands & access below.
    var policyProposals = document.getElementById("policy-proposals");
    if (policyProposals) {
      if (data.kind.indexOf("policy.propos") === 0) queue(policyProposals);
      return;
    }
    // Settings → Rules: the same.
    var ruleProposals = document.getElementById("rule-proposals");
    if (ruleProposals) {
      if (data.kind.indexOf("rules.propos") === 0) queue(ruleProposals);
      return;
    }
    // Stands & access: only the proposals list is live; the forms stay as the user left them.
    var proposals = document.getElementById("proposals");
    if (proposals) {
      if (data.kind.indexOf("environment.propos") === 0 && data.project === live.dataset.project) {
        queue(proposals);
      }
      return;
    }
    // Docs & rules: documents, decisions and notes are live; block forms and search results
    // stay as the user left them. Global notes and blocks carry no project.
    var docsLive = document.getElementById("docs-live");
    if (docsLive) {
      if (DOCS_KINDS.indexOf(data.kind) !== -1 &&
          (!data.project || data.project === live.dataset.project)) {
        queue(docsLive);
      }
      return;
    }
    // Messages: the list is refetched from the server, marks change on rows already shown.
    var messages = document.getElementById("messages");
    if (messages) {
      if (messages.dataset.live && isMessage(data.kind)) queueThrottled(messages);
      return;
    }
    var audit = document.getElementById("audit");
    if (audit) {
      if (audit.dataset.live && (data.kind === "live.action" || (audit.dataset.changes &&
          /^(policy\.|environment\.|session\.no_live_)/.test(data.kind)))) {
        prependActivity(audit);
      }
      return;
    }
    var activity = document.getElementById("activity");
    if (activity) {
      if (activity.dataset.live) prependActivity(activity);
      return;
    }
    if (!document.getElementById("board")) return;
    // Every page listens to all projects (the Inbox count); the board only to its own.
    if (data.project !== live.dataset.project) return;
    if (data.key && dialog && dialog.open && data.key === dialog.dataset.key) {
      if (isComment(data.kind)) queue(thread(data.key));
      else if (isReview(data.kind)) queueReview(data.key);
      else if (isPlan(data.kind)) queue(planPanel(data.key));
      else if (isRun(data.kind)) queueRun(data.key);
      else refreshDialog();
    }
    if (isRun(data.kind)) return;
    if (isReview(data.kind) || isPlan(data.kind)) {
      // The card shows the plan's steps, not the agent's todo.
      if (data.kind !== "session.todo_updated") queue(card(data.key) || column(data.stage));
      return;
    }

    switch (data.kind) {
      case "task.created":
        queue(column(data.stage));
        break;
      case "task.stage_changed":
        queue(column(data.from));
        queue(column(data.to));
        break;
      case "task.reordered":
        queue(column(data.stage));
        break;
      case "task.updated":
      case "task.tracker_unlogged":
        // A done task can leave the done column (archived) when it is marked as logged.
        queue(data.stage === "done" ? column("done") : changed(data.key, data.stage));
        break;
      case "task.session_bound":
      case "task.session_unbound":
        queue(changed(data.key, data.stage));
        break;
      case "report.added":
      case "intake.answer_received":
      case "intake.answer_cleared":
        queue(card(data.key) || column(data.stage));
        break;
      case "session.status_changed":
        // One event names every bound task of this project; a card not on the board is skipped.
        (data.keys || []).forEach(function (key) { queue(card(key)); });
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
  function boardUrl(key, tab) {
    var params = new URLSearchParams(window.location.search);
    params.delete("task");
    params.delete("tab");
    if (key) params.set("task", key);
    if (key && (tab === "review" || tab === "plan")) params.set("tab", tab);
    var query = params.toString();
    return projectPath() + (query ? "?" + query : "");
  }

  function editForm() { return dialog.querySelector("form.task-form"); }

  function openTask(key, push, tab) {
    if (dialog.dataset.key !== key) {
      dialog.dataset.key = key;
      dialog.replaceChildren(loadingNote("Loading " + key + "…"));
      htmx.ajax("GET", projectPath() + "/t/" + encodeURIComponent(key) + "/dialog" +
        (tab === "review" || tab === "plan" ? "?tab=" + tab : ""),
        { target: dialog, swap: "innerHTML" });
    } else {
      showTab(dialog, tab === "review" || tab === "plan" ? tab : "overview");
    }
    if (!dialog.open) dialog.showModal();
    if (!push) return;
    // The URL still names a task when this open raced the previous close: reuse that entry.
    if (new URLSearchParams(window.location.search).has("task")) {
      window.history.replaceState({ task: key }, "", boardUrl(key, tab));
    } else {
      window.history.pushState({ task: key }, "", boardUrl(key, tab));
      pushed = true;
    }
  }

  // Overview | Plan | Review tabs of the task dialog; the open tab is in the URL (?tab=plan).
  function showTab(root, name) {
    var tabs = root.querySelectorAll(".dialog-tab");
    if (!tabs.length) return;
    tabs.forEach(function (tab) {
      var on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      var panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
  }

  function loadingNote(text) {
    var p = document.createElement("p");
    p.className = "dialog-loading muted";
    p.textContent = text;
    return p;
  }

  function commentDraft(root) {
    var field = root.querySelector("form.comment-form textarea");
    return !!(field && field.value.trim());
  }

  function refreshDialog() {
    queue(dialog.querySelector(".task-view"));
    queue(dialog.querySelector(".comment-thread"));
    queueReview(dialog.dataset.key);
    var form = editForm();
    if (!form) return;
    if (form.dataset.dirty) {
      var note = form.querySelector(".form-stale");
      if (note) note.hidden = false;
    } else {
      refreshForm(form.closest(".task-form-box"));
    }
  }

  // The save's own events arrive right after it: a refetch would wipe its rule warnings at once.
  // While they are shown (5 s), a refresh is deferred to the end of that time, not dropped.
  var WARNING_HOLD_MS = 5000;
  function refreshForm(box) {
    if (!box.querySelector(".form-warning")) { queue(box); return; }
    if (!box.dataset.warnedAt) box.dataset.warnedAt = String(Date.now());
    var left = WARNING_HOLD_MS - (Date.now() - Number(box.dataset.warnedAt));
    if (left <= 0) { queue(box); return; }
    if (box.dataset.refreshDeferred) return;
    box.dataset.refreshDeferred = "1";
    window.setTimeout(function () {
      var form = box.querySelector("form.task-form");
      if (!document.body.contains(box) || !form) return;
      if (form.dataset.dirty) {
        var note = form.querySelector(".form-stale");
        if (note) note.hidden = false;
      } else {
        queue(box);
      }
    }, left);
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
      var params = new URLSearchParams(window.location.search);
      var key = params.get("task");
      if (key) {
        openTask(key, false, params.get("tab"));
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

  // --- what the fragments do not hold: a refresh renders every card collapsed and every older
  // report closed, so their state lives here and is put back after each swap. Storage can be
  // unavailable: the choice then lasts until the page is left.

  var COMPACT_KEY = "relaybay.compact";  // read by nav.js before the first paint
  var EXPANDED_KEY = "relaybay.compact.expanded";
  var EXPANDED_MAX = 200;
  var OPEN_REPORTS_KEY = "relaybay.reports.open";
  var FOLDS_KEY = "relaybay.folds";  // section key -> open, for all tasks

  function storedSet(area, key) {
    try {
      return new Set(JSON.parse(window[area].getItem(key) || "[]"));
    } catch (e) {
      return new Set();
    }
  }
  function storeValue(area, key, value) {
    try { window[area].setItem(key, value); } catch (e) { /* see above */ }
  }
  var expandedCards = storedSet("localStorage", EXPANDED_KEY);
  // sessionStorage: the task page reloads itself on most live events.
  var openReports = storedSet("sessionStorage", OPEN_REPORTS_KEY);

  function storedFolds() {
    try {
      return JSON.parse(window.localStorage.getItem(FOLDS_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }
  var folds = storedFolds();
  function applyFolds(root) {
    root.querySelectorAll("details[data-fold]").forEach(function (details) {
      var open = folds[details.dataset.fold];
      if (open !== undefined) details.open = open;
    });
  }

  function showCardExpanded(button, on) {
    var card = button.closest(".card");
    var text = (on ? "Collapse " : "Expand ") + card.dataset.key;
    card.classList.toggle("is-expanded", on);
    button.setAttribute("aria-expanded", on ? "true" : "false");
    button.setAttribute("aria-label", text);
    button.title = text;
  }
  function restoreKept(root) {
    applyFolds(root);
    root.querySelectorAll(".card-expand").forEach(function (button) {
      showCardExpanded(button, expandedCards.has(button.closest(".card").dataset.key));
    });
    root.querySelectorAll("details[data-report]").forEach(function (details) {
      if (openReports.has(details.dataset.report)) details.open = true;
    });
  }
  function toggleCard(button) {
    var key = button.closest(".card").dataset.key;
    var on = !expandedCards.has(key);
    if (on) expandedCards.add(key); else expandedCards.delete(key);
    showCardExpanded(button, on);
    var kept = Array.from(expandedCards).slice(-EXPANDED_MAX);
    expandedCards = new Set(kept);
    storeValue("localStorage", EXPANDED_KEY, JSON.stringify(kept));
  }
  function showCompactButton() {
    var button = document.querySelector(".board-compact");
    if (!button) return;
    button.setAttribute("aria-pressed", document.documentElement.dataset.compact ? "true" : "false");
  }
  function toggleCompact() {
    var root = document.documentElement;
    if (root.dataset.compact) delete root.dataset.compact; else root.dataset.compact = "1";
    storeValue("localStorage", COMPACT_KEY, root.dataset.compact ? "1" : "0");
    showCompactButton();
  }

  document.addEventListener("htmx:load", function (event) {
    if (event.detail.elt.querySelectorAll) restoreKept(event.detail.elt);
  });
  // Right after the swap, ahead of htmx:load (which waits for the settle): no flash of an open
  // section the owner closed.
  document.addEventListener("htmx:afterSwap", function () { applyFolds(document); });
  // The click, not "toggle": toggle also fires for a section just rendered open, and would take
  // that for the owner's choice. Enter and Space on a summary arrive as a click too.
  document.addEventListener("click", function (event) {
    var summary = event.target.closest && event.target.closest("summary");
    var details = summary && summary.parentElement;
    if (!details || !details.matches("details[data-fold]")) return;
    folds[details.dataset.fold] = !details.open;
    storeValue("localStorage", FOLDS_KEY, JSON.stringify(folds));
  });
  // "toggle" does not bubble, hence the capture.
  document.addEventListener("toggle", function (event) {
    var details = event.target;
    if (!details.matches || !details.matches("details[data-report]")) return;
    if (details.open) openReports.add(details.dataset.report);
    else openReports.delete(details.dataset.report);
    storeValue("sessionStorage", OPEN_REPORTS_KEY, JSON.stringify(Array.from(openReports)));
  }, true);

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var chevron = event.target.closest(".card-expand");
    if (chevron) {
      toggleCard(chevron);
      return;
    }
    if (event.target.closest(".board-compact")) {
      toggleCompact();
      return;
    }
    var button = event.target.closest(".copy-link");
    if (button) {
      event.preventDefault();
      copyLink(button);
      return;
    }
    var plain = event.button === 0 &&
      !(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey);
    var open = event.target.closest(
      "a.card-open[data-task], a.review-chip[data-task], a.plan-chip[data-task]");
    if (open && dialog && plain) {
      event.preventDefault();
      openTask(open.dataset.task, true, open.dataset.tab);
      return;
    }
    var tab = event.target.closest(".dialog-tab");
    if (tab && dialog && dialog.contains(tab)) {
      showTab(dialog, tab.dataset.tab);
      if (dialog.dataset.key) {
        window.history.replaceState(window.history.state, "",
          boardUrl(dialog.dataset.key, tab.dataset.tab));
      }
      return;
    }
    var shot = event.target.closest("a[data-viewer]");
    if (shot && plain) {
      event.preventDefault();
      openViewer(shot);
      return;
    }
    var pager = event.target.closest(".pager-link[data-page]");
    if (pager && plain) {
      event.preventDefault();
      setPage(parseInt(pager.dataset.page, 10));
      return;
    }
    if (event.target.closest("[data-notify-enable]")) {
      askNotifications();
      return;
    }
    if (dialog && event.target.closest(".dialog-close")) {
      requestClose();
      return;
    }
    var reload = event.target.closest(".form-reload");
    if (reload) queue(reload.closest(".task-form-box"));
  });

  // The Review tab's "accepted / all" and the Plan tab's "done / all" follow their panels.
  document.addEventListener("htmx:load", function (event) {
    var panel = event.detail.elt;
    if (!panel.matches || !dialog) return;
    var name = panel.matches(".review[data-count]") ? "review" :
      panel.matches(".plan[data-count]") ? "plan" : null;
    if (!name) return;
    var count = dialog.querySelector(".dialog-tab[data-tab=" + name + "] .col-count");
    if (count) count.textContent = panel.dataset.count;
  });

  // A sent comment clears its form; a refused one (error shown in the thread) keeps the text.
  document.addEventListener("htmx:afterRequest", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("form.comment-form")) return;
    var xhr = event.detail.xhr;
    if (event.detail.successful && xhr && xhr.getResponseHeader("X-Relaybay-Comment") === "saved") {
      form.reset();
    }
  });

  // Plain forms with `data-confirm` (deleting an environment or an access entry) ask first.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
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

  // --- paged board: page 1 plan…test, page 2 live and done; ←/→ and the pager links --------
  // The page is in the URL (?page=2); the columns stay in the page, so live updates go on.

  function boardEl() { return document.getElementById("board"); }
  function pageCount() { return document.querySelectorAll(".pager-link[data-page]").length; }

  function setPage(n) {
    var board = boardEl();
    var count = pageCount();
    if (!board || !count || isNaN(n) || n < 1 || n > count) return;
    board.dataset.page = String(n);
    document.querySelectorAll(".pager-link[data-page]").forEach(function (link) {
      var on = link.dataset.page === String(n);
      link.classList.toggle("is-active", on);
      if (on) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    });
    var params = new URLSearchParams(window.location.search);
    if (n > 1) params.set("page", String(n)); else params.delete("page");
    var query = params.toString();
    window.history.replaceState(window.history.state, "",
      window.location.pathname + (query ? "?" + query : ""));
  }

  // Keys that type or move a caret, and open dialogs (task, viewer), keep their arrows.
  function arrowsBelongElsewhere(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return true;
    var el = event.target;
    if (el && el.closest && el.closest("input, textarea, select, [contenteditable], dialog")) {
      return true;
    }
    return !!document.querySelector("dialog[open]");
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    var board = boardEl();
    if (!board || !pageCount() || arrowsBelongElsewhere(event)) return;
    var current = parseInt(board.dataset.page || "1", 10);
    var next = current + (event.key === "ArrowRight" ? 1 : -1);
    if (next < 1 || next > pageCount()) return;
    event.preventDefault();
    setPage(next);
  });

  // --- screenshot viewer: full screen, fit, caption, ←/→, Esc, counter, swipe ---------------

  var viewer = null;
  var shots = [];
  var shotIndex = 0;

  function showShot(i) {
    if (!shots.length || window.relaybayAnnotate.holds()) return;
    shotIndex = (i + shots.length) % shots.length;
    var link = shots[shotIndex];
    var img = viewer.querySelector(".viewer-img");
    img.src = link.getAttribute("href");
    var inner = link.querySelector("img");
    img.alt = inner ? inner.alt : "";
    viewer.querySelector(".viewer-caption").textContent = link.dataset.caption || "";
    viewer.querySelector(".viewer-count").textContent = (shotIndex + 1) + " / " + shots.length;
    var many = shots.length > 1;
    viewer.querySelector(".viewer-prev").hidden = !many;
    viewer.querySelector(".viewer-next").hidden = !many;
    window.relaybayAnnotate.show(link);
  }

  function openViewer(link) {
    if (!viewer) return;
    var scope = link.closest(".review") || document;
    shots = Array.prototype.slice.call(scope.querySelectorAll("a[data-viewer]"));
    showShot(Math.max(0, shots.indexOf(link)));
    if (!viewer.open) viewer.showModal();
  }

  // Also from the `close` event, for a close the page did not ask for. That event is queued, and
  // a browser may hold it back while the window is hidden, so the page does not wait for it.
  function afterViewerClosed() {
    if (viewer.open) return;
    viewer.querySelector(".viewer-img").removeAttribute("src");
    window.relaybayAnnotate.hide();
    if (!staleBehindViewer) return;
    staleBehindViewer = false;
    if (commentDraft(document)) {
      queue(document.querySelector(".task-view"));
      queue(thread(live.dataset.task));
    } else {
      window.location.reload();
    }
  }

  function closeViewer() {
    if (window.relaybayAnnotate.holds()) return;
    viewer.close();
    afterViewerClosed();
  }

  function setupViewer() {
    viewer = document.getElementById("viewer");
    if (!viewer) return;
    window.relaybayAnnotate.setup(viewer);
    viewer.addEventListener("keydown", function (event) {
      if (window.relaybayAnnotate.onKey(event)) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        event.stopPropagation();
        showShot(shotIndex + (event.key === "ArrowRight" ? 1 : -1));
      } else if (event.key === "Escape") {
        // Only the viewer closes: the task dialog under it stays open.
        event.preventDefault();
        event.stopPropagation();
        closeViewer();
      }
    });
    viewer.addEventListener("click", function (event) {
      if (event.target.closest(".viewer-prev")) showShot(shotIndex - 1);
      else if (event.target.closest(".viewer-next")) showShot(shotIndex + 1);
      else if (event.target.closest(".viewer-close") || event.target === viewer) closeViewer();
    });
    var startX = null;
    viewer.addEventListener("touchstart", function (event) {
      var swipes = event.touches.length === 1 && !window.relaybayAnnotate.takesTouch();
      startX = swipes ? event.touches[0].clientX : null;
    }, { passive: true });
    viewer.addEventListener("touchend", function (event) {
      if (startX === null || !event.changedTouches.length) return;
      var dx = event.changedTouches[0].clientX - startX;
      startX = null;
      if (Math.abs(dx) > 40) showShot(shotIndex + (dx < 0 ? 1 : -1));
    });
    // A close the page did not ask for (the browser's own cancel) waits like the rest.
    viewer.addEventListener("cancel", function (event) {
      if (window.relaybayAnnotate.holds()) event.preventDefault();
    });
    viewer.addEventListener("close", function () {
      afterViewerClosed();
    });
  }

  // --- paste a screenshot into a review step (Ctrl+V) ---------------------------------------

  function csrfValue() {
    var field = document.querySelector("input[name=csrf_token]");
    return field ? field.value : "";
  }

  document.addEventListener("paste", function (event) {
    var step = event.target.closest && event.target.closest(".review-step[data-paste]");
    if (!step || !event.clipboardData) return;
    var file = null;
    Array.prototype.forEach.call(event.clipboardData.items || [], function (item) {
      if (!file && item.kind === "file" && item.type.indexOf("image/") === 0) {
        file = item.getAsFile();
      }
    });
    if (!file) return; // plain text goes into the comment box as usual
    event.preventDefault();
    var note = step.querySelector(".paste-note");
    if (note) note.textContent = "Uploading the screenshot…";
    fetch(step.dataset.paste, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Relaybay-CSRF": csrfValue(), "Content-Type": file.type },
      body: file
    }).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) throw new Error(text || "HTTP " + response.status);
        var panel = step.closest(".review");
        if (panel) htmx.trigger(panel, "rb:refresh");
      });
    }).catch(function (error) {
      if (note) note.textContent = "Not attached: " + error.message;
    });
  });

  // --- browser notification: "Waiting for your acceptance: <task>" ----------------------------
  // Permission is asked only from the button in Settings → Notifications.

  function notifyAcceptance(data) {
    if (data.to !== "review" && data.to !== "test") return;
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    var title = "Waiting for your acceptance: " + (data.title || data.key);
    try {
      var note = new Notification(title, { body: data.key + " · " + data.to, tag: data.key });
      note.onclick = function () {
        window.focus();
        window.location.href = "/p/" + encodeURIComponent(data.project) + "?task=" +
          encodeURIComponent(data.key) + "&tab=review";
      };
    } catch (e) {
      // Some browsers allow notifications only from a service worker; then none is shown.
    }
  }

  // "Answer received: <task>": a message from a chat or a mailbox for a task waiting on someone.
  function notifyAnswer(data) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      var note = new Notification("Answer received: " + data.key, {
        body: data.key + " · " + (data.source || "intake"),
        tag: "answer-" + data.key
      });
      note.onclick = function () {
        window.focus();
        window.location.href = "/p/" + encodeURIComponent(data.project) + "?task=" +
          encodeURIComponent(data.key);
      };
    } catch (e) {
      // Some browsers allow notifications only from a service worker; then none is shown.
    }
  }

  function showNotifyState() {
    var state = document.querySelector("[data-notify-state]");
    var button = document.querySelector("[data-notify-enable]");
    if (!state) return;
    if (!("Notification" in window)) {
      state.textContent = "This browser does not support notifications.";
      return;
    }
    var texts = {
      granted: "Allowed: this browser shows them while a relaybay tab is open.",
      denied: "Blocked in this browser; allow them in its site settings.",
      "default": "Not allowed yet."
    };
    state.textContent = texts[Notification.permission] || "";
    if (button) button.hidden = Notification.permission !== "default";
  }

  function askNotifications() {
    if (!("Notification" in window)) return;
    Notification.requestPermission().then(showNotifyState);
  }

  // A deferred script runs after parsing but before htmx processes the page on DOMContentLoaded,
  // so the starting id must be read here, not in a DOMContentLoaded handler.
  htmx.createEventSource = createEventSource;
  live = document.getElementById("live");
  if (!live) { setStatus("off"); return; }
  lastId = parseInt(live.dataset.lastEventId || "0", 10) || 0;
  setupDialog();
  setupViewer();
  showNotifyState();
  showCompactButton();
  restoreKept(document);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") reconnect();
  });
  window.addEventListener("online", reconnect);
  window.setInterval(refreshAges, 60000);
})();

// The session picker in the task dialog closes on a click outside it or on Escape.
document.addEventListener("click", (event) => {
  for (const picker of document.querySelectorAll(".bind-picker[open]")) {
    if (!picker.contains(event.target)) picker.removeAttribute("open");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const picker of document.querySelectorAll(".bind-picker[open]")) picker.removeAttribute("open");
});
