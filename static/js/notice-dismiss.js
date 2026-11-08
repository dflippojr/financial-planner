(function () {
  // Dismiss buttons on notices (templates/finance/_investment_notice.html, #699). The form posts
  // on its own without JavaScript; this sends it with fetch and removes the note in place only
  // when the server confirms it saved the dismissal. Anything else, including a sign-in redirect
  // after the session ended and a notice whose wording changed, falls back to the plain POST.
  document.addEventListener("submit", function (event) {
    var form = event.target instanceof HTMLFormElement && event.target.matches("form[data-notice-dismiss]")
      ? event.target
      : null;
    if (!form || !window.fetch) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    var data = new FormData(form);
    fetch(form.action, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      // A redirect (to sign-in) is never a confirmation; fail it so the plain POST takes over.
      redirect: "manual",
      headers: {Accept: "application/json", "X-CSRFToken": data.get("csrfmiddlewaretoken")},
    })
      .then(function (response) {
        if (!response.ok || response.type === "opaqueredirect") throw new Error("dismiss failed");
        return response.json();
      })
      .then(function (result) {
        if (!result || result.dismissed !== true) throw new Error("dismiss not saved");
        var note = form.closest("[data-investment-notice]");
        if (note) note.remove();
        // The focused button is gone; keep keyboard focus in the page.
        var main = document.getElementById("main-content");
        if (main) main.focus({preventScroll: true});
      })
      .catch(function () {
        form.submit();
      });
  });
})();
