(function () {
  // Phones keep the row checkboxes and the bulk-edit bar behind a Select button.
  var toggle = document.querySelector("[data-select-toggle]");
  var root = document.getElementById("transaction-list-root");
  if (toggle && root) {
    toggle.addEventListener("click", function () {
      var on = root.getAttribute("data-select") !== "on";
      root.setAttribute("data-select", on ? "on" : "off");
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
      toggle.textContent = on ? "Done" : "Select";
    });
  }

  var page = document.getElementById("select-page");
  if (!page) return;
  page.addEventListener("change", function () {
    document.querySelectorAll(".js-bulk-row").forEach(function (box) {
      box.checked = page.checked;
    });
  });
})();
