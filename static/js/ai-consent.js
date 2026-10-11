(() => {
  "use strict";
  const dialog = document.getElementById("ai-consent");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const slot = document.getElementById("ai-slot-container");
  const announcement = document.getElementById("ai-consent-announcement");
  const dialogAnnouncement = dialog.querySelector("[data-ai-consent-announcement]");
  let opener = null;
  let busy = false;

  function focusReturn() {
    const target = opener && opener.isConnected ? opener :
      slot.querySelector("a") || document.getElementById("sidebar-settings");
    if (target) target.focus();
  }
  function open(trigger) {
    opener = trigger || slot.querySelector("[data-ai-consent-open]");
    announcement.textContent = "";
    dialogAnnouncement.textContent = "";
    if (!dialog.open) dialog.showModal();
    dialog.querySelector("#ai-consent-heading").focus();
  }
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-ai-consent-open]");
    if (trigger) {
      event.preventDefault();
      open(trigger);
    }
    if (event.target.closest("[data-ai-consent-close]")) {
      event.preventDefault();
      dialog.close();
      focusReturn();
    }
  });
  dialog.addEventListener("close", focusReturn);
  dialog.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("[data-ai-consent-form]")) return;
    event.preventDefault();
    if (busy || !event.submitter) return;
    busy = true;
    const action = event.submitter.value;
    const data = new FormData(form);
    data.set("action", action);
    const buttons = [...form.querySelectorAll("button")];
    buttons.forEach((button) => { button.disabled = true; });
    const error = dialog.querySelector("[data-ai-consent-error]");
    error.hidden = true;
    try {
      const response = await fetch(form.getAttribute("action"), {
        method: "POST", body: data, credentials: "same-origin",
        headers: { "Accept": "application/json", "X-CSRFToken": data.get("csrfmiddlewaretoken") },
      });
      const result = await response.json();
      if (!response.ok) {
        error.textContent = result.error || "Your response could not be saved. Try again.";
        if (result.policy_url) {
          const link = document.createElement("a");
          link.href = result.policy_url;
          link.className = "link ml-1";
          link.textContent = "Read the current policy";
          error.appendChild(link);
        }
        error.hidden = false;
        return;
      }
      slot.innerHTML = result.slot_html;
      document.querySelectorAll("[data-policy-prompt]").forEach((prompt) => prompt.remove());
      if (result.ai_slot.state === "denied") {
        opener = document.getElementById("sidebar-settings");
        dialog.close();
        dialog.remove();
        opener.focus();
        announcement.textContent = "AI is off. You can turn it on in Settings › AI.";
      } else {
        const next = new DOMParser().parseFromString(result.dialog_html, "text/html")
          .querySelector("[data-ai-consent-content]");
        const oldHeight = dialog.getBoundingClientRect().height;
        dialog.querySelector("[data-ai-consent-content]").replaceWith(next);
        const newHeight = dialog.getBoundingClientRect().height;
        if (!matchMedia("(prefers-reduced-motion: reduce)").matches && oldHeight !== newHeight) {
          dialog.animate([{ height: oldHeight + "px" }, { height: newHeight + "px" }],
            { duration: 160, easing: "ease" });
        }
        dialog.querySelector("#ai-consent-heading").focus();
        dialogAnnouncement.textContent = dialog.querySelector("[data-ai-consent-status]").textContent.trim();
      }
    } catch (_error) {
      error.textContent = "Your response could not be saved. Check your connection and try again.";
      error.hidden = false;
    } finally {
      busy = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  });
  // The open attribute supplies a navigable plain-form fallback without JavaScript.
  if (dialog.open) {
    dialog.close();
    open();
  }
})();
