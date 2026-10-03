(function () {
  var storageKey = "financial-planner-theme";

  function preferred() {
    try {
      var stored = window.localStorage.getItem(storageKey);
      if (stored === "light" || stored === "dark") {
        return stored;
      }
    } catch (error) {
      /* localStorage can throw in private mode */
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.style.colorScheme = theme;
  }

  apply(preferred());

  window.financialPlannerTheme = {
    preferred: preferred,
    apply: apply,
    toggle: function () {
      var next = preferred() === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(storageKey, next);
      } catch (error) {
        /* ignore quota / private-mode failures */
      }
      apply(next);
      window.dispatchEvent(
        new CustomEvent("financial-planner:themechange", { detail: { theme: next } }),
      );
      return next;
    },
  };

  function syncToggle(button) {
    var theme = preferred();
    var dark = theme === "dark";
    var label = dark ? "Switch to light theme" : "Switch to dark theme";
    button.setAttribute("aria-pressed", dark ? "true" : "false");
    button.setAttribute("aria-label", label);
    button.setAttribute("title", label);
    var tipHost = button.closest("[data-tip]");
    if (tipHost) {
      tipHost.setAttribute("data-tip", label);
    }
    var sun = button.querySelector(".theme-icon-sun");
    var moon = button.querySelector(".theme-icon-moon");
    if (sun) {
      sun.hidden = dark;
    }
    if (moon) {
      moon.hidden = !dark;
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", function () {
        window.financialPlannerTheme.toggle();
        syncToggle(button);
      });
      syncToggle(button);
    }
    document.querySelectorAll("[data-copy-target]").forEach(function (control) {
      control.addEventListener("click", function () {
        var target = document.getElementById(control.getAttribute("data-copy-target"));
        if (!target || !navigator.clipboard) {
          return;
        }
        navigator.clipboard.writeText(target.textContent).then(function () {
          control.textContent = "Copied";
        });
      });
    });
  });
})();
