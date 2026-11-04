// Markup on a screenshot in the viewer: an SVG layer over the image, in the image's own pixels, so
// it scales with it; geometry is stored in fractions of the image size. Agent text only ever
// reaches the page through textContent.
window.relaybayAnnotate = (function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var STROKE = 0.004; // of the image's width; the server's DEFAULT_STROKE
  var PIN_RADIUS = 0.014; // of the image's larger side
  var MIN_SIZE = 0.005; // a click, not a drag
  var POINTS_MAX = 4000; // below the server's limit; the server simplifies the line

  var viewer, img, svg, tools, thread, note;
  var attachment = null; // the id of the screenshot on screen
  var visit = 0; // grows on every change of screenshot: an answer from an earlier visit is not applied
  var sending = false;
  var failed = ""; // a write refused after the screen moved on, shown on the next screenshot
  var annotations = [];
  var tool = "select";
  var colour = null;
  var selected = null;
  var openPin = null;
  var drag = null;
  var saving = []; // finished strokes on their way to the server, still on screen
  var undo = [];
  var writes = Promise.resolve(); // one write at a time, in order
  var loadToken = 0;

  function say(text) { note.textContent = text || ""; }

  function errorText(text, status) {
    var detail = text;
    try { detail = JSON.parse(text).detail || text; } catch (e) { /* plain text, as it is */ }
    return detail || "HTTP " + status;
  }

  // `shot` is the screenshot the action was made on, fixed when it was queued.
  function call(shot, path, options) {
    var settings = options || {};
    var headers = { "X-Relaybay-CSRF": viewer.dataset.csrf };
    var body = settings.body;
    if (body !== undefined && !(body instanceof Blob)) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    } else if (body instanceof Blob) {
      headers["Content-Type"] = body.type;
    }
    return fetch("/files/" + shot + path, {
      method: settings.method || "POST",
      credentials: "same-origin",
      headers: headers,
      body: body
    }).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) throw new Error(errorText(text, response.status));
        return text ? JSON.parse(text) : null;
      });
    });
  }

  // Runs after the writes before it; a failure is shown and does not stop the next one.
  function write(step) {
    var result = writes.then(step);
    writes = result.catch(function (error) {
      failed = "Not saved: " + error.message;
      say(failed);
    });
    return result;
  }

  // The screen as the action sees it: its screenshot and visit, fixed when the action starts.
  function here() { return { shot: attachment, visit: visit }; }
  function stillOn(at) { return at.visit === visit; }

  function fetchAnnotations(shot) {
    return fetch("/files/" + shot + "/annotations", { credentials: "same-origin" })
      .then(function (response) {
        return response.text().then(function (text) {
          if (!response.ok) throw new Error(errorText(text, response.status));
          return JSON.parse(text).annotations;
        });
      });
  }

  // --- drawing: the same shapes on the SVG layer and on the flattening canvas -----------------

  function sizes() {
    return { w: img.naturalWidth, h: img.naturalHeight };
  }

  function pinRadius(s) { return Math.max(s.w, s.h) * PIN_RADIUS; }

  function arrowHead(x1, y1, x2, y2, width) {
    var size = Math.max(width * 4, 8);
    var angle = Math.atan2(y2 - y1, x2 - x1);
    return [
      [x2, y2],
      [x2 - size * Math.cos(angle - 0.45), y2 - size * Math.sin(angle - 0.45)],
      [x2 - size * Math.cos(angle + 0.45), y2 - size * Math.sin(angle + 0.45)]
    ];
  }

  function node(name, attrs) {
    var el = document.createElementNS(SVG, name);
    Object.keys(attrs).forEach(function (key) { el.setAttribute(key, attrs[key]); });
    return el;
  }

  function outline(item, s) {
    var g = item.geometry;
    if (item.kind === "rect") {
      return node("rect", { x: g.x * s.w, y: g.y * s.h, width: g.w * s.w, height: g.h * s.h });
    }
    if (item.kind === "arrow") {
      return node("line", { x1: g.x1 * s.w, y1: g.y1 * s.h, x2: g.x2 * s.w, y2: g.y2 * s.h });
    }
    return node("polyline", {
      points: g.points.map(function (p) { return (p[0] * s.w) + "," + (p[1] * s.h); }).join(" ")
    });
  }

  function shapeNode(item, s) {
    var group = node("g", { "data-annotation": item.id });
    var width = (item.stroke_width || STROKE) * s.w;
    if (item.id === selected) group.setAttribute("class", "is-selected");
    if (item.kind === "pin") {
      var r = pinRadius(s);
      group.appendChild(node("circle", {
        cx: item.geometry.x * s.w, cy: item.geometry.y * s.h, r: r,
        fill: item.colour, stroke: "#fff", "stroke-width": r * 0.15
      }));
      var label = node("text", {
        x: item.geometry.x * s.w, y: item.geometry.y * s.h, fill: "#fff",
        "font-size": r * 1.1, "font-weight": "700", "font-family": "sans-serif",
        "text-anchor": "middle", "dominant-baseline": "central"
      });
      label.textContent = String(item.pin_number);
      group.appendChild(label);
      return group;
    }
    // A wide invisible stroke under the line makes it easy to hit with a finger.
    var hit = outline(item, s);
    hit.setAttribute("fill", "none");
    hit.setAttribute("stroke", "transparent");
    hit.setAttribute("stroke-width", Math.max(width * 5, s.w * 0.015));
    hit.setAttribute("pointer-events", "stroke");
    group.appendChild(hit);
    var line = outline(item, s);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", item.colour);
    line.setAttribute("stroke-width", width);
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("stroke-linejoin", "round");
    group.appendChild(line);
    if (item.kind === "arrow") {
      var g = item.geometry;
      var head = arrowHead(g.x1 * s.w, g.y1 * s.h, g.x2 * s.w, g.y2 * s.h, width);
      group.appendChild(node("polygon", {
        points: head.map(function (p) { return p.join(","); }).join(" "), fill: item.colour
      }));
    }
    return group;
  }

  function paint() {
    if (!img.naturalWidth) return;
    var s = sizes();
    svg.setAttribute("viewBox", "0 0 " + s.w + " " + s.h);
    svg.textContent = "";
    annotations.forEach(function (item) { svg.appendChild(shapeNode(item, s)); });
    saving.forEach(function (item) { svg.appendChild(shapeNode(item, s)); });
    if (drag && drag.preview) svg.appendChild(shapeNode(drag.preview, s));
  }

  function drawOnCanvas(ctx, item, s) {
    var g = item.geometry;
    var width = (item.stroke_width || STROKE) * s.w;
    ctx.strokeStyle = item.colour;
    ctx.fillStyle = item.colour;
    ctx.lineWidth = width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (item.kind === "rect") {
      ctx.strokeRect(g.x * s.w, g.y * s.h, g.w * s.w, g.h * s.h);
    } else if (item.kind === "arrow") {
      ctx.beginPath();
      ctx.moveTo(g.x1 * s.w, g.y1 * s.h);
      ctx.lineTo(g.x2 * s.w, g.y2 * s.h);
      ctx.stroke();
      var head = arrowHead(g.x1 * s.w, g.y1 * s.h, g.x2 * s.w, g.y2 * s.h, width);
      ctx.beginPath();
      ctx.moveTo(head[0][0], head[0][1]);
      ctx.lineTo(head[1][0], head[1][1]);
      ctx.lineTo(head[2][0], head[2][1]);
      ctx.closePath();
      ctx.fill();
    } else if (item.kind === "freehand") {
      ctx.beginPath();
      g.points.forEach(function (p, i) {
        if (i === 0) ctx.moveTo(p[0] * s.w, p[1] * s.h);
        else ctx.lineTo(p[0] * s.w, p[1] * s.h);
      });
      ctx.stroke();
    } else {
      var r = pinRadius(s);
      ctx.beginPath();
      ctx.arc(g.x * s.w, g.y * s.h, r, 0, 2 * Math.PI);
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = r * 0.15;
      ctx.stroke();
      ctx.fillStyle = "#fff";
      ctx.font = "700 " + (r * 1.1) + "px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(item.pin_number), g.x * s.w, g.y * s.h);
    }
  }

  // The image and the markup at the image's own resolution, whatever is on screen.
  function flatten() {
    return new Promise(function (resolve, reject) {
      if (!img.complete || !img.naturalWidth) {
        reject(new Error("the image has not loaded"));
        return;
      }
      var s = sizes();
      var canvas = document.createElement("canvas");
      canvas.width = s.w;
      canvas.height = s.h;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(img, 0, 0, s.w, s.h);
      annotations.forEach(function (item) { drawOnCanvas(ctx, item, s); });
      canvas.toBlob(function (blob) {
        if (blob) resolve(blob);
        else reject(new Error("the browser could not make the image"));
      }, "image/png");
    });
  }

  // --- pointer: mouse, pen and touch alike ---------------------------------------------------

  function fractionAt(event) {
    var matrix = svg.getScreenCTM();
    if (!matrix) return null;
    var point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    var s = sizes();
    return {
      x: Math.min(1, Math.max(0, point.x / s.w)),
      y: Math.min(1, Math.max(0, point.y / s.h))
    };
  }

  function round(value) { return Math.round(value * 100000) / 100000; }

  function geometryOf(state) {
    var a = state.start;
    var b = state.last;
    if (state.kind === "rect") {
      return {
        x: round(Math.min(a.x, b.x)), y: round(Math.min(a.y, b.y)),
        w: round(Math.abs(a.x - b.x)), h: round(Math.abs(a.y - b.y))
      };
    }
    if (state.kind === "arrow") return { x1: round(a.x), y1: round(a.y), x2: round(b.x), y2: round(b.y) };
    return { points: state.points };
  }

  function bigEnough(kind, geometry) {
    if (kind === "rect") return geometry.w >= MIN_SIZE && geometry.h >= MIN_SIZE;
    if (kind === "arrow") {
      return Math.hypot(geometry.x2 - geometry.x1, geometry.y2 - geometry.y1) >= MIN_SIZE * 2;
    }
    return geometry.points.length >= 2;
  }

  function hitAnnotation(target) {
    var group = target.closest && target.closest("[data-annotation]");
    if (!group) return null;
    var id = parseInt(group.getAttribute("data-annotation"), 10);
    return annotations.filter(function (a) { return a.id === id; })[0] || null;
  }

  function onPointerDown(event) {
    if (!event.isPrimary || attachment === null || !img.naturalWidth || sending) return;
    var hit = hitAnnotation(event.target);
    if (hit && hit.kind === "pin") {
      selected = hit.id;
      openThread(hit);
      paint();
      return;
    }
    if (tool === "select") {
      selected = hit ? hit.id : null;
      if (!hit) closeThread();
      paint();
      return;
    }
    var start = fractionAt(event);
    if (!start) return;
    event.preventDefault();
    if (tool === "pin") {
      addAnnotation({ kind: "pin", colour: colour, geometry: { x: round(start.x), y: round(start.y) } })
        .then(function (added) { if (added) openThread(added, true); });
      return;
    }
    svg.setPointerCapture(event.pointerId);
    drag = {
      id: event.pointerId,
      kind: tool,
      colour: colour,
      start: start,
      last: start,
      points: [[round(start.x), round(start.y)]]
    };
  }

  function onPointerMove(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var at = fractionAt(event);
    if (!at) return;
    drag.last = at;
    if (drag.kind === "freehand" && drag.points.length < POINTS_MAX) {
      drag.points.push([round(at.x), round(at.y)]);
    }
    var geometry = geometryOf(drag);
    drag.preview = bigEnough(drag.kind, geometry)
      ? { id: -1, kind: drag.kind, colour: drag.colour, stroke_width: STROKE, geometry: geometry }
      : null;
    paint();
  }

  function onPointerEnd(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var done = drag;
    // Over before the server answers: the next stroke may start at once.
    drag = null;
    var geometry = geometryOf(done);
    if (event.type === "pointercancel" || !bigEnough(done.kind, geometry)) {
      paint();
      return;
    }
    var shown = done.preview;
    saving.push(shown);
    addAnnotation({ kind: done.kind, colour: done.colour, stroke_width: STROKE, geometry: geometry })
      .then(function () {
        saving = saving.filter(function (item) { return item !== shown; });
        paint();
      });
  }

  // --- writes ---------------------------------------------------------------------------------

  // Resolves with the new annotation, or undefined when it was refused (the note says why).
  function addAnnotation(values) {
    var at = here();
    return write(function () { return call(at.shot, "/annotations", { body: values }); })
      .then(function (added) {
        if (!stillOn(at)) return undefined;
        annotations.push(added);
        undo.push({ op: "add", id: added.id });
        selected = added.id;
        say("");
        paint();
        return added;
      })
      .catch(function () { return undefined; });
  }

  function removeAnnotation(id, remember) {
    var at = here();
    return write(function () { return call(at.shot, "/annotations/" + id + "/delete"); }).then(function () {
      if (!stillOn(at)) return;
      var gone = annotations.filter(function (a) { return a.id === id; })[0];
      annotations = annotations.filter(function (a) { return a.id !== id; });
      if (remember && gone) undo.push({ op: "remove", id: id });
      if (selected === id) selected = null;
      if (openPin === id) closeThread();
      paint();
    }).catch(function () { /* write() has shown it */ });
  }

  function restoreAnnotation(id) {
    var at = here();
    return write(function () { return call(at.shot, "/annotations/" + id + "/restore"); }).then(function (item) {
      if (!stillOn(at)) return;
      annotations.push(item);
      annotations.sort(function (a, b) { return a.id - b.id; });
      paint();
    }).catch(function () { /* write() has shown it */ });
  }

  function undoLast() {
    var last = undo.pop();
    if (!last) { say("Nothing to undo."); return; }
    if (last.op === "add") removeAnnotation(last.id, false);
    else restoreAnnotation(last.id);
  }

  function replaceAnnotation(item) {
    annotations = annotations.map(function (a) { return a.id === item.id ? item : a; });
  }

  // --- a pin's thread ---------------------------------------------------------------------------

  function threadPart(name) { return thread.querySelector(".viewer-thread-" + name); }

  function showReplies(item) {
    var list = threadPart("replies");
    list.textContent = "";
    item.replies.forEach(function (reply) {
      var row = document.createElement("li");
      var who = document.createElement("strong");
      who.textContent = reply.author + ": ";
      row.appendChild(who);
      row.appendChild(document.createTextNode(reply.body));
      list.appendChild(row);
    });
  }

  function openThread(item, focus) {
    openPin = item.id;
    threadPart("title").textContent = "Pin #" + item.pin_number;
    threadPart("body").value = item.body || "";
    threadPart("reply").value = "";
    showReplies(item);
    // Beside the pin, not over it.
    thread.classList.toggle("is-left", item.geometry.x > 0.5);
    thread.hidden = false;
    if (focus) threadPart("body").focus();
  }

  function closeThread() {
    openPin = null;
    thread.hidden = true;
  }

  // Resolves with whether the comment is saved; a refusal is on screen by then.
  function saveBody() {
    var id = openPin;
    if (id === null || attachment === null) return Promise.resolve(true);
    var at = here();
    var text = threadPart("body").value;
    return write(function () { return call(at.shot, "/annotations/" + id, { body: { body: text } }); })
      .then(function (item) {
        if (stillOn(at)) {
          replaceAnnotation(item);
          say("Comment saved.");
        }
        return true;
      })
      .catch(function () { return false; });
  }

  function sendReply() {
    var id = openPin;
    if (id === null || attachment === null) return;
    var at = here();
    var box = threadPart("reply");
    var text = box.value;
    write(function () { return call(at.shot, "/annotations/" + id + "/replies", { body: { body: text } }); })
      .then(function (item) {
        if (!stillOn(at)) return;
        replaceAnnotation(item);
        box.value = "";
        if (openPin === item.id) showReplies(item);
      })
      .catch(function () { /* write() has shown it */ });
  }

  // --- Send to agent ----------------------------------------------------------------------------

  function send(button) {
    if (attachment === null) return;
    var at = here();
    sending = true;
    button.disabled = true;
    say("Sending…");
    // A pin comment still in its box counts: the agent gets what the owner sees.
    saveBody().then(function (saved) {
      if (!saved) return undefined;
      say("Sending…");
      // Paging and closing wait for `sending`, so the image on screen is this screenshot's. The
      // markup comes fresh, after every write queued before: the PNG shows what the server holds.
      return write(function () {
        return fetchAnnotations(at.shot)
          .then(function (list) {
            annotations = list;
            paint();
            return flatten();
          })
          .then(function (blob) { return call(at.shot, "/annotated", { body: blob }); })
          .then(function () { return call(at.shot, "/send-annotations"); });
      }).then(function (answer) {
        say(answer.comment ? "Sent to the agent as a comment." : "Already sent; nothing changed.");
      }).catch(function (error) {
        say("Not sent: " + error.message);
      });
    }).finally(function () {
      sending = false;
      button.disabled = false;
    });
  }

  // --- toolbar and the viewer's calls -------------------------------------------------------------

  function setTool(name) {
    tool = name;
    tools.querySelectorAll("[data-tool]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.tool === name));
    });
    svg.classList.toggle("is-drawing", name !== "select");
  }

  function setColour(value) {
    colour = value;
    tools.querySelectorAll("[data-colour]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.colour === value));
    });
  }

  function onToolbar(event) {
    var button = event.target.closest("button");
    if (!button) return;
    if (sending) { say("Sending… one moment."); return; }
    if (button.dataset.tool) setTool(button.dataset.tool);
    else if (button.dataset.colour) setColour(button.dataset.colour);
    else if (button.dataset.action === "undo") undoLast();
    else if (button.dataset.action === "delete") deleteSelected();
    else if (button.dataset.action === "send") send(button);
    else if (button.dataset.action === "hide") {
      svg.hidden = !svg.hidden;
      button.setAttribute("aria-pressed", String(svg.hidden));
      button.textContent = svg.hidden ? "Show markup" : "Hide markup";
    }
  }

  function deleteSelected() {
    if (selected === null) { say("Select a shape or a pin first."); return; }
    removeAnnotation(selected, true);
  }

  // After the writes queued so far, so a stroke just made on this screenshot is in the answer.
  function load() {
    var token = ++loadToken;
    var shot = attachment;
    return writes
      .then(function () { return fetchAnnotations(shot); })
      .then(function (list) {
        if (token !== loadToken) return;
        annotations = list;
        var pin = annotations.filter(function (a) { return a.id === openPin; })[0];
        if (pin) showReplies(pin);
        else if (openPin !== null) closeThread();
        paint();
      })
      .catch(function (error) {
        if (token === loadToken) say("Markup not loaded: " + error.message);
      });
  }

  function show(link) {
    var id = parseInt(link.dataset.attachment, 10);
    attachment = isNaN(id) ? null : id;
    visit += 1;
    annotations = [];
    undo = [];
    selected = null;
    drag = null;
    saving = [];
    closeThread();
    say(failed);
    failed = "";
    svg.textContent = "";
    tools.hidden = attachment === null;
    if (attachment !== null) load();
  }

  function hide() {
    attachment = null;
    visit += 1;
    loadToken += 1;
    annotations = [];
    undo = [];
    selected = null;
    drag = null;
    saving = [];
    closeThread();
    svg.textContent = "";
    setTool("select");
  }

  // True when the viewer must not treat this key as its own (typing in a pin's boxes).
  function onKey(event) {
    var typing = event.target.closest && event.target.closest("textarea");
    if (typing) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        typing.blur();
        closeThread();
      } else {
        event.stopPropagation();
      }
      return true;
    }
    if (sending) return false;
    if (event.key === "Delete" && selected !== null) {
      event.preventDefault();
      deleteSelected();
      return true;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      undoLast();
      return true;
    }
    return false;
  }

  function onEvent(data) {
    if (attachment !== null && data.attachment_id === attachment) load();
  }

  // True while a Send runs: paging and closing wait; the note says why.
  function holds() {
    if (sending) say("Sending… one moment.");
    return sending;
  }

  // A swipe pages between screenshots only while no drawing tool is on.
  function takesTouch() {
    return attachment !== null && tool !== "select" && !svg.hidden;
  }

  function setup(root) {
    viewer = root;
    img = root.querySelector(".viewer-img");
    svg = root.querySelector(".viewer-svg");
    tools = root.querySelector(".viewer-tools");
    thread = root.querySelector(".viewer-thread");
    note = root.querySelector(".viewer-note");
    colour = tools.querySelector("[data-colour]").dataset.colour;
    img.addEventListener("load", paint);
    svg.addEventListener("pointerdown", onPointerDown);
    svg.addEventListener("pointermove", onPointerMove);
    svg.addEventListener("pointerup", onPointerEnd);
    svg.addEventListener("pointercancel", onPointerEnd);
    tools.addEventListener("click", onToolbar);
    // Leaving the box keeps what was typed: Esc, Close and paging all pass through a blur.
    threadPart("body").addEventListener("change", saveBody);
    thread.addEventListener("click", function (event) {
      if (sending) return;
      if (event.target.closest(".viewer-thread-close")) closeThread();
      else if (event.target.closest(".viewer-thread-save")) saveBody();
      else if (event.target.closest(".viewer-thread-send")) sendReply();
    });
  }

  return {
    setup: setup,
    show: show,
    hide: hide,
    onKey: onKey,
    onEvent: onEvent,
    takesTouch: takesTouch,
    holds: holds
  };
})();
