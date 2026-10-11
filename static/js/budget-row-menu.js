(function () {
  var table = document.querySelector("[data-budget-table]");
  if (!table) return;

  function position(menu, list) {
    var trigger = menu.querySelector("summary").getBoundingClientRect();
    if (!trigger.width || trigger.bottom < 0 || trigger.top > window.innerHeight) {
      menu.open = false;
      list.hidePopover();
      return;
    }
    var box = list.getBoundingClientRect();
    var left = Math.max(8, Math.min(trigger.right - box.width, window.innerWidth - box.width - 8));
    var top = trigger.bottom;
    if (top + box.height > window.innerHeight - 8 && trigger.top >= box.height + 8) {
      top = trigger.top - box.height;
    }
    top = Math.max(8, Math.min(top, window.innerHeight - box.height - 8));
    list.style.left = left + "px";
    list.style.top = top + "px";
  }

  table.addEventListener("toggle", function (event) {
    var menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.hasAttribute("data-row-menu")) return;
    var list = menu.querySelector(".row-menu-list");
    if (typeof list.showPopover !== "function") return;
    if (menu.open) {
      list.classList.add("budget-menu-popover");
      list.setAttribute("popover", "manual");
      list.showPopover();
      position(menu, list);
    } else if (list.matches(":popover-open")) {
      list.hidePopover();
    }
  }, true);

  var pending = false;
  function reposition() {
    if (pending) return;
    pending = true;
    window.requestAnimationFrame(function () {
      pending = false;
      table.querySelectorAll("details[data-row-menu][open]").forEach(function (menu) {
        var list = menu.querySelector(".budget-menu-popover:popover-open");
        if (list) position(menu, list);
      });
    });
  }
  window.addEventListener("resize", reposition);
  document.addEventListener("scroll", reposition, true);
})();
