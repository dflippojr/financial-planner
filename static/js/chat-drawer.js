(function () {
  // Ask about this page opens the chat drawer, whose thread already carries the
  // page's route and query. The checkbox keeps the drawer a CSS toggle; these
  // buttons make it reachable from the keyboard (issue #327).
  var opener = null;

  function setOpen(open) {
    var toggle = document.getElementById("finance-chat-drawer");
    var panel = document.getElementById("finance-chat-panel");
    if (!toggle || !panel) return;
    toggle.checked = open;
    document.querySelectorAll("[data-chat-drawer-open]").forEach(function (button) {
      button.setAttribute("aria-expanded", open ? "true" : "false");
    });
    if (open) {
      var field = panel.querySelector("textarea, input:not([type=hidden]), button");
      (field || panel).focus();
    } else if (opener) {
      opener.focus();
      opener = null;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-chat-drawer-open]").forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
      button.addEventListener("click", function () {
        opener = button;
        setOpen(true);
      });
    });
    document.querySelectorAll("[data-chat-drawer-close]").forEach(function (button) {
      button.addEventListener("click", function () {
        setOpen(false);
      });
    });
    document.addEventListener("keydown", function (event) {
      var toggle = document.getElementById("finance-chat-drawer");
      if (event.key === "Escape" && toggle && toggle.checked) {
        setOpen(false);
      }
    });
  });
})();
