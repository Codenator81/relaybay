// Document page: the contents mark the section being read, and the words of a search inside the
// document are marked in the text.
(function () {
  "use strict";

  function markCurrent() {
    var links = document.querySelectorAll(".doc-toc [data-toc]");
    if (!links.length || !("IntersectionObserver" in window)) return;
    var byAnchor = {};
    links.forEach(function (a) { byAnchor[a.dataset.toc] = a; });
    var headings = Array.prototype.filter.call(
      document.querySelectorAll(".doc-body [id]"),
      function (h) { return byAnchor[h.id]; }
    );
    var current = null;
    function show(id) {
      if (current) current.classList.remove("is-current");
      current = byAnchor[id] || null;
      if (!current) return;
      current.classList.add("is-current");
      var fold = current.closest("details");
      if (fold) fold.open = true;
    }
    var observer = new IntersectionObserver(function () {
      // The last heading above the top quarter of the window is the section being read.
      var line = window.innerHeight / 4;
      var above = headings.filter(function (h) { return h.getBoundingClientRect().top <= line; });
      show((above[above.length - 1] || headings[0]).id);
    }, { rootMargin: "0px 0px -75% 0px" });
    headings.forEach(function (h) { observer.observe(h); });
  }

  function markWords() {
    var article = document.querySelector(".doc-body[data-mark]");
    if (!article) return;
    var words = article.dataset.mark.toLowerCase().match(/[\p{L}\p{N}_]+/gu) || [];
    words = words.filter(function (w) { return w.length > 1; });
    if (!words.length) return;
    var escaped = words.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); });
    var pattern = new RegExp("(" + escaped.join("|") + ")", "giu");
    var walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      pattern.lastIndex = 0;
      if (!pattern.test(text)) return;
      var parts = text.split(pattern);
      var frag = document.createDocumentFragment();
      parts.forEach(function (part, i) {
        if (i % 2) {
          var mark = document.createElement("mark");
          mark.textContent = part;
          frag.appendChild(mark);
        } else if (part) {
          frag.appendChild(document.createTextNode(part));
        }
      });
      node.parentNode.replaceChild(frag, node);
    });
  }

  markCurrent();
  markWords();
})();
