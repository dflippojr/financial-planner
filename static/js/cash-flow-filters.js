(function () {
  var filters = document.getElementById("cash-flow-filters");
  if (!filters) return;
  var panel = filters.querySelector(".filters-panel");
  var header = filters.querySelector("[data-filters-header]");
  // Keep native scrolling clear of the header, including wrapped or enlarged text.
  new ResizeObserver(function () {
    var height = header.getBoundingClientRect().height;
    if (height) panel.style.setProperty("--filters-header-height", height + "px");
  }).observe(header);
  function close() {
    filters.open = false;
    filters.querySelector("summary").focus();
  }
  filters.querySelector("[data-close-filters]").addEventListener("click", close);
  filters.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || event.defaultPrevented || !filters.open) return;
    event.preventDefault();
    close();
  });
})();
