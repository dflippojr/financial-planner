(function () {
  // Only the full Chat page reserves space; the floating chat uses its own layout.
  var page = document.querySelector(".chat-page");
  if (!page) return;
  var composer = page.querySelector("#chat-send-form");

  // Land at the newest turn (#686). The URL's anchor wins: chat_send points at the question just
  // sent, a proposal reload at its reply, apply and dismiss at the card. Without one, the latest
  // question is the start of the newest exchange. A flashed alert at the top keeps the page there.
  // The scroll is instant, so reduced motion needs no case, and focus never moves.
  function anchored() {
    var id = window.location.hash.slice(1);
    if (!id) return null;
    try {
      return page.querySelector("#" + CSS.escape(decodeURIComponent(id)));
    } catch (error) {
      return null;
    }
  }

  function target() {
    var found = anchored();
    if (found) return found;
    if (page.querySelector(":scope > .alert")) return null;
    var turns = page.querySelectorAll('article[data-chat-role="user"]');
    if (!turns.length) turns = page.querySelectorAll("article[data-chat-role]");
    return turns.length ? turns[turns.length - 1] : null;
  }

  function visibleBottom() {
    var bottom = window.innerHeight;
    return composer ? Math.min(bottom, composer.getBoundingClientRect().top) : bottom;
  }

  function land() {
    var el = target();
    if (!el) return null;
    el.scrollIntoView({block: "start"});
    // A reply's cards can sit below a long answer; bring the first one up so it is seen.
    var card = el.matches("article") ? el.querySelector("[data-chat-proposal]") : null;
    if (card && card.getBoundingClientRect().top > visibleBottom() - 48) card.scrollIntoView({block: "start"});
    return window.scrollY;
  }

  var landed = land();
  if (landed !== null) {
    // Charts and the composer's measured height settle late; land again unless the reader moved.
    window.addEventListener("load", function () {
      if (window.scrollY === landed) land();
    }, {once: true});
  }

  if (!composer || !window.ResizeObserver) return;
  var observer = new ResizeObserver(function () {
    page.style.setProperty("--chat-composer-height", composer.getBoundingClientRect().height + "px");
  });
  observer.observe(composer);
})();
