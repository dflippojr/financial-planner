(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    var pending = form.getAttribute("data-pending-label");
    if (!pending || event.defaultPrevented) return;
    if (form.getAttribute("aria-busy") === "true") {
      // A second tap while the first send is still waiting must not post again.
      event.preventDefault();
      return;
    }
    form.setAttribute("aria-busy", "true");
    form.querySelectorAll("button[type=submit], button:not([type])").forEach(function (button) {
      button.setAttribute("data-idle-label", button.textContent);
      button.classList.add("btn-disabled");
      button.setAttribute("aria-disabled", "true");
      button.textContent = "";
      var spinner = document.createElement("span");
      spinner.className = "loading loading-spinner loading-sm";
      spinner.setAttribute("aria-hidden", "true");
      button.appendChild(spinner);
      button.appendChild(document.createTextNode(" " + pending));
    });
  });

  // Coming back to the page from the browser cache must not leave the form stuck.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[aria-busy=true][data-pending-label]").forEach(function (form) {
      form.removeAttribute("aria-busy");
      form.querySelectorAll("[data-idle-label]").forEach(function (button) {
        button.textContent = button.getAttribute("data-idle-label");
        button.removeAttribute("data-idle-label");
        button.classList.remove("btn-disabled");
        button.removeAttribute("aria-disabled");
      });
    });
  });

  document.addEventListener("click", function (event) {
    var opener = event.target instanceof Element ? event.target.closest("[data-open-dialog]") : null;
    if (!opener) return;
    var dialog = document.getElementById(opener.getAttribute("data-open-dialog"));
    if (dialog && typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });
})();
