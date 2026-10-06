(function () {
  var statusEl = document.getElementById("chat-local-status");
  var box = document.querySelector("[data-chat-warm]");
  var cancelBtn = document.getElementById("chat-local-cancel");
  if (!statusEl || !box) return;
  var warmed = false;
  var timer = null;
  var csrf = document.querySelector("#chat-send-form [name=csrfmiddlewaretoken]");
  function stopPolling() {
    if (timer) clearInterval(timer);
    timer = null;
    if (cancelBtn) cancelBtn.hidden = true;
  }
  function render(data) {
    if (!data) return;
    if (data.ready) {
      statusEl.textContent = "Local model: loaded";
      stopPolling();
    } else if (data.loading) {
      statusEl.textContent = "Loading the local model (about " + (data.waking_seconds || 0) + " s)";
      if (cancelBtn && timer) cancelBtn.hidden = false;
    } else statusEl.textContent = "Local model: not loaded";
  }
  function poll() {
    fetch(statusEl.getAttribute("data-chat-status-url"), {headers: {"Accept": "application/json"}})
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }
  // Cancelling only stops this page from watching; the harness keeps loading the model.
  if (cancelBtn) {
    cancelBtn.addEventListener("click", function () {
      stopPolling();
      statusEl.textContent = "Local model: still loading in the background";
    });
  }
  box.addEventListener("input", function () {
    if (warmed) return;
    warmed = true;
    fetch(statusEl.getAttribute("data-chat-warm-url"), {
      method: "POST",
      headers: {"X-CSRFToken": csrf ? csrf.value : "", "Accept": "application/json"},
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (!data.ok && data.error) statusEl.textContent = data.error;
      else {
        timer = setInterval(poll, 2000);
        render(data);
      }
    }).catch(function () { statusEl.textContent = "The local model can't load right now."; });
  }, {once: true});
})();
