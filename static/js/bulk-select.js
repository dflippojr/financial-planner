(function () {
  var page = document.getElementById("select-page");
  if (!page) return;
  page.addEventListener("change", function () {
    document.querySelectorAll(".js-bulk-row").forEach(function (box) {
      box.checked = page.checked;
    });
  });
})();
