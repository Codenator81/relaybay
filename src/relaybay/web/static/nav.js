// Sidebar: collapsed or shown on wide screens (remembered in this browser), a drawer on phones.
// Loaded without `defer` so the remembered state applies before the first paint.
(function () {
  "use strict";

  var KEY = "relaybay.nav";
  var root = document.documentElement;
  var phone = window.matchMedia("(max-width: 760px)");

  // Storage can be unavailable (private window, blocked site data): the sidebar is then shown
  // and the choice lasts until the page is left.
  function remembered() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function remember(value) {
    try { window.localStorage.setItem(KEY, value); } catch (e) { /* see remembered() */ }
  }

  if (remembered() === "collapsed") root.dataset.nav = "collapsed";
  // The board's Compact toggle (app.js), here for the same reason.
  try {
    if (window.localStorage.getItem("relaybay.compact") === "1") root.dataset.compact = "1";
  } catch (e) { /* see remembered() */ }

  function toggleButton() { return document.querySelector(".nav-toggle"); }
  function drawerOpen() { return root.dataset.drawer === "open"; }

  function sync() {
    var button = toggleButton();
    if (!button) return;
    var shown = phone.matches ? drawerOpen() : root.dataset.nav !== "collapsed";
    button.setAttribute("aria-expanded", shown ? "true" : "false");
  }

  function setDrawer(open) {
    if (open) {
      root.dataset.drawer = "open";
      var first = document.querySelector("#sidebar a, #sidebar summary");
      if (first) first.focus();
    } else if (drawerOpen()) {
      delete root.dataset.drawer;
      var button = toggleButton();
      if (button) button.focus();
    }
    sync();
  }

  function toggle() {
    if (phone.matches) {
      setDrawer(!drawerOpen());
      return;
    }
    if (root.dataset.nav === "collapsed") {
      delete root.dataset.nav;
      remember("shown");
    } else {
      root.dataset.nav = "collapsed";
      remember("collapsed");
    }
    sync();
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    if (event.target.closest(".nav-toggle")) {
      toggle();
    } else if (event.target.closest(".nav-backdrop")) {
      setDrawer(false);
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && drawerOpen()) setDrawer(false);
  });
  phone.addEventListener("change", function () {
    delete root.dataset.drawer;
    sync();
  });
  document.addEventListener("DOMContentLoaded", sync);
})();
