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

  // <details data-open-below="md"> is the reverse: open on a phone, closed from that width up,
  // unless data-keep-open says it holds something the person must see (a field error).
  function bindBelow(details) {
    var min = widths[details.getAttribute("data-open-below")];
    if (!min || !window.matchMedia) return;
    var query = window.matchMedia("(min-width: " + min + "px)");
    details.open = !query.matches || details.hasAttribute("data-keep-open");
    query.addEventListener("change", function (event) {
      details.open = !event.matches || details.hasAttribute("data-keep-open");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("details[data-open-from]").forEach(bind);
    document.querySelectorAll("details[data-open-below]").forEach(bindBelow);
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

(function () {
  // <a data-open-details="id"> opens that <details> and moves focus to its first field.
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-open-details]");
    if (!link) return;
    var details = document.getElementById(link.getAttribute("data-open-details"));
    if (!details || details.tagName !== "DETAILS") return;
    event.preventDefault();
    details.open = true;
    var field = details.querySelector("input:not([type=hidden]), select, textarea");
    if (field) field.focus();
  });
})();
