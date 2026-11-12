// The owner's order of the cards: drag a card within its column or into another one (pointer
// events: a mouse or pen moves it, a finger holds it first), or use the card's menu (keyboard,
// phone). The page moves the card at once and the server renumbers the column; a refusal puts
// the card back and says why.
(function () {
  "use strict";

  var DRAG_PX = 6;          // a mouse or pen press moving this far becomes a drag
  var LONG_PRESS_MS = 350;  // a finger held this long picks the card up; moving sooner scrolls
  var TOUCH_SLOP_PX = 8;
  var EDGE_PX = 48;         // near these edges of the window and the board, the page scrolls
  var EDGE_STEP = 14;
  var FOCUS_MS = 4000;      // the column is refetched after a move: the focus follows the card

  var press = null;
  var drag = null;
  var saving = 0;
  var suppressClick = false;
  var focusKey = null;
  var focusUntil = 0;

  function boardEl() { return document.getElementById("board"); }

  function cardsOf(container) {
    return Array.prototype.filter.call(container.children, function (el) {
      return el.matches(".card[data-key]");
    });
  }

  function sibling(card, step) {
    var list = cardsOf(card.parentNode);
    return list[list.indexOf(card) + step] || null;
  }

  function column(card) { return card.closest(".col[data-stage]"); }

  // Where the card is now, by its neighbours: the server places it by the same keys.
  function spot(card) {
    var before = sibling(card, 1);
    var after = sibling(card, -1);
    return {
      stage: column(card).dataset.stage,
      before: before ? before.dataset.key : "",
      after: after ? after.dataset.key : ""
    };
  }

  function sameSpot(a, b) {
    return a.stage === b.stage && a.before === b.before && a.after === b.after;
  }

  // `key`: the task the lines are about. They stay until the owner closes them or the next move.
  function flash(lines, kind, key) {
    var box = document.getElementById("board-flash");
    var list = document.getElementById("board-flash-lines");
    if (!box || !list) return;
    list.replaceChildren();
    box.hidden = !lines.length;
    box.dataset.key = lines.length && key ? key : "";
    lines.forEach(function (text) {
      var p = document.createElement("p");
      p.className = kind === "error" ? "form-error" : "form-warning";
      p.setAttribute("role", kind === "error" ? "alert" : "status");
      p.textContent = text;
      list.appendChild(p);
    });
  }

  // The button goes away with the notice: the focus moves to the moved card's menu button.
  document.addEventListener("click", function (event) {
    if (!event.target.closest("#board-flash-close")) return;
    var box = document.getElementById("board-flash");
    var card = document.getElementById("card-" + (box.dataset.key || ""));
    var target = card && card.querySelector(".card-menu-button");
    if (!target) {
      target = boardEl();
      if (target) target.tabIndex = -1;
    }
    flash([]);
    if (target) target.focus();
  });

  // The plan of the moved task was accepted: its "needs an accepted plan" warning is answered.
  // The text is rules.requirement_label's for plan_accepted.
  function planAccepted(key) {
    var box = document.getElementById("board-flash");
    if (!box || box.hidden || box.dataset.key !== key) return;
    var rest = Array.prototype.filter.call(box.querySelectorAll(".form-warning"), function (p) {
      return p.textContent.indexOf("an accepted plan") === -1;
    }).map(function (p) { return p.textContent; });
    flash(rest, "warning", key);
  }

  function refresh(col) {
    if (col && window.htmx && document.body.contains(col)) window.htmx.trigger(col, "rb:refresh");
  }

  // `home` is where the card was: a refusal puts it back there.
  function save(card, home) {
    var board = boardEl();
    var live = document.getElementById("live");
    var here = spot(card);
    var target = column(card);
    var source = document.getElementById("col-" + home.stage);
    var body = new FormData();
    body.set("csrf_token", board.dataset.csrf || "");
    if (here.stage !== home.stage) body.set("stage", here.stage);
    body.set("before", here.before);
    body.set("after", here.after);
    var url = "/p/" + encodeURIComponent(live.dataset.project) + "/t/" +
      encodeURIComponent(card.dataset.key) + "/move";
    saving += 1;
    flash([]);
    fetch(url, { method: "POST", credentials: "same-origin", body: body }).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) {
          // A domain refusal is plain text; a 403 / 422 from the framework is {"detail": ...}.
          var json = (response.headers.get("content-type") || "").indexOf("json") !== -1;
          var detail = json ? JSON.parse(text).detail : text;
          throw new Error((typeof detail === "string" && detail) || "HTTP " + response.status);
        }
        return JSON.parse(text);
      });
    }).then(function (answer) {
      flash(answer.warnings || [], "warning", card.dataset.key);
    }).catch(function (error) {
      putBack(card, home);
      flash(["Not moved: " + error.message], "error");
    }).finally(function () {
      saving -= 1;
      // Counts and "Nothing here" come from the server; live events refetch other boards.
      refresh(source);
      if (target !== source) refresh(target);
    });
  }

  function putBack(card, home) {
    var col = document.getElementById("col-" + home.stage);
    var cards = col && col.querySelector(".cards");
    if (!cards) return;
    var before = home.before && document.getElementById("card-" + home.before);
    var after = home.after && document.getElementById("card-" + home.after);
    if (before && before.parentNode === cards) cards.insertBefore(card, before);
    else if (after && after.parentNode === cards) cards.insertBefore(card, after.nextSibling);
    else cards.insertBefore(card, cards.firstChild);
  }

  function keepFocus(card) {
    focusKey = card.dataset.key;
    focusUntil = Date.now() + FOCUS_MS;
    var button = card.querySelector(".card-menu-button");
    if (button) button.focus();
  }

  // A refetched column renders a new card: give its menu button the focus back.
  document.addEventListener("htmx:load", function () {
    if (!focusKey || Date.now() > focusUntil) return;
    var active = document.activeElement;
    if (active && active !== document.body && document.body.contains(active)) return;
    var card = document.getElementById("card-" + focusKey);
    var button = card && card.querySelector(".card-menu-button");
    if (button) button.focus();
  });

  // --- the card's menu ------------------------------------------------------------------

  function closeMenus(except) {
    document.querySelectorAll("#board .card-menu[open]").forEach(function (menu) {
      if (menu !== except) menu.open = false;
    });
  }

  document.addEventListener("toggle", function (event) {
    var menu = event.target;
    if (!menu.matches || !menu.matches(".card-menu")) return;
    if (!menu.open) { delete menu.dataset.placed; return; }
    closeMenus(menu);
    var card = menu.closest(".card");
    var up = menu.querySelector("[data-move=up]");
    var down = menu.querySelector("[data-move=down]");
    if (up) up.disabled = !sibling(card, -1);
    if (down) down.disabled = !sibling(card, 1);
    var items = menu.querySelector(".card-menu-items");
    var r = menu.querySelector(".card-menu-button").getBoundingClientRect();
    var height = items.offsetHeight;
    var room = window.innerHeight - r.bottom - 4;
    items.style.top = (room >= height || r.top < height ? r.bottom + 4 : r.top - height - 4) + "px";
    items.style.right = Math.max(4, document.documentElement.clientWidth - r.right) + "px";
    menu.dataset.placed = "1";
  }, true);

  // A fixed menu would stay put while its card scrolls away.
  window.addEventListener("scroll", function () { closeMenus(null); }, true);

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var item = event.target.closest(".card-menu [data-move], .card-menu [data-move-stage]");
    if (!item) {
      if (!event.target.closest(".card-menu")) closeMenus(null);
      return;
    }
    var menu = item.closest(".card-menu");
    var card = item.closest(".card[data-key]");
    var home = spot(card);
    menu.open = false;
    if (item.dataset.move === "up" || item.dataset.move === "down") {
      var up = item.dataset.move === "up";
      var other = sibling(card, up ? -1 : 1);
      if (!other) return;
      card.parentNode.insertBefore(card, up ? other : other.nextSibling);
    } else {
      var cards = document.querySelector("#col-" + item.dataset.moveStage + " .cards");
      if (!cards) return;
      cards.insertBefore(card, cards.firstChild);
    }
    keepFocus(card);
    save(card, home);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var menu = event.target.closest && event.target.closest(".card-menu[open]");
    if (!menu) return;
    event.preventDefault();
    menu.open = false;
    menu.querySelector(".card-menu-button").focus();
  });

  // --- dragging ----------------------------------------------------------------------------

  function startDrag() {
    var card = press.card;
    window.clearTimeout(press.timer);
    closeMenus(null);
    var rect = card.getBoundingClientRect();
    var ghost = card.cloneNode(true);
    ghost.removeAttribute("id");
    ghost.removeAttribute("hx-get");
    ghost.removeAttribute("hx-trigger");
    ghost.classList.add("card-ghost");
    ghost.setAttribute("aria-hidden", "true");
    ghost.style.width = rect.width + "px";
    document.body.appendChild(ghost);
    var marker = document.createElement("div");
    marker.className = "drop-marker";
    card.classList.add("is-dragging");
    document.documentElement.classList.add("is-dragging-card");
    drag = {
      card: card,
      id: press.id,
      ghost: ghost,
      marker: marker,
      home: spot(card),
      dx: press.x - rect.left,
      dy: press.y - rect.top,
      x: press.x,
      y: press.y,
      frame: 0
    };
    press = null;
    suppressClick = true;
    try { card.setPointerCapture(drag.id); } catch (e) { /* the pointer is gone already */ }
    follow(drag.x, drag.y);
    drag.frame = window.requestAnimationFrame(edgeScroll);
  }

  // Columns are as tall as their cards: under a short one the pointer still means its end.
  function below(x, y) {
    var board = boardEl();
    if (y > board.getBoundingClientRect().bottom) return null;
    return Array.prototype.find.call(board.querySelectorAll(".col[data-stage]"), function (col) {
      var r = col.getBoundingClientRect();
      return r.width && x >= r.left && x <= r.right && y >= r.bottom;
    }) || null;
  }

  // The marker goes before the first card whose middle is below the pointer.
  function follow(x, y) {
    drag.x = x;
    drag.y = y;
    drag.ghost.style.transform = "translate(" + (x - drag.dx) + "px, " + (y - drag.dy) + "px)";
    var under = document.elementFromPoint(x, y);
    var col = (under && under.closest && under.closest("#board .col[data-stage]")) || below(x, y);
    var cards = col && col.querySelector(".cards");
    if (!cards) {
      drag.marker.remove();
      return;
    }
    var next = null;
    cardsOf(cards).some(function (card) {
      if (card === drag.card) return false;
      var r = card.getBoundingClientRect();
      if (y < r.top + r.height / 2) { next = card; return true; }
      return false;
    });
    if (next) cards.insertBefore(drag.marker, next);
    else cards.appendChild(drag.marker);
  }

  function edgeScroll() {
    if (!drag) return;
    var dy = drag.y < EDGE_PX ? -EDGE_STEP : drag.y > window.innerHeight - EDGE_PX ? EDGE_STEP : 0;
    if (dy) window.scrollBy(0, dy);
    var board = boardEl();
    var r = board.getBoundingClientRect();
    var dx = drag.x < r.left + EDGE_PX ? -EDGE_STEP : drag.x > r.right - EDGE_PX ? EDGE_STEP : 0;
    var before = board.scrollLeft;
    if (dx) board.scrollLeft += dx;
    if (dy || board.scrollLeft !== before) follow(drag.x, drag.y);
    drag.frame = window.requestAnimationFrame(edgeScroll);
  }

  // The card stays where it is: the caller has put it at the marker, or not.
  function endDrag() {
    var d = drag;
    drag = null;
    window.cancelAnimationFrame(d.frame);
    d.ghost.remove();
    d.card.classList.remove("is-dragging");
    document.documentElement.classList.remove("is-dragging-card");
    // A click may follow the release: it must not open the task.
    window.setTimeout(function () { suppressClick = false; }, 0);
    d.marker.remove();
  }

  function cancelPress() {
    if (press) window.clearTimeout(press.timer);
    press = null;
  }

  document.addEventListener("pointerdown", function (event) {
    if (drag || press || event.button !== 0 || !event.isPrimary) return;
    var card = event.target.closest && event.target.closest("#board .card[data-key]");
    if (!card) return;
    // Links, buttons and the menu keep their own clicks; the title link covers the rest.
    var own = event.target.closest("button, summary, details, form, input, select, textarea, a");
    if (own && !own.matches("a.card-open")) return;
    press = {
      card: card,
      id: event.pointerId,
      touch: event.pointerType === "touch",
      x: event.clientX,
      y: event.clientY,
      timer: 0
    };
    if (press.touch) {
      press.timer = window.setTimeout(function () { if (press) startDrag(); }, LONG_PRESS_MS);
    }
  });

  document.addEventListener("pointermove", function (event) {
    if (press && event.pointerId === press.id) {
      var far = Math.hypot(event.clientX - press.x, event.clientY - press.y);
      if (press.touch) {
        if (far > TOUCH_SLOP_PX) cancelPress();
      } else if (far >= DRAG_PX) {
        startDrag();
      }
      return;
    }
    if (drag && event.pointerId === drag.id) {
      event.preventDefault();
      follow(event.clientX, event.clientY);
    }
  });

  document.addEventListener("pointerup", function (event) {
    if (press && event.pointerId === press.id) { cancelPress(); return; }
    if (!drag || event.pointerId !== drag.id) return;
    var d = drag;
    var cards = d.marker.parentNode;
    if (cards) cards.insertBefore(d.card, d.marker);
    endDrag();
    if (cards && !sameSpot(spot(d.card), d.home)) save(d.card, d.home);
  });

  document.addEventListener("pointercancel", function (event) {
    if (press && event.pointerId === press.id) { cancelPress(); return; }
    if (drag && event.pointerId === drag.id) endDrag();
  });

  // While a card is held: no page scroll by the finger, no native link drag, no long-press menu.
  document.addEventListener("touchmove", function (event) {
    if (drag) event.preventDefault();
  }, { passive: false });
  document.addEventListener("dragstart", function (event) {
    if (event.target.closest && event.target.closest("#board .card")) event.preventDefault();
  });
  document.addEventListener("contextmenu", function (event) {
    if (drag || (press && press.touch)) event.preventDefault();
  });
  document.addEventListener("click", function (event) {
    if (!suppressClick) return;
    suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && drag) {
      event.preventDefault();
      endDrag();
    }
  });

  window.relaybayBoard = {
    busy: function () { return !!drag || saving > 0; },
    planAccepted: planAccepted
  };
})();
