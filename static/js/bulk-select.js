(function () {
  // Select mode (issues #221 and #330): the row checkboxes and the bulk-edit panel stay hidden
  // behind a Select button. The page renders data-select="nojs" so that, without this script,
  // they stay visible from md up; this script starts Select mode off.
  var root = document.getElementById("transaction-list-root");
  var toggles = document.querySelectorAll("[data-select-toggle]");
  if (root && toggles.length) {
    root.setAttribute("data-select", "off");

    var setMode = function (on) {
      root.setAttribute("data-select", on ? "on" : "off");
      toggles.forEach(function (toggle) {
        toggle.setAttribute("aria-pressed", on ? "true" : "false");
        toggle.textContent = on ? "Done" : "Select";
      });
    };

    toggles.forEach(function (toggle) {
      toggle.addEventListener("click", function () {
        setMode(root.getAttribute("data-select") !== "on");
      });
    });

    // Escape leaves Select mode and returns focus to the Select button the person can see,
    // unless something else (an open menu or dialog) already handled the key.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (root.getAttribute("data-select") !== "on") return;
      if (event.target instanceof Element && event.target.closest("details[data-row-menu][open], dialog[open]")) return;
      setMode(false);
      for (var i = 0; i < toggles.length; i += 1) {
        if (toggles[i].offsetParent !== null) {
          toggles[i].focus();
          break;
        }
      }
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
