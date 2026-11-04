// The Map page: draws /map/graph with Cytoscape and keeps it current. app.js passes each SSE event
// here; a burst of events ends in one refetch, and only the elements that changed are touched.
(function () {
  "use strict";

  var root = document.getElementById("map");
  if (!root) return;

  var REFRESH_MS = 1500;
  var MAX_FIT_ZOOM = 1.4;
  var NOW_TICK_MS = 60000;  // in Now view edges age out of the window without any event
  var KINDS_PREFIXES = ["project.", "task.", "session.", "comment.", "environment."];
  var view = root.dataset.view;
  var projectFilter = root.dataset.project;
  var status = document.getElementById("map-status");
  var detail = document.getElementById("map-detail");
  var graph = null;
  var selected = null;
  var collapsed = new Set();
  var seenProjects = new Set();

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function theme() {
    return {
      text: token("--text"), muted: token("--muted"), surface: token("--surface"),
      border: token("--border"), accent: token("--accent"), ok: token("--ok"),
      warn: token("--warn"), bad: token("--bad"), idle: token("--badge-text"),
      chip: token("--chip-bg"), badge: token("--badge-bg")
    };
  }

  function stylesheet() {
    var t = theme();
    var statusColor = { working: t.ok, waiting: t.warn, idle: t.idle, ended: t.muted };
    var levelColor = { none: t.muted, read: t.ok, ask: t.warn, full: t.bad };
    var style = [
      { selector: "node", style: {
        "label": "data(text)", "color": t.text, "font-size": 11, "text-wrap": "wrap",
        "text-max-width": 150, "text-valign": "bottom", "text-margin-y": 4,
        "background-color": t.surface, "border-width": 2, "border-color": t.border,
        "width": 24, "height": 24, "text-background-color": t.surface,
        "text-background-opacity": 0.75, "text-background-padding": 2,
        "min-zoomed-font-size": 6
      } },
      { selector: "node[kind = 'owner']", style: {
        "shape": "star", "width": 44, "height": 44, "background-color": t.accent,
        "border-color": t.accent, "font-weight": 600
      } },
      { selector: "node[kind = 'project']", style: {
        "shape": "round-rectangle", "width": 44, "height": 30, "background-color": t.badge,
        "border-color": t.accent, "font-weight": 600
      } },
      { selector: "node[kind = 'task']", style: {
        "shape": "round-rectangle", "width": 22, "height": 16, "font-size": 10
      } },
      { selector: "node[kind = 'environment']", style: {
        "shape": "hexagon", "width": 26, "height": 24, "background-color": t.chip
      } },
      { selector: "node[?external]", style: { "border-style": "dashed" } },
      { selector: "node[?waiting_owner]", style: { "border-color": t.warn, "border-width": 4 } },
      { selector: "edge", style: {
        "width": 1.5, "line-color": t.border, "target-arrow-color": t.border,
        "curve-style": "bezier", "arrow-scale": 0.8
      } },
      { selector: "edge[kind = 'binding']", style: {
        "line-color": t.accent, "target-arrow-color": t.accent, "target-arrow-shape": "triangle"
      } },
      { selector: "edge[kind = 'message']", style: {
        "line-color": t.text, "target-arrow-color": t.text, "target-arrow-shape": "vee",
        "line-style": "dashed"
      } },
      { selector: "edge[kind = 'comment']", style: {
        "line-color": t.idle, "target-arrow-color": t.idle, "target-arrow-shape": "circle",
        "line-style": "dotted"
      } },
      { selector: ".dim", style: { "opacity": 0.25 } },
      { selector: "edge.recent", style: { "width": 4, "opacity": 1, "z-index": 5 } },
      { selector: "node:selected", style: { "overlay-color": t.accent, "overlay-opacity": 0.2 } }
    ];
    // Before the waiting_owner rule, which must win over a status colour.
    Object.keys(statusColor).forEach(function (name) {
      style.splice(6, 0, {
        selector: "node[status = '" + name + "']",
        style: { "border-color": statusColor[name], "border-width": 3 }
      });
    });
    Object.keys(levelColor).forEach(function (name) {
      style.push({
        selector: "edge[kind = 'access'][level = '" + name + "']",
        style: { "line-color": levelColor[name], "target-arrow-color": levelColor[name],
                 "target-arrow-shape": "diamond" }
      });
    });
    return style;
  }

  var cy = cytoscape({
    container: document.getElementById("map-canvas"),
    style: stylesheet(),
    minZoom: 0.1,
    maxZoom: 3
  });

  function badges(node) {
    var parts = [];
    if (node.kind === "session" && node.status) parts.push(node.status);
    if (node.kind === "session" && node.external) parts.push("not a known session");
    if (node.kind === "task" && node.stage) parts.push(node.stage);
    if (node.kind === "environment") parts.push(node.env_kind + ": " + (node.level || "unknown"));
    if (node.kind === "project" && collapsed.has(node.slug)) parts.push("collapsed");
    if (node.kind === "project" && !collapsed.has(node.slug)) parts.push(node.open_tasks + " open");
    if (node.unread) parts.push(node.unread + " unread");
    if (node.kind === "owner" && node.waiting_count) parts.push(node.waiting_count + " waiting");
    else if (node.waiting_owner && node.kind !== "owner") parts.push("waiting for you");
    return parts;
  }

  function shorten(text) { return text.length > 40 ? text.slice(0, 39) + "…" : text; }

  function nodeText(node) {
    var parts = badges(node);
    return shorten(node.label) + (parts.length ? "\n" + parts.join(" · ") : "");
  }

  // What is drawn: a collapsed project stands in for its nodes, and the edges that reached them
  // reach the project instead, one per kind.
  function visible() {
    var nodes = {};
    var home = {};
    graph.nodes.forEach(function (node) {
      if (node.kind !== "project" && node.project && collapsed.has(node.project)) {
        home[node.id] = "project:" + node.project;
      } else {
        nodes[node.id] = Object.assign({}, node, { text: nodeText(node) });
      }
    });
    var edges = {};
    graph.edges.forEach(function (edge) {
      var source = home[edge.source] || edge.source;
      var target = home[edge.target] || edge.target;
      if (source === target || !nodes[source] || !nodes[target]) return;
      var moved = source !== edge.source || target !== edge.target;
      var id = moved ? edge.kind + ":" + source + ">" + target : edge.id;
      var known = edges[id];
      if (known) {
        known.recent = known.recent || edge.recent;
        return;
      }
      edges[id] = Object.assign({}, edge, { id: id, source: source, target: target });
    });
    return { nodes: nodes, edges: edges };
  }

  // A view the user panned or zoomed stays theirs when a live event adds a node.
  var userMoved = false;
  var laidOut = false;
  cy.on("dragpan pinchzoom scrollzoom", function () { userMoved = true; });

  function fitAll() {
    cy.fit(undefined, 30);
    if (cy.zoom() > MAX_FIT_ZOOM) {
      cy.zoom(MAX_FIT_ZOOM);
      cy.center();
    }
  }

  function layout() {
    var run = cy.layout({
      name: "cose", animate: false, randomize: !laidOut, fit: false,
      boundingBox: { x1: 0, y1: 0, w: cy.width(), h: cy.height() },
      nodeRepulsion: function () { return 9000; },
      idealEdgeLength: function () { return 70; },
      nodeDimensionsIncludeLabels: true
    });
    run.one("layoutstop", function () {
      laidOut = true;
      if (!userMoved) fitAll();
    });
    run.run();
  }

  function sameData(element, data) {
    var old = element.data();
    return Object.keys(data).every(function (key) { return old[key] === data[key]; });
  }

  function markNow() {
    if (view !== "now") return;
    cy.batch(function () {
      var active = cy.collection();
      cy.edges().forEach(function (edge) {
        edge.toggleClass("recent", !!edge.data("recent"));
        edge.toggleClass("dim", !edge.data("recent"));
        if (edge.data("recent")) active = active.union(edge.connectedNodes());
      });
      cy.nodes().forEach(function (node) { node.toggleClass("dim", !active.has(node)); });
    });
  }

  function draw() {
    var want = visible();
    var added = false;
    cy.batch(function () {
      cy.elements().forEach(function (element) {
        var data = element.isNode() ? want.nodes[element.id()] : want.edges[element.id()];
        if (!data) element.remove();
      });
      Object.keys(want.nodes).forEach(function (id) {
        var found = cy.getElementById(id);
        if (found.nonempty()) {
          if (!sameData(found, want.nodes[id])) found.data(want.nodes[id]);
          return;
        }
        cy.add({ group: "nodes", data: want.nodes[id] });
        added = true;
      });
      Object.keys(want.edges).forEach(function (id) {
        var found = cy.getElementById(id);
        if (found.nonempty()) {
          if (!sameData(found, want.edges[id])) found.data(want.edges[id]);
          return;
        }
        cy.add({ group: "edges", data: want.edges[id] });
        added = true;
      });
    });
    markNow();
    if (added) layout();
    showDetail();
    var recent = graph.edges.filter(function (edge) { return edge.recent; }).length;
    status.textContent = graph.nodes.length + " nodes, " + graph.edges.length + " edges" +
      (view === "now" ? "; " + recent + " active in the last " + graph.minutes + " min" : "") +
      (collapsed.size ? "; " + collapsed.size + " project(s) collapsed" : "") + ".";
  }

  function fetchGraph() {
    return fetch(root.dataset.graphUrl, { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        graph = data;
        graph.nodes.forEach(function (node) {
          if (node.kind !== "project" || seenProjects.has(node.slug)) return;
          seenProjects.add(node.slug);
          if (graph.collapsed) collapsed.add(node.slug);
        });
        draw();
      })
      .catch(function (error) {
        status.textContent = "Could not load the map: " + error.message;
      });
  }

  // --- selected node --------------------------------------------------------------------------

  function selectedNode() {
    if (!selected || !graph) return null;
    return graph.nodes.find(function (node) { return node.id === selected; }) || null;
  }

  function showDetail() {
    var node = selectedNode();
    detail.hidden = !node;
    if (!node) return;
    document.getElementById("map-detail-title").textContent = node.label;
    var parts = badges(node);
    if (node.activity) parts.push(node.activity);
    document.getElementById("map-detail-text").textContent = parts.join(" · ");
    document.getElementById("map-detail-link").href = node.href;
    var usage = document.getElementById("map-detail-usage");
    usage.hidden = !node.usage_href;
    if (node.usage_href) usage.href = node.usage_href;
    var toggle = document.getElementById("map-detail-toggle");
    toggle.hidden = node.kind !== "project";
    toggle.textContent = collapsed.has(node.slug) ? "Expand" : "Collapse";
  }

  cy.on("tap", "node", function (event) {
    selected = event.target.id();
    showDetail();
  });
  cy.on("tap", function (event) {
    if (event.target === cy) { selected = null; showDetail(); }
  });
  cy.on("dbltap", "node[kind = 'project']", function (event) {
    toggle(event.target.data("slug"));
  });

  function relayout() {
    userMoved = false;
    draw();
    layout();
  }

  function toggle(slug) {
    if (collapsed.has(slug)) collapsed.delete(slug); else collapsed.add(slug);
    relayout();
  }

  document.getElementById("map-detail-toggle").addEventListener("click", function () {
    var node = selectedNode();
    if (node && node.kind === "project") toggle(node.slug);
  });
  document.getElementById("map-collapse").addEventListener("click", function () {
    seenProjects.forEach(function (slug) { collapsed.add(slug); });
    relayout();
  });
  document.getElementById("map-expand").addEventListener("click", function () {
    collapsed.clear();
    relayout();
  });

  // --- live -----------------------------------------------------------------------------------

  var refreshTimer = null;
  function refreshSoon() {
    if (refreshTimer !== null) return;
    refreshTimer = window.setTimeout(function () {
      refreshTimer = null;
      fetchGraph();
    }, REFRESH_MS);
  }

  window.relaybayMap = {
    onEvent: function (data) {
      var relevant = KINDS_PREFIXES.some(function (p) { return data.kind.indexOf(p) === 0; }) ||
        data.kind === "live.action" || data.kind === "policy.updated";
      // An event without a project (a session's own, a global policy) can touch any project.
      if (relevant && (!projectFilter || !data.project || data.project === projectFilter)) {
        refreshSoon();
      }
    }
  };

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    cy.style(stylesheet());
  });
  if (view === "now") window.setInterval(refreshSoon, NOW_TICK_MS);
  window.addEventListener("resize", function () { cy.resize(); });
  fetchGraph();
})();
