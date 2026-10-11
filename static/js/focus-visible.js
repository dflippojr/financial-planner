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
    // A checkbox's label is its effective activation region, not just the glyph.
    const target = active.closest('.form-check-label, .form-choices label') || active;
    const panel = active.closest('.filters-panel');
    if (!dockBox.height && !panel) return;
    // Also reveal controls inside horizontally scrolling settings sections.
    // Native keyboard scrolling does not consistently expose those in WebKit.
    if (active !== main) target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    // Native focus scrolling can leave a field behind a sticky panel header.
    // Apply the panel's scroll padding before handling the viewport/dock below.
    if (panel && !active.closest('[data-filters-header]')) {
      const panelBox = panel.getBoundingClientRect();
      const panelStyle = getComputedStyle(panel);
      const paddingTop = parseFloat(panelStyle.scrollPaddingTop) || 0;
      const header = panel.querySelector('[data-filters-header]');
      const panelTop = Math.max(panelBox.top + panel.clientTop + paddingTop, header.getBoundingClientRect().bottom);
      const panelBottom = panelBox.top + panel.clientTop + panel.clientHeight;
      const targetBox = target.getBoundingClientRect();
      if (targetBox.top < panelTop) {
        const distance = targetBox.top - panelTop;
        // Below md the form flows with the page; its header sticks to the viewport.
        if (['auto', 'scroll'].includes(panelStyle.overflowY)) {
          panel.scrollTop += distance;
        } else {
          window.scrollBy(0, distance);
        }
      } else if (targetBox.bottom > panelBottom) {
        panel.scrollTop += targetBox.bottom - panelBottom;
      }
    }
    if (!dockBox.height) return;
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
