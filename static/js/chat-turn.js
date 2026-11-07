(function () {
  // Pending chat turns are answered in the background; poll each one and swap in the reply.
  var FIRST_POLL_MS = 500;
  var POLL_MS = 1000;
  var MAX_FAILURES = 60;

  function list(className, items, build) {
    var ul = document.createElement("ul");
    ul.className = className;
    items.forEach(function (item) {
      var li = document.createElement("li");
      build(li, item);
      ul.appendChild(li);
    });
    return ul;
  }

  // An assistant reply arrives as server-rendered markdown (issue #634): the same escaped markup
  // the saved turn shows, swapped in for the Thinking row. Parsing it never runs scripts.
  function showMarkdown(article, html) {
    var slot = article.querySelector("[data-chat-turn-slot]");
    var parsed = new DOMParser().parseFromString(html, "text/html");
    var nodes = Array.from(parsed.body.childNodes, function (node) { return document.importNode(node, true); });
    slot.replaceWith.apply(slot, nodes);
  }

  function render(article, data) {
    var label = article.querySelector("[data-chat-turn-label]");
    var body = article.querySelector("[data-chat-turn-body]");
    if (data.role === "error" && label) label.textContent = label.textContent + " (error)";
    if (data.role !== "error" && typeof data.content_html === "string" && article.querySelector("[data-chat-turn-slot]")) {
      showMarkdown(article, data.content_html);
    } else {
      body.textContent = data.content || "";
    }
    if (typeof data.charts_html === "string" && data.charts_html) {
      // Charts come as the saved turn's server-rendered include (#646), then draw from its stored series.
      var parsedCharts = new DOMParser().parseFromString(data.charts_html, "text/html");
      Array.from(parsedCharts.body.childNodes, function (node) { article.appendChild(document.importNode(node, true)); });
      if (window.ChatCharts) window.ChatCharts.start(article);
    }
    if (data.figures && data.figures.length) {
      // Same collapsed Sources block the saved turn renders (_chat_messages.html).
      var details = document.createElement("details");
      details.className = "mt-2 text-sm";
      details.setAttribute("data-chat-figures", "");
      var summary = document.createElement("summary");
      summary.className = "cursor-pointer py-3";
      summary.textContent = "Sources (" + data.figures.length + ")";
      details.appendChild(summary);
      article.appendChild(details);
      details.appendChild(list("list-disc pl-5", data.figures, function (li, figure) {
        var text = figure.label + ": " + figure.amount_display;
        if (!figure.url) {
          li.textContent = text;
          return;
        }
        var link = document.createElement("a");
        link.className = "link inline-flex min-h-11 items-center";
        link.href = figure.url;
        link.textContent = text;
        li.appendChild(link);
      }));
    }
    if (data.notices && data.notices.length) {
      article.appendChild(list("mt-2 text-sm opacity-80", data.notices, function (li, notice) {
        li.textContent = notice;
      }));
    }
    var dots = article.querySelector("[data-chat-thinking]");
    if (dots) dots.remove();
    article.removeAttribute("data-chat-turn-url");
    article.removeAttribute("aria-busy");
    // Proposal cards are rendered by the server, so load the page to show them.
    article.dispatchEvent(new CustomEvent("chat:answered", {bubbles: true}));
    if (data.proposals) {
      if (article.closest("#ai-popover")) article.dispatchEvent(new CustomEvent("chat:proposals", {bubbles: true}));
      else reloadAt(article);
    }
  }

  // Reload anchored to the turn that produced the cards (#686), so the reader stays with it. A
  // change of hash alone never reloads, so set it in place first; chat-page-layout.js scrolls to it.
  function reloadAt(article) {
    if (article.id) history.replaceState(history.state, "", "#" + article.id);
    window.location.reload();
  }

  function watch(article) {
    var url = article.getAttribute("data-chat-turn-url");
    var body = article.querySelector("[data-chat-turn-body]");
    var dots = article.querySelector("[data-chat-thinking]");
    article.setAttribute("data-chat-turn-polling", "true");
    // The phone design's Thinking state: animated dots plus a line of text (issue #333).
    if (body) body.textContent = "Working on it. Answers can take a minute.";
    if (dots) dots.hidden = false;
    var failures = 0;
    function retry() {
      // A signed-out session or a server error page: stop after about a minute.
      failures += 1;
      if (failures >= MAX_FAILURES) {
        if (body) body.textContent = "Thinking… refresh to see the answer.";
        if (dots) dots.hidden = true;
        return;
      }
      setTimeout(poll, POLL_MS);
    }
    function poll() {
      if (!article.isConnected) return;
      fetch(url, {headers: {"Accept": "application/json"}, credentials: "same-origin", redirect: "error"})
        .then(function (response) {
          // Gone (deleted or expired): stop asking.
          if (response.status === 404) return {ok: false, gone: true};
          if (!response.ok) throw new Error("status " + response.status);
          return response.json();
        })
        .then(function (data) {
          if (data && data.gone) {
            article.dispatchEvent(new CustomEvent("chat:gone", {bubbles: true}));
            return;
          }
          failures = 0;
          if (data && data.ok && data.status !== "pending") render(article, data);
          else setTimeout(poll, POLL_MS);
        })
        .catch(retry);
    }
    setTimeout(poll, FIRST_POLL_MS);
  }

  function start(root) {
    root.querySelectorAll("[data-chat-turn-url]:not([data-chat-turn-polling])").forEach(watch);
  }
  window.ChatTurns = {start: start};
  start(document);
})();
