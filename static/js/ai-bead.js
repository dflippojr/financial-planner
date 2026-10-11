(function () {
  "use strict";
  const desktop = window.matchMedia("(min-width: 1024px)");
  const coarse = window.matchMedia("(hover: none)");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  document.querySelectorAll("[data-ai-bead]").forEach(function (bead) {
    const tile = bead.closest("section, article, figure");
    if (!tile) return;
    tile.classList.add("ai-tile");
    let frame = null;
    let hiding = null;
    let pointerX = 0;
    let onBead = false;
    let keyboard = false;
    let touch = false;
    function home() {
      tile.style.setProperty("--ai-bead-x", (tile.clientWidth - 24) + "px");
    }
    function track() {
      frame = null;
      if (onBead || keyboard || touch || coarse.matches || reduced.matches) return;
      const rect = tile.getBoundingClientRect();
      tile.style.setProperty("--ai-bead-x", Math.max(36, Math.min(pointerX - rect.left, rect.width - 36)) + "px");
    }
    function show(event) {
      clearTimeout(hiding);
      touch = event.pointerType === "touch";
      tile.classList.toggle("ai-bead-touch", touch);
      pointerX = event.clientX;
      if (touch || coarse.matches || reduced.matches || keyboard) home();
      else track();
      tile.classList.add("ai-bead-visible");
    }
    home();
    tile.addEventListener("pointerenter", show);
    tile.addEventListener("pointermove", function (event) {
      if (event.pointerType === "touch" || touch) { show(event); return; }
      pointerX = event.clientX;
      if (frame === null) frame = requestAnimationFrame(track);
    });
    tile.addEventListener("pointerleave", function (event) {
      if (touch || event.pointerType === "touch") return;
      hiding = setTimeout(function () { tile.classList.remove("ai-bead-visible"); }, 300);
    });
    bead.addEventListener("pointerenter", function () { onBead = true; });
    bead.addEventListener("pointerleave", function () { onBead = false; });
    tile.addEventListener("keydown", function (event) {
      if (event.key === "Tab") { keyboard = true; tile.classList.add("ai-bead-keyboard"); home(); }
    });
    tile.addEventListener("focusin", function (event) {
      if (event.target.matches(":focus-visible")) { keyboard = true; tile.classList.add("ai-bead-keyboard"); home(); }
    });
    tile.addEventListener("focusout", function (event) {
      if (!tile.contains(event.relatedTarget)) { keyboard = false; tile.classList.remove("ai-bead-keyboard"); }
    });
    bead.addEventListener("click", function () {
      if (!window.AiPopover) return;
      const context = JSON.parse(bead.dataset.tileContext);
      context.label = bead.dataset.tileLabel;
      context.prompts = JSON.parse(bead.dataset.tilePrompts);
      window.AiPopover.open({origin: bead, tile: context});
    });
    function reset() {
      home();
      if (!desktop.matches) tile.classList.remove("ai-bead-visible");
    }
    window.addEventListener("resize", reset);
    coarse.addEventListener("change", reset);
    reduced.addEventListener("change", reset);
  });
})();
