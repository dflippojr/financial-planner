(function () {
  // Fix and Add a bank on SimpleFIN Bridge in a pop-up (#694). This page keeps
  // the only reference to the pop-up and never reads anything from it: it
  // only checks whether it is closed and closes it. Success is decided by the
  // server from SimpleFIN reads. The pop-up keeps its opener link (owner
  // decision 4 on #694): browsers ignore close() on a cross-origin pop-up
  // whose opener was cut. Should a browser still refuse, the page asks the
  // member to close the window and reloads once it is gone.
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
  var RELOAD_MS = 1500;
  var CLOSE_SETTLE_MS = 300;
  var WAITING = "Waiting for SimpleFIN…";
  var flow = null;
  var lingering = null;

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
    try { return window.open(url, "simplefin-bridge", FEATURES) || null; } catch (error) { return null; }
  }

  function isClosed(win) {
    try { return win.closed; } catch (error) { return false; }
  }

  function closePopup() {
    if (!flow || !flow.win) return;
    try { if (!flow.win.closed) flow.win.close(); } catch (error) { /* Nothing more the page can do. */ }
  }

  function reloadWhenClosed() {
    if (lingering && isClosed(lingering.win)) {
      clearInterval(lingering.watch);
      lingering = null;
      window.location.reload();
    }
  }

  // The flow is over: close the pop-up, then reload, or ask the member to close it.
  function settle(message) {
    var win = flow && flow.win;
    closePopup();
    stopTimers();
    flow = null;
    setTimeout(function () {
      if (!win || isClosed(win)) {
        finish(message, true);
        return;
      }
      finish(message + " Close the SimpleFIN window to finish.", false);
      lingering = {win: win, watch: setInterval(reloadWhenClosed, WATCH_MS)};
    }, CLOSE_SETTLE_MS);
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
    done.hidden = false;
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
    if (flow !== current || current.busy || current.cancelling || current.over) return;
    if (!current.started) {
      current.pendingCheck = true;  // Runs once the server has the flow.
      return;
    }
    current.busy = true;
    setBusy(true);
    text.textContent = "Checking SimpleFIN…";
    current.checking = post(checkUrl, {flow_id: current.flowId}).then(function (data) {
      current.lastCheck = data;
      if (flow !== current || current.cancelling) return;
      current.busy = false;
      setBusy(false);
      if (data.state === "success") {
        settle(data.message);
      } else if (data.state === "pending") {
        text.textContent = WAITING + " " + data.checks_left + " more check" + (data.checks_left === 1 ? "" : "s") + " left.";
        schedule(current, data.next_check_in);
      } else if (data.state === "wait") {
        text.textContent = WAITING;
        schedule(current, data.retry_in);
      } else {
        giveUp(current, data.message || "SimpleFIN could not be checked.");
      }
    }, function () {
      if (flow !== current || current.cancelling) return;
      // Keep the window, its close watcher and Cancel: the flow is still open.
      current.busy = false;
      setBusy(false);
      text.textContent = "The app could not be reached. Press Done to check again, or Cancel.";
    });
  }

  // The checks are over but the pop-up may still be in use: keep watching it,
  // and let Cancel (or closing it) close it and reload with the outcome.
  function giveUp(current, message) {
    clearTimeout(current.timer);
    current.over = message;
    text.textContent = message;
    done.hidden = true;
    if (!current.win || isClosed(current.win)) finish(message, true);
  }

  function cancelFlow() {
    var current = flow;
    if (!current || current.cancelling) return;
    if (current.over) {
      current.cancelling = true;
      settle(current.over);
      return;
    }
    current.cancelling = true;
    stopTimers();
    closePopup();
    setBusy(true);
    text.textContent = "Closing SimpleFIN…";
    // Cancel only after the start request and any check in flight finished: the
    // cancel finds the flow it ends, and a check's sync is shown, not lost.
    var settled = function (promise) { return (promise || Promise.resolve()).catch(function () {}); };
    settled(current.ready).then(function () { return settled(current.checking); }).then(function () {
      var last = current.lastCheck;
      if (last && ["success", "exhausted", "budget"].indexOf(last.state) !== -1) return last;
      return post(cancelUrl, {flow_id: current.flowId});
    }).then(function (data) {
      if (flow !== current) return;
      settle(data.message || "Cancelled.");
    }, function () {
      if (flow !== current) return;
      settle("Cancelled. The next scheduled sync will pick up any changes.");
    });
  }

  function watchClosed() {
    var current = flow;
    if (!current || !current.win || current.cancelling) return;
    // Any close counts as Cancel (owner decision 5 on #694).
    if (isClosed(current.win)) cancelFlow();
  }

  function begin(kind, connId, url) {
    if (flow || lingering) return;
    var win = openPopup(url);
    var current = {kind: kind, win: win, timer: null, watch: null, busy: false,
                   cancelling: false, started: false, pendingCheck: false, ready: null};
    flow = current;
    result.hidden = true;
    panel.hidden = false;
    blocked.hidden = win !== null;
    text.textContent = win ? WAITING : "Pop-up blocked. " + WAITING;
    if (win) current.watch = setInterval(watchClosed, WATCH_MS);
    current.ready = post(startUrl, {kind: kind, conn_id: connId}).then(function (data) {
      current.started = !!data.ok;
      current.flowId = data.flow_id || "";
      if (flow !== current || current.cancelling) return;
      if (!data.ok) {
        closePopup();
        finish(data.message || "That could not be started.", false);
        return;
      }
      if (current.pendingCheck) check(current);
      else schedule(current, data.first_check_in);
    }, function () {
      if (flow !== current || current.cancelling) return;
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
    if (document.visibilityState !== "visible") return;
    watchClosed();
    reloadWhenClosed();
  });
})();
