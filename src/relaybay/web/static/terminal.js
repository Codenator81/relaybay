// The Terminal tab of a run: the vendored xterm.js on a WebSocket to `claude --resume` on the
// server. The size is fitted after layout and sent with the socket's URL (a fit before layout
// would start a 2x1 PTY); later sizes go as messages.
(function () {
  "use strict";

  var page = document.getElementById("terminal-page");
  if (!page || !window.Terminal || !window.FitAddon) return;
  var status = document.getElementById("terminal-status");
  var hint = document.getElementById("terminal-hint");
  var closeButton = document.getElementById("terminal-close");
  var pasteButton = document.getElementById("terminal-paste");
  var backLink = document.getElementById("terminal-back");
  var hintKey = "relaybay:terminal-hint:" + page.dataset.run;

  function hintSeen() {
    try {
      return window.localStorage.getItem(hintKey) === "1";
    } catch (e) {
      return false; // storage blocked: the hint shows each time
    }
  }

  function rememberHint() {
    try {
      window.localStorage.setItem(hintKey, "1");
    } catch (e) {
      // storage blocked: nothing to remember it in
    }
  }

  if (!hintSeen()) hint.hidden = false;

  var term = new window.Terminal({
    cursorBlink: true,
    fontFamily: '"Cascadia Mono", Consolas, Menlo, "DejaVu Sans Mono", monospace',
    fontSize: 14,
    scrollback: 5000,
  });
  var fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(document.getElementById("terminal-screen"));

  var socket = null;
  var leaving = false;
  var over = false;

  function setStatus(text) {
    status.textContent = text;
  }

  function send(message) {
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }

  function ended(text) {
    over = true;
    setStatus(text);
    term.options.disableStdin = true;
    closeButton.hidden = true;
    pasteButton.hidden = true;
    backLink.hidden = false;
  }

  // xterm sends Ctrl+V to the TUI as ^V: let the browser paste instead (xterm's own paste
  // listener sends the text). Shift+Insert likewise; Ctrl+C with a selection copies it.
  term.attachCustomKeyEventHandler(function (ev) {
    if (ev.type !== "keydown") return true;
    var key = (ev.key || "").toLowerCase();
    if (ev.ctrlKey && !ev.altKey && !ev.metaKey && key === "v") return false;
    if (ev.shiftKey && key === "insert") return false;
    if (ev.ctrlKey && key === "c" && term.hasSelection()) return false;
    return true;
  });

  term.onData(function (data) {
    send({ type: "input", data: data });
  });
  term.onResize(function (size) {
    send({ type: "resize", rows: size.rows, cols: size.cols });
  });

  var refit = 0;
  window.addEventListener("resize", function () {
    window.cancelAnimationFrame(refit);
    refit = window.requestAnimationFrame(function () {
      fit.fit();
    });
  });

  pasteButton.addEventListener("click", function () {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      setStatus("This window gives no access to the clipboard: use Ctrl+V in the terminal.");
      return;
    }
    navigator.clipboard.readText().then(
      function (text) {
        term.paste(text);
        term.focus();
      },
      function (err) {
        setStatus("The clipboard could not be read (" + err.message + "): use Ctrl+V in the terminal.");
      }
    );
  });

  closeButton.addEventListener("click", function () {
    leaving = true;
    closeButton.disabled = true;
    setStatus("Closing…");
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close(1000, "closed by the owner");
    else window.location.assign(page.dataset.back);
  });

  function connect(token) {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    var query = new URLSearchParams({ token: token, rows: term.rows, cols: term.cols });
    socket = new WebSocket(scheme + "//" + window.location.host + page.dataset.path + "?" + query);
    socket.onopen = function () {
      setStatus("Connected: claude --resume in the run's folder.");
      rememberHint();
      term.focus();
    };
    socket.onmessage = function (ev) {
      term.write(ev.data);
    };
    socket.onclose = function (ev) {
      if (leaving) {
        window.location.assign(page.dataset.back);
        return;
      }
      if (over) return;
      if (ev.code === 1000) ended("claude exited; the run is back with relaybay.");
      else if (ev.code === 4000) ended("The terminal did not open: the reason is above.");
      else ended("The connection closed (code " + ev.code + "); the run is back with relaybay.");
    };
  }

  function start() {
    fit.fit();
    var body = new URLSearchParams();
    body.set(page.dataset.csrfField, page.dataset.csrf);
    fetch(page.dataset.path + "/token", { method: "POST", body: body, credentials: "same-origin" })
      .then(function (response) {
        if (response.ok) return response.json();
        return response.text().then(function (text) {
          throw new Error(text || response.statusText);
        });
      })
      .then(function (data) {
        connect(data.token);
      })
      .catch(function (err) {
        ended("The terminal did not open: " + err.message);
      });
  }

  // After the load event the stylesheets are in, so the fit measures the laid-out screen; no
  // animation frame, which a window in the background never gets.
  if (document.readyState === "complete") start();
  else window.addEventListener("load", start);
})();
