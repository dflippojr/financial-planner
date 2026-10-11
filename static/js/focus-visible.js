// Keep sequential focus above the fixed phone dock, including after rotation.
(function () {
  const main = document.getElementById('main-content');
  const dock = document.querySelector('nav.dock');
  if (!main || !dock) return;
  let keyboardNavigation = false;
  let revealVersion = 0;

  function revealFocus() {
    const active = document.activeElement;
    if (!main.contains(active) || !active.matches('input, select, textarea, button, a, summary, [tabindex]') || active.closest('dialog')) return;
    const dockBox = dock.getBoundingClientRect();
    if (!dockBox.height) return;
    // A checkbox's label is its effective activation region, not just the glyph.
    const target = active.closest('.form-check-label, .form-choices label') || active;
    // Also reveal controls inside horizontally scrolling settings sections.
    // Native keyboard scrolling does not consistently expose those in WebKit.
    if (active !== main) target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const box = target.getBoundingClientRect();
    const viewport = window.visualViewport;
    const top = (viewport ? viewport.offsetTop : 0) + 8;
    const bottom = Math.min(dockBox.top, viewport ? viewport.offsetTop + viewport.height : window.innerHeight) - 8;
    if (box.height > bottom - top || box.top < top) {
      window.scrollBy(0, box.top - top);
    } else if (box.bottom > bottom) {
      window.scrollBy(0, box.bottom - bottom);
    }
  }

  function scheduleReveal() {
    // Native focus scrolling runs first; then measure its settled geometry.
    const version = ++revealVersion;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (version === revealVersion) revealFocus();
    }));
  }

  document.addEventListener('keydown', event => {
    if (event.key === 'Tab') keyboardNavigation = true;
  });
  function allowManualScroll() {
    keyboardNavigation = false;
    ++revealVersion;
  }
  for (const event of ['pointerdown', 'touchstart', 'touchmove', 'wheel']) {
    document.addEventListener(event, allowManualScroll, { capture: true, passive: true });
  }
  function revealAfterKeyboardResize() {
    if (keyboardNavigation) scheduleReveal();
  }
  document.addEventListener('focusin', scheduleReveal);
  window.addEventListener('resize', revealAfterKeyboardResize);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', revealAfterKeyboardResize);
  }
}());
