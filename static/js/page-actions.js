(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    var opener = event.target instanceof Element ? event.target.closest("[data-open-dialog]") : null;
    if (!opener) return;
    var dialog = document.getElementById(opener.getAttribute("data-open-dialog"));
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });
})();
