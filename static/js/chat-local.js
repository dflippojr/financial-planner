(function () {
  var statusEl = document.getElementById("chat-local-status");
  var box = document.querySelector("[data-chat-warm]");
  if (!statusEl || !box) return;
  var warmed = false;
  var started = 0;
  var csrf = document.querySelector("#chat-send-form [name=csrfmiddlewaretoken]");
  function render(data) {
    if (!data) return;
    if (data.ready) statusEl.textContent = "Local model: loaded";
    else if (data.loading) {
      var elapsed = started ? Math.round((Date.now() - started) / 1000) : data.waking_seconds || 0;
      statusEl.textContent = "Local model: loading (" + elapsed + "s)";
    } else statusEl.textContent = "Local model: not loaded";
  }
  function poll() {
    fetch(statusEl.getAttribute("data-chat-status-url"), {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }
  box.addEventListener("input", function () {
    if (warmed) return;
    warmed = true;
    started = Date.now();
    fetch(statusEl.getAttribute("data-chat-warm-url"), {
      method: "POST",
      headers: {"X-CSRFToken": csrf ? csrf.value : "", "Accept": "application/json"},
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (!data.ok && data.error) statusEl.textContent = data.error;
      else render(data);
    }).catch(function () { statusEl.textContent = "The local model can't load right now."; });
    setInterval(poll, 2000);
  }, {once: true});
})();
