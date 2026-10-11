(function () {
  // Only the full Chat page reserves space; the floating chat uses its own layout.
  var page = document.querySelector(".chat-page");
  if (!page) return;
  var composer = page.querySelector("#chat-send-form");
  if (!composer || !window.ResizeObserver) return;
  var observer = new ResizeObserver(function () {
    page.style.setProperty("--chat-composer-height", composer.getBoundingClientRect().height + "px");
  });
  observer.observe(composer);
})();
