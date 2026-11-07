(function () {
  // Dismiss buttons on notices (templates/finance/_investment_notice.html, #699). The form posts
  // on its own without JavaScript; this sends it with fetch and removes the note in place. Any
  // failure, including a notice whose wording changed, falls back to the plain POST, which
  // redirects back to the page.
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
      headers: {Accept: "application/json", "X-CSRFToken": data.get("csrfmiddlewaretoken")},
    })
      .then(function (response) {
        if (!response.ok) throw new Error("dismiss failed");
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
