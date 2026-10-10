(function () {
  // A [data-print-page] button opens the browser's print dialog; the CSP blocks inline handlers.
  document.addEventListener("click", function (event) {
    var button = event.target instanceof Element ? event.target.closest("[data-print-page]") : null;
    if (!button) return;
    var menu = button.closest("details[open]");
    if (menu) menu.open = false;
    window.print();
  });
})();
