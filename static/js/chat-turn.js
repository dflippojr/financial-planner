(function () {
  // Pending chat turns are answered in the background; poll each one and swap in the reply.
  var POLL_MS = 2000;
  var MAX_FAILURES = 30;

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

  function render(article, data) {
    var label = article.querySelector("[data-chat-turn-label]");
    var body = article.querySelector("[data-chat-turn-body]");
    if (data.role === "error" && label) label.textContent = label.textContent + " (error)";
    body.textContent = data.content || "";
    if (data.figures && data.figures.length) {
      article.appendChild(list("mt-2 list-disc pl-5 text-sm", data.figures, function (li, figure) {
        var text = figure.label + ": " + figure.amount_display;
        if (!figure.url) {
          li.textContent = text;
          return;
        }
        var link = document.createElement("a");
        link.className = "link";
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
    article.removeAttribute("data-chat-turn-url");
    // Proposal cards are rendered by the server, so load the page to show them.
    if (data.proposals) window.location.reload();
  }

  function watch(article) {
    var url = article.getAttribute("data-chat-turn-url");
    var body = article.querySelector("[data-chat-turn-body]");
    article.setAttribute("data-chat-turn-polling", "true");
    if (body) body.textContent = "Thinking…";
    var failures = 0;
    function retry() {
      // A signed-out session or a server error page: stop after about a minute.
      failures += 1;
      if (failures >= MAX_FAILURES) {
        if (body) body.textContent = "Thinking… refresh to see the answer.";
        return;
      }
      setTimeout(poll, POLL_MS);
    }
    function poll() {
      fetch(url, {headers: {"Accept": "application/json"}, credentials: "same-origin", redirect: "error"})
        .then(function (response) {
          // Gone (deleted or expired): stop asking.
          if (response.status === 404) return {ok: false, gone: true};
          if (!response.ok) throw new Error("status " + response.status);
          return response.json();
        })
        .then(function (data) {
          if (data && data.gone) return;
          failures = 0;
          if (data && data.ok && data.status !== "pending") render(article, data);
          else setTimeout(poll, POLL_MS);
        })
        .catch(retry);
    }
    setTimeout(poll, POLL_MS);
  }

  document.querySelectorAll("[data-chat-turn-url]:not([data-chat-turn-polling])").forEach(watch);
})();
