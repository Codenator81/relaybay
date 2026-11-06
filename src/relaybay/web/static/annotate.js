// Markup on a screenshot in the viewer: an SVG layer over the image, in the image's own pixels, so
// it scales with it; geometry is stored in fractions of the image size. Zoom is one CSS transform
// on the box that holds both. Agent text only ever reaches the page through textContent.
window.relaybayAnnotate = (function () {
  "use strict";

  var SVG = "http://www.w3.org/2000/svg";
  var STROKE = 0.004; // of the image's width; the server's DEFAULT_STROKE
  var PIN_RADIUS = 0.014; // of the image's larger side
  var MIN_SIZE = 0.005; // a click, not a drag
  var POINTS_MAX = 4000; // below the server's limit; the server simplifies the line
  var ZOOM_MAX = 8; // times the natural size
  var ZOOM_STEP = 1.25; // one key press, button or wheel notch
  var TAP_SLOP = 10; // CSS px a finger may move and still place a pin

  var viewer, stage, zoomBox, zoomLevel, img, svg, tools, thread, note;
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
  var view = { k: 1, x: 0, y: 0 }; // the zoom: see zoomMath
  var panning = null;
  var pinch = null;
  var touches = {}; // touch pointers down on the stage, by id: their last stage point
  var spaceHeld = false;
  var tap = null; // a finger down with the Pin tool: the pin waits for its lift

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

  // --- zoom ----------------------------------------------------------------------------------

  // Pure math in the stage's CSS pixels. `k` scales the fitted picture (1 = fit), (x, y) moves
  // the top-left corner of the fitted box; the transform origin is that corner.
  var zoomMath = {
    // 8x the natural size at most, never below fit; `fit` is screen pixels per image pixel.
    clampScale: function (k, fit) {
      return Math.min(Math.max(1, ZOOM_MAX / fit), Math.max(1, k));
    },
    // Scale to `k` keeping the picture's point under (px, py) in place.
    around: function (from, k, px, py) {
      var ratio = k / from.k;
      return { k: k, x: px - (px - from.x) * ratio, y: py - (py - from.y) * ratio };
    },
    // Along one axis: a picture longer than the stage covers it, a shorter one is centred.
    axis: function (offset, k, stage, picture) {
      var edge = ((stage - picture) / 2) * k; // the letterbox before the picture, scaled
      var length = picture * k;
      if (length <= stage) return (stage - length) / 2 - edge;
      return Math.min(-edge, Math.max(stage - length - edge, offset));
    },
    clampPan: function (next, box) {
      return {
        k: next.k,
        x: zoomMath.axis(next.x, next.k, box.width, box.pictureWidth),
        y: zoomMath.axis(next.y, next.k, box.height, box.pictureHeight)
      };
    }
  };

  function stageBox() {
    var w = img.naturalWidth;
    var h = img.naturalHeight;
    var width = stage.clientWidth;
    var height = stage.clientHeight;
    if (!w || !h || !width || !height) return null;
    var fit = Math.min(width / w, height / h);
    return { width: width, height: height, fit: fit, pictureWidth: w * fit, pictureHeight: h * fit };
  }

  function setView(next) {
    var box = stageBox();
    var before = view.k;
    view = box
      ? zoomMath.clampPan({ k: zoomMath.clampScale(next.k, box.fit), x: next.x, y: next.y }, box)
      : { k: 1, x: 0, y: 0 };
    zoomBox.style.transform = view.k === 1 ? ""
      : "translate(" + view.x + "px, " + view.y + "px) scale(" + view.k + ")";
    stage.classList.toggle("is-zoomed", view.k > 1);
    zoomLevel.textContent = box ? Math.round(view.k * box.fit * 100) + "%" : "";
    if (view.k !== before) paint(); // pins keep their size on screen
  }

  function stagePoint(clientX, clientY) {
    var rect = stage.getBoundingClientRect();
    return { x: clientX - rect.left, y: clientY - rect.top };
  }

  // Around a stage point, or its middle.
  function zoomTo(k, at) {
    var box = stageBox();
    if (!box) return;
    var point = at || { x: box.width / 2, y: box.height / 2 };
    setView(zoomMath.around(view, zoomMath.clampScale(k, box.fit), point.x, point.y));
  }

  function zoomFit() { setView({ k: 1, x: 0, y: 0 }); }

  function zoomActual(at) {
    var box = stageBox();
    if (box) zoomTo(1 / box.fit, at);
  }

  function onZoomBar(event) {
    var button = event.target.closest("button[data-zoom]");
    if (!button) return;
    if (event.detail > 0) stage.focus({ preventScroll: true }); // as in onToolbar
    var action = button.dataset.zoom;
    if (action === "in") zoomTo(view.k * ZOOM_STEP);
    else if (action === "out") zoomTo(view.k / ZOOM_STEP);
    else if (action === "fit") zoomFit();
    else zoomActual();
  }

  function onWheel(event) {
    if (event.target.closest(".viewer-thread")) return; // the thread scrolls
    event.preventDefault();
    var pixels = event.deltaY * (event.deltaMode === 1 ? 33 : event.deltaMode === 2 ? 400 : 1);
    // 100 px, a usual notch, is one step.
    zoomTo(view.k * Math.pow(ZOOM_STEP, -pixels / 100), stagePoint(event.clientX, event.clientY));
  }

  function onDoubleClick(event) {
    if (event.target.closest(".viewer-thread") || hitAnnotation(event.target)) return;
    if (tool !== "select" && !svg.hidden) return;
    if (view.k > 1) zoomFit();
    else zoomActual(stagePoint(event.clientX, event.clientY));
  }

  function startPan(event) {
    panning = { id: event.pointerId, x: event.clientX, y: event.clientY, from: view };
    stage.setPointerCapture(event.pointerId);
    stage.classList.add("is-panning");
  }

  function endPan() {
    panning = null;
    stage.classList.remove("is-panning");
  }

  function touchList() {
    return Object.keys(touches).map(function (id) { return touches[id]; });
  }

  function pinchShape(points) {
    return {
      d: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y) || 1,
      mid: { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 }
    };
  }

  // A second finger: a stroke or a pan under way gives way to the pinch.
  function startPinch() {
    drag = null;
    tap = null;
    endPan();
    var shape = pinchShape(touchList());
    pinch = { d: shape.d, mid: shape.mid, from: view };
    paint();
  }

  // The picture's point under the first midpoint follows the fingers: zoom and pan in one.
  function movePinch() {
    var box = stageBox();
    if (!box) return;
    var now = pinchShape(touchList());
    var k = zoomMath.clampScale(pinch.from.k * now.d / pinch.d, box.fit);
    var next = zoomMath.around(pinch.from, k, pinch.mid.x, pinch.mid.y);
    setView({ k: k, x: next.x + now.mid.x - pinch.mid.x, y: next.y + now.mid.y - pinch.mid.y });
  }

  function setSpace(held) {
    spaceHeld = held;
    stage.classList.toggle("is-space", held);
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
      var r = pinRadius(s) / view.k;
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

  // From the layer's box on screen, zoom included ("xMidYMid meet" centres the picture in it);
  // not getScreenCTM, whose handling of CSS transforms on HTML parents varies by engine.
  function fractionAt(event) {
    var rect = svg.getBoundingClientRect();
    var s = sizes();
    var scale = Math.min(rect.width / s.w, rect.height / s.h);
    if (!scale) return null;
    var left = rect.left + (rect.width - s.w * scale) / 2;
    var top = rect.top + (rect.height - s.h * scale) / 2;
    return {
      x: Math.min(1, Math.max(0, (event.clientX - left) / (s.w * scale))),
      y: Math.min(1, Math.max(0, (event.clientY - top) / (s.h * scale)))
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

  // On the stage: the picture pans (the select tool, the space bar, the middle button) and
  // pinches; the markup layer draws and selects.
  function onPointerDown(event) {
    if (event.target.closest(".viewer-thread")) return;
    if (event.pointerType === "touch") {
      touches[event.pointerId] = stagePoint(event.clientX, event.clientY);
      // Its lift then reaches the stage wherever it happens (lostpointercapture otherwise).
      stage.setPointerCapture(event.pointerId);
      if (touchList().length === 2) { startPinch(); return; }
    }
    if (!event.isPrimary || pinch) return;
    if (event.button === 1 || spaceHeld) {
      event.preventDefault();
      startPan(event);
      return;
    }
    if (svg.hidden || !svg.contains(event.target)) {
      startPan(event);
      return;
    }
    if (attachment === null || !img.naturalWidth || sending) return;
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
      startPan(event);
      return;
    }
    var start = fractionAt(event);
    if (!start) return;
    event.preventDefault();
    if (tool === "pin") {
      // A finger may be the first of a pinch: the pin waits for a lift without one.
      if (event.pointerType === "touch") {
        tap = { id: event.pointerId, start: start, x: event.clientX, y: event.clientY };
      } else {
        placePin(start);
      }
      return;
    }
    stage.setPointerCapture(event.pointerId);
    drag = {
      id: event.pointerId,
      kind: tool,
      colour: colour,
      start: start,
      last: start,
      points: [[round(start.x), round(start.y)]]
    };
  }

  function placePin(at) {
    addAnnotation({ kind: "pin", colour: colour, geometry: { x: round(at.x), y: round(at.y) } })
      .then(function (added) { if (added) openThread(added, true); });
  }

  function onPointerMove(event) {
    if (tap && event.pointerId === tap.id
        && Math.hypot(event.clientX - tap.x, event.clientY - tap.y) > TAP_SLOP) {
      tap = null;
    }
    var touch = touches[event.pointerId];
    if (touch) touches[event.pointerId] = stagePoint(event.clientX, event.clientY);
    if (pinch) {
      if (touch) movePinch();
      return;
    }
    if (panning && event.pointerId === panning.id) {
      setView({
        k: panning.from.k,
        x: panning.from.x + event.clientX - panning.x,
        y: panning.from.y + event.clientY - panning.y
      });
      return;
    }
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

  function dropTouch(id) {
    delete touches[id];
    if (pinch && touchList().length < 2) pinch = null;
  }

  function onPointerEnd(event) {
    var wasPinch = pinch !== null;
    dropTouch(event.pointerId);
    if (wasPinch) return;
    if (tap && event.pointerId === tap.id) {
      var at = tap.start;
      tap = null;
      if (event.type !== "pointercancel") placePin(at);
      return;
    }
    if (panning && event.pointerId === panning.id) {
      endPan();
      return;
    }
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
    // Clicked with a pointer (detail > 0): focus moves to the picture, so Space pans instead of
    // pressing the button; still inside the dialog, which listens for the keys.
    if (event.detail > 0) stage.focus({ preventScroll: true });
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
    resetZoom();
    if (attachment !== null) load();
  }

  function resetZoom() {
    endPan();
    pinch = null;
    tap = null;
    touches = {};
    setSpace(false);
    zoomFit();
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
    resetZoom();
  }

  var ZOOM_KEYS = {
    "+": function () { zoomTo(view.k * ZOOM_STEP); },
    "=": function () { zoomTo(view.k * ZOOM_STEP); },
    "-": function () { zoomTo(view.k / ZOOM_STEP); },
    "0": zoomFit,
    "1": function () { zoomActual(); },
    " ": function () { setSpace(true); }
  };

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
    // A field keeps all its keys, a focused button its Space (it presses the button).
    var plain = !event.ctrlKey && !event.metaKey && !event.altKey
      && !event.target.closest("input, select, textarea")
      && !(event.key === " " && event.target.closest("button"));
    if (plain && Object.prototype.hasOwnProperty.call(ZOOM_KEYS, event.key)) {
      event.preventDefault(); // the space bar would scroll or press elsewhere
      ZOOM_KEYS[event.key]();
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

  // A swipe pages between screenshots only at fit and while no drawing tool is on.
  function takesTouch() {
    return view.k > 1 || (attachment !== null && tool !== "select" && !svg.hidden);
  }

  function setup(root) {
    viewer = root;
    stage = root.querySelector(".viewer-stage");
    zoomBox = root.querySelector(".viewer-zoom");
    zoomLevel = root.querySelector(".viewer-zoom-level");
    img = root.querySelector(".viewer-img");
    svg = root.querySelector(".viewer-svg");
    tools = root.querySelector(".viewer-tools");
    thread = root.querySelector(".viewer-thread");
    note = root.querySelector(".viewer-note");
    colour = tools.querySelector("[data-colour]").dataset.colour;
    img.addEventListener("load", function () {
      zoomFit();
      paint();
    });
    stage.addEventListener("pointerdown", onPointerDown);
    stage.addEventListener("pointermove", onPointerMove);
    stage.addEventListener("pointerup", onPointerEnd);
    stage.addEventListener("pointercancel", onPointerEnd);
    stage.addEventListener("wheel", onWheel, { passive: false });
    stage.addEventListener("dblclick", onDoubleClick);
    // The middle button's autoscroll is a default of mousedown, not of pointerdown.
    stage.addEventListener("mousedown", function (event) {
      if (event.button === 1) event.preventDefault();
    });
    stage.addEventListener("lostpointercapture", function (event) { dropTouch(event.pointerId); });
    root.addEventListener("keyup", function (event) {
      if (event.key === " ") setSpace(false);
    });
    // A key released in another window never reaches this one.
    window.addEventListener("blur", function () { setSpace(false); });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) setSpace(false);
    });
    window.addEventListener("resize", function () { if (root.open) setView(view); });
    root.querySelector(".viewer-zoom-bar").addEventListener("click", onZoomBar);
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
