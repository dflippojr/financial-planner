(function () {
  // Sign-in popup for linking a member's own Claude or Codex plan (#237).
  var POLL_MS = 3000;
  var dialog = document.getElementById("plan-link-dialog");
  if (!dialog) return;
  var csrf = dialog.getAttribute("data-csrf") || "";
  var title = document.getElementById("plan-link-title");
  var message = document.getElementById("plan-link-message");
  var urlRow = document.getElementById("plan-link-url-row");
  var url = document.getElementById("plan-link-url");
  var userCodeRow = document.getElementById("plan-link-user-code-row");
  var userCode = document.getElementById("plan-link-user-code");
  var codeForm = document.getElementById("plan-link-code-form");
  var codeInput = document.getElementById("plan-link-code-input");
  var countdown = document.getElementById("plan-link-countdown");
  var cancel = document.getElementById("plan-link-cancel");
  var session = null;

  function stop() {
    if (!session) return;
    session.active = false;
    clearTimeout(session.pollTimer);
    clearInterval(session.tickTimer);
    session = null;
  }

  function reset() {
    urlRow.hidden = true;
    userCodeRow.hidden = true;
    codeForm.hidden = true;
    countdown.hidden = true;
    codeInput.value = "";
    userCode.textContent = "";
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      credentials: "same-origin",
      redirect: "error",
      headers: {"Accept": "application/json", "Content-Type": "application/json", "X-CSRFToken": csrf},
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(function (data) { return data; }, function () { return {ok: false}; });
    });
  }

  function finish(text, reload) {
    stop();
    message.textContent = text;
    reset();
    if (reload) setTimeout(function () { window.location.reload(); }, 800);
  }

  function poll(current) {
    if (!current.active) return;
    fetch(current.statusUrl, {headers: {"Accept": "application/json"}, credentials: "same-origin", redirect: "error"})
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!current.active) return;
        if (data.ok && data.linked) return finish("Your " + current.label + " plan is linked.", true);
        if (data.ok && data.failed) return finish("The sign-in ended before it finished. Close this and try Link again.", false);
        if (!data.ok && data.error) return finish(data.error, false);
        current.pollTimer = setTimeout(function () { poll(current); }, POLL_MS);
      })
      .catch(function () {
        if (current.active) current.pollTimer = setTimeout(function () { poll(current); }, POLL_MS);
      });
  }

  function tick(current) {
    var left = Math.max(0, Math.round((current.expiresAt - Date.now()) / 1000));
    var minutes = Math.floor(left / 60);
    var seconds = String(left % 60).padStart(2, "0");
    countdown.textContent = "This sign-in expires in " + minutes + ":" + seconds + ".";
    if (left === 0) finish("The sign-in expired. Close this and try Link again.", false);
  }

  function open(button) {
    stop();
    reset();
    var label = button.getAttribute("data-plan-label") || "";
    title.textContent = "Link your " + label + " plan";
    message.textContent = "Starting the sign-in…";
    if (!dialog.open) dialog.showModal();
    var current = {
      active: true,
      label: label,
      backend: button.getAttribute("data-plan-link"),
      codeUrl: button.getAttribute("data-plan-code"),
      statusUrl: button.getAttribute("data-plan-status"),
      attemptId: ""
    };
    session = current;
    post(button.getAttribute("data-plan-start"), {}).then(function (data) {
      if (!current.active) return;
      if (data.reauth) {
        window.location.href = data.reauth;
        return;
      }
      if (!data.ok) return finish(data.error || "The sign-in could not be started.", false);
      current.attemptId = data.attempt_id;
      current.expiresAt = Date.now() + (data.expires_in || 600) * 1000;
      url.href = data.verification_url;
      urlRow.hidden = false;
      if (data.user_code) {
        userCode.textContent = data.user_code;
        userCodeRow.hidden = false;
      }
      codeForm.hidden = !data.needs_code;
      message.textContent = data.needs_code
        ? "Open the sign-in page, approve access, then paste the code it shows."
        : "Open the sign-in page and finish there. This window updates when it is done.";
      countdown.hidden = false;
      tick(current);
      current.tickTimer = setInterval(function () { tick(current); }, 1000);
      poll(current);
    }).catch(function () {
      if (current.active) finish("The sign-in could not be started.", false);
    });
  }

  codeForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var current = session;
    var code = codeInput.value.trim();
    if (!current || !code) return;
    // The code is sent once, then cleared from the page.
    codeInput.value = "";
    codeForm.hidden = true;
    message.textContent = "Checking the code…";
    post(current.codeUrl, {attempt_id: current.attemptId, code: code}).then(function (data) {
      if (!current.active) return;
      if (!data.ok) return finish(data.error || "That code was not accepted.", false);
      message.textContent = "Code accepted. Finishing the link…";
    }).catch(function () {
      if (current.active) finish("The code could not be sent. Try Link again.", false);
    });
  });

  cancel.addEventListener("click", function () {
    stop();
    dialog.close();
  });
  dialog.addEventListener("close", stop);

  document.querySelectorAll("[data-plan-link]").forEach(function (button) {
    button.addEventListener("click", function () { open(button); });
  });
})();
