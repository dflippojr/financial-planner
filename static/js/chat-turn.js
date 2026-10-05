(function () {
  // Pending chat turns are answered in the background; poll each one and swap in the reply.
  var POLL_MS = 2000;

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
  }

  function watch(article) {
    var url = article.getAttribute("data-chat-turn-url");
    var body = article.querySelector("[data-chat-turn-body]");
    article.setAttribute("data-chat-turn-polling", "true");
    if (body) body.textContent = "Thinking…";
    function poll() {
      fetch(url, {headers: {"Accept": "application/json"}, credentials: "same-origin"})
        .then(function (response) {
          // Gone (deleted or expired): stop asking.
          if (response.status === 404) return {ok: false, gone: true};
          return response.json();
        })
        .then(function (data) {
          if (data && data.gone) return;
          if (data && data.ok && data.status !== "pending") render(article, data);
          else setTimeout(poll, POLL_MS);
        })
        .catch(function () { setTimeout(poll, POLL_MS); });
    }
    setTimeout(poll, POLL_MS);
  }

  document.querySelectorAll("[data-chat-turn-url]:not([data-chat-turn-polling])").forEach(watch);
})();
