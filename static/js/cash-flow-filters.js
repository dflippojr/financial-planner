(function () {
  var filters = document.getElementById("cash-flow-filters");
  if (!filters) return;
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
