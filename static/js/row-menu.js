(function () {
  // Row menus (templates/finance/_row_menu.html) are <details>: Enter and Space toggle them
  // natively. This adds Escape, outside clicks, and one open menu at a time.
  function openMenus() {
    return document.querySelectorAll("details[data-row-menu][open]");
  }

  function close(menu, refocus) {
    menu.open = false;
    if (refocus) menu.querySelector("summary").focus();
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var target = event.target instanceof Element ? event.target.closest("details[data-row-menu][open]") : null;
    openMenus().forEach(function (menu) {
      close(menu, menu === target);
    });
  });

  document.addEventListener("click", function (event) {
    var inside = event.target instanceof Element ? event.target.closest("details[data-row-menu]") : null;
    openMenus().forEach(function (menu) {
      if (menu !== inside) close(menu, false);
    });
  });

  document.addEventListener(
    "toggle",
    function (event) {
      var opened = event.target;
      if (!(opened instanceof HTMLDetailsElement) || !opened.open || !opened.hasAttribute("data-row-menu")) return;
      openMenus().forEach(function (menu) {
        if (menu !== opened) close(menu, false);
      });
    },
    true,
  );
})();
