(function () {
  // <details data-open-from="md"> starts closed on a phone and is always open from that width up.
  var widths = { sm: 640, md: 768, lg: 1024 };

  function bind(details) {
    var min = widths[details.getAttribute("data-open-from")];
    if (!min || !window.matchMedia) return;
    var query = window.matchMedia("(min-width: " + min + "px)");
    details.open = query.matches;
    query.addEventListener("change", function (event) {
      details.open = event.matches;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("details[data-open-from]").forEach(bind);
  });
})();

(function () {
  // A dialog whose form failed server-side validation reopens so the errors are visible.
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("dialog[data-show-on-load]").forEach(function (dialog) {
      if (typeof dialog.showModal === "function") dialog.showModal();
    });
  });
})();
