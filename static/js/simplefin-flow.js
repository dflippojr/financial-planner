(function () {
  // Fix and Add a bank on SimpleFIN Bridge in a pop-up (#694). This page keeps
  // the only reference to the pop-up, cuts the pop-up's way back (opener =
  // null), and never reads anything from it: it only checks whether it is
  // closed and closes it. Success is decided by the server from SimpleFIN reads.
  var root = document.getElementById("simplefin-flow");
  if (!root) return;
  var csrf = root.getAttribute("data-csrf") || "";
  var startUrl = root.getAttribute("data-start-url");
  var checkUrl = root.getAttribute("data-check-url");
  var cancelUrl = root.getAttribute("data-cancel-url");
  var panel = document.getElementById("simplefin-flow-panel");
  var text = document.getElementById("simplefin-flow-text");
  var blocked = document.getElementById("simplefin-flow-blocked");
  var done = document.getElementById("simplefin-flow-done");
  var cancel = document.getElementById("simplefin-flow-cancel");
  var result = document.getElementById("simplefin-flow-result");
  var FEATURES = "popup,width=520,height=720";
  var WATCH_MS = 1000;
  // A pop-up that reads as closed this soon was cut off by the browser (for
  // example by a cross-origin opener policy), not closed by the member.
  var EARLY_CLOSE_MS = 3000;
  var RELOAD_MS = 1500;
  var WAITING = "Waiting for SimpleFIN…";
  var flow = null;

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      headers: {"Accept": "application/json", "Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return {ok: false, state: "error"}; });
    });
  }

  function openPopup(url) {
    var win = null;
    try { win = window.open("", "simplefin-bridge", FEATURES); } catch (error) { win = null; }
    if (!win) return null;
    try { win.opener = null; } catch (error) { /* An already-open cross-origin pop-up keeps its own. */ }
    try { win.location.href = url; } catch (error) { return null; }
    return win;
  }

  function closePopup() {
    if (!flow || !flow.win) return;
    try { if (!flow.win.closed) flow.win.close(); } catch (error) { /* Nothing more the page can do. */ }
  }

  function stopTimers() {
    if (!flow) return;
    clearTimeout(flow.timer);
    clearInterval(flow.watch);
  }

  function setBusy(busy) {
    done.disabled = busy;
  }

  function finish(message, reload) {
    stopTimers();
    flow = null;
    panel.hidden = true;
    blocked.hidden = true;
    setBusy(false);
    result.textContent = message || "";
    result.hidden = !message;
    if (reload) setTimeout(function () { window.location.reload(); }, RELOAD_MS);
  }

  function schedule(current, seconds) {
    clearTimeout(current.timer);
    current.timer = setTimeout(function () { check(current); }, Math.max(1, seconds) * 1000);
  }

  function check(current) {
    if (flow !== current || current.busy || current.cancelling) return;
    current.busy = true;
    setBusy(true);
    text.textContent = "Checking SimpleFIN…";
    post(checkUrl, {}).then(function (data) {
      if (flow !== current || current.cancelling) return;
      current.busy = false;
      setBusy(false);
      if (data.state === "success") {
        closePopup();
        finish(data.message, true);
      } else if (data.state === "pending") {
        text.textContent = WAITING + " " + data.checks_left + " more check" + (data.checks_left === 1 ? "" : "s") + " left.";
        schedule(current, data.next_check_in);
      } else if (data.state === "wait") {
        text.textContent = WAITING;
        schedule(current, data.retry_in);
      } else {
        finish(data.message || "SimpleFIN could not be checked.", true);
      }
    }, function () {
      if (flow !== current) return;
      finish("The app could not be reached. Reload the page to try again.", false);
    });
  }

  function cancelFlow() {
    var current = flow;
    if (!current || current.cancelling) return;
    current.cancelling = true;
    stopTimers();
    closePopup();
    setBusy(true);
    text.textContent = "Closing SimpleFIN…";
    post(cancelUrl, {}).then(function (data) {
      if (flow !== current) return;
      finish(data.message || "Cancelled.", true);
    }, function () {
      if (flow !== current) return;
      finish("Cancelled. The next scheduled sync will pick up any changes.", false);
    });
  }

  function watchClosed() {
    var current = flow;
    if (!current || !current.win || current.cancelling) return;
    var closed;
    try { closed = current.win.closed; } catch (error) { closed = false; }
    if (!closed) return;
    if (Date.now() - current.openedAt < EARLY_CLOSE_MS) {
      // Keep Done and Cancel; the member says when they are finished.
      clearInterval(current.watch);
      current.win = null;
      return;
    }
    cancelFlow();
  }

  function begin(kind, connId, url) {
    if (flow) return;
    var win = openPopup(url);
    var current = {kind: kind, win: win, openedAt: Date.now(), timer: null, watch: null, busy: false, cancelling: false};
    flow = current;
    result.hidden = true;
    panel.hidden = false;
    blocked.hidden = win !== null;
    text.textContent = win ? WAITING : "Pop-up blocked. " + WAITING;
    if (win) current.watch = setInterval(watchClosed, WATCH_MS);
    post(startUrl, {kind: kind, conn_id: connId}).then(function (data) {
      if (flow !== current) return;
      if (!data.ok) {
        closePopup();
        finish(data.message || "That could not be started.", false);
        return;
      }
      schedule(current, data.first_check_in);
    }, function () {
      if (flow !== current) return;
      closePopup();
      finish("The app could not be reached. Reload the page to try again.", false);
    });
  }

  root.addEventListener("click", function (event) {
    var fix = event.target.closest("[data-simplefin-fix]");
    var add = event.target.closest("[data-simplefin-add]");
    var link = fix || add;
    if (!link) return;
    event.preventDefault();
    begin(fix ? "fix" : "add", fix ? fix.getAttribute("data-simplefin-fix") : "", link.href);
  });
  done.addEventListener("click", function () { if (flow) check(flow); });
  cancel.addEventListener("click", cancelFlow);
  // iPhone pauses timers in a background tab; look again when this page returns.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") watchClosed();
  });
})();
