(function () {
  "use strict";
  const KEY = "finance.ai-popover";
  const IDLE_MS = 5 * 60 * 1000;
  const pop = document.getElementById("ai-popover");
  const skip = document.getElementById("skip-ai-chat");
  const desktop = window.matchMedia("(min-width: 1024px)");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  function clearStored() { try { sessionStorage.removeItem(KEY); } catch (_) { /* Storage can be disabled. */ } }
  document.querySelectorAll('form[action="/sign-out/"], form[action="/logout/"]').forEach(function (form) {
    form.addEventListener("submit", clearStored);
  });
  if (!pop) { clearStored(); return; }
  if (!desktop.matches) { pop.remove(); return; }
  const form = pop.querySelector("#ai-popover-form");
  const composer = form.elements.prompt;
  const thread = pop.querySelector("[data-pop-thread]");
  const scroll = pop.querySelector("[data-pop-scroll]");
  const status = pop.querySelector("[data-pop-status]");
  const error = pop.querySelector("[data-pop-error]");
  const grip = pop.querySelector('[data-pop-action="move"]');
  const expand = pop.querySelector('[data-pop-action="expand"]');
  const body = pop.querySelector("#ai-popover-body");
  let state = {open: false, collapsed: false, pinned: false, conversationId: null,
    x: 260, y: 80, dragged: false, lastActivityAt: Date.now()};
  let opener = null;
  let moving = false;
  let drag = null;
  let draggedClick = false;
  let timer = null;
  let count = 0;
  let answerReady = false;
  let sending = false;
  let restoring = false;
  let generation = 0;
  let transition = null;
  let subject = "";
  function save() { try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch (_) { /* Optional persistence. */ } }
  function announce(text) { status.textContent = text; }
  function pending() { return sending || restoring || !!thread.querySelector("[data-chat-turn-url]"); }
  function schedule() {
    clearTimeout(timer);
    if (!state.open || state.collapsed || state.pinned || pending() || composer.value) return;
    timer = setTimeout(function () { collapse(true); }, Math.max(0, IDLE_MS - (Date.now() - state.lastActivityAt)));
  }
  function activity() { state.lastActivityAt = Date.now(); save(); schedule(); }
  function bounds() {
    const sidebar = document.querySelector(".drawer-side > div");
    const left = (sidebar ? sidebar.getBoundingClientRect().right : 0) + 8;
    return {left: left, right: Math.max(left, innerWidth - pop.offsetWidth - 8),
      bottom: Math.max(8, innerHeight - pop.offsetHeight - 8)};
  }
  function position(clamp) {
    if (clamp) {
      const area = bounds();
      state.x = Math.max(area.left, Math.min(state.x, area.right));
      state.y = Math.max(8, Math.min(state.y, area.bottom));
    }
    pop.style.setProperty("--ai-pop-x", state.x + "px");
    pop.style.setProperty("--ai-pop-y", state.y + "px");
  }
  function animate(frames, duration, target) {
    if (transition) transition.cancel();
    const element = target || pop;
    const motionFrames = reduced.matches ? [{opacity: 0.3}, {opacity: 1}] : frames;
    transition = element.animate(motionFrames, {duration: reduced.matches ? 100 : duration,
      easing: "cubic-bezier(0.2, 0, 0, 1)"});
    return transition.finished.catch(function () {});
  }
  function pillStatus() {
    const line = pop.querySelector("[data-pill-status]");
    line.replaceChildren(document.createTextNode(answerReady ? "Answer ready " : subject + " · " + count + " messages"));
    if (answerReady) {
      const badge = document.createElement("span");
      badge.className = "badge badge-primary badge-xs";
      badge.textContent = "New";
      line.appendChild(badge);
    }
  }
  function draw() {
    pop.hidden = !state.open || !desktop.matches;
    skip.hidden = pop.hidden;
    pop.classList.toggle("is-collapsed", state.collapsed);
    if (state.collapsed) {
      pop.setAttribute("role", "region");
      pop.removeAttribute("aria-modal");
      pop.removeAttribute("aria-labelledby");
      pop.setAttribute("aria-label", "Ask AI, collapsed");
    } else {
      pop.setAttribute("role", "dialog");
      pop.setAttribute("aria-modal", "false");
      pop.setAttribute("aria-labelledby", "ai-popover-title");
      pop.removeAttribute("aria-label");
    }
    body.hidden = state.collapsed;
    ["pin", "collapse"].forEach(function (action) {
      pop.querySelector('[data-pop-action="' + action + '"]').hidden = state.collapsed;
    });
    pop.querySelector("[data-pop-chat]").hidden = state.collapsed;
    pop.querySelector("[data-pill-status]").hidden = !state.collapsed;
    expand.hidden = !state.collapsed;
    pop.querySelector('[data-pop-action="pin"]').setAttribute("aria-pressed", String(state.pinned));
    pop.querySelector("[data-pin-status]").hidden = !state.pinned;
    form.elements.conversation_id.value = state.conversationId || "";
    pop.querySelector("[data-pop-chat]").href = pop.dataset.chatUrl + (state.conversationId ? "?c=" + state.conversationId : "");
    pillStatus();
    if (!pop.hidden) position(true);
    save();
  }
  function setContext(tile) {
    const heading = document.querySelector("main h1");
    const params = new URLSearchParams(pop.dataset.pageQuery);
    const dates = [params.get("date_from"), params.get("date_to")].filter(Boolean).join(" – ");
    subject = (heading ? heading.textContent.trim() : "This page") + (dates ? " · " + dates : "");
    // #342 supplies a registry tile through this hook; route/query remain the only sent context here.
    if (tile && typeof tile.label === "string") subject = tile.label;
    pop.querySelector("[data-pop-subject]").textContent = subject;
    pop.querySelector("[data-pop-context]").hidden = false;
    form.elements.page_route.value = pop.dataset.pageRoute;
    form.elements.page_query.value = pop.dataset.pageQuery;
    pillStatus();
  }
  function unavailable() {
    generation += 1;
    state.open = false;
    state.conversationId = null;
    count = 0;
    thread.replaceChildren();
    draw();
    clearStored();
    announce("That conversation is no longer available.");
  }
  function install(data) {
    state.conversationId = data.conversationId;
    count = data.messageCount;
    // Only member-checked, server-rendered markup enters the thread; no scripts are evaluated.
    const parsed = new DOMParser().parseFromString(data.html, "text/html");
    thread.replaceChildren(...Array.from(parsed.body.childNodes, function (node) { return document.importNode(node, true); }));
    thread.setAttribute("aria-busy", String(!!thread.querySelector("[data-chat-turn-url]")));
    if (window.ChatTurns) window.ChatTurns.start(thread);
    draw();
    scroll.scrollTop = scroll.scrollHeight;
  }
  async function loadThread() {
    const version = ++generation;
    restoring = true;
    schedule();
    try {
      const url = pop.dataset.threadUrl + (state.conversationId ? "?c=" + state.conversationId : "");
      const response = await fetch(url, {headers: {Accept: "application/json"}, credentials: "same-origin", redirect: "error"});
      if (version !== generation) return;
      if (response.status === 404 || response.status === 403) { unavailable(); return; }
      if (!response.ok) throw new Error("Thread unavailable");
      const data = await response.json();
      if (version === generation) { install(data); error.textContent = ""; }
    } catch (_) { error.textContent = "Chat could not load. Reopen it to try again."; }
    finally { restoring = false; schedule(); }
  }
  function collapse(automatic) {
    if (state.collapsed || (automatic && (state.pinned || pending() || composer.value))) return;
    const focused = pop.contains(document.activeElement);
    const height = pop.offsetHeight;
    state.collapsed = true;
    save();
    animate([{height: height + "px", width: "380px", borderRadius: "16px"},
      {height: "52px", width: "320px", borderRadius: "26px"}], 200);
    body.animate([{opacity: 1}, {opacity: 0}], {duration: reduced.matches ? 100 : 80}).finished.then(function () {
      if (!state.collapsed) return;
      draw();
      if (focused) expand.focus();
      announce(automatic ? "Chat collapsed after 5 minutes" : "Collapsed");
    }).catch(function () {});
    schedule();
  }
  function openExpanded() {
    state.collapsed = false;
    answerReady = false;
    draw();
    animate([{height: "52px", width: "320px", borderRadius: "26px"},
      {height: pop.offsetHeight + "px", width: "380px", borderRadius: "16px"}], 200);
    body.animate([{opacity: 0}, {opacity: 1}], {duration: reduced.matches ? 100 : 80,
      delay: reduced.matches ? 0 : 120, fill: "backwards"});
    composer.focus();
    activity();
  }
  function returnFocus() {
    const fallback = document.querySelector("main h1") || document.getElementById("main-content");
    const target = opener && opener.isConnected ? opener : fallback;
    if (target) {
      if (!target.matches("button, a, input, textarea, [tabindex]")) target.setAttribute("tabindex", "-1");
      target.focus();
    }
  }
  async function close() {
    if (!state.open) return;
    state.open = false;
    generation += 1;
    save();
    clearTimeout(timer);
    const rect = pop.getBoundingClientRect();
    const origin = opener && opener.isConnected ? opener.getBoundingClientRect() : null;
    await animate([{opacity: 1, transform: "scale(1)"},
      {opacity: 0, transform: origin ? "translate(" + (origin.x - rect.x) + "px," + (origin.y - rect.y) + "px) scale(0.1)" : "scale(0.95)"}], 160);
    if (!state.open) { draw(); returnFocus(); announce("Closed"); }
  }
  window.AiPopover = {
    open: function (options) {
      if (!desktop.matches) return;
      const args = options || {};
      opener = args.origin instanceof Element ? args.origin : document.activeElement;
      const wasOpen = state.open;
      state.open = true;
      state.collapsed = false;
      answerReady = false;
      setContext(args.tile);
      if (!state.dragged && opener instanceof Element) {
        const rect = opener.getBoundingClientRect();
        state.x = rect.right + 12;
        state.y = rect.top;
      }
      draw();
      if (!wasOpen) {
        const rect = opener instanceof Element ? opener.getBoundingClientRect() : {x: state.x, y: state.y};
        animate([{opacity: 0, transform: "translate(" + (rect.x - state.x) + "px," + (rect.y - state.y) + "px) scale(0.1)"},
          {opacity: 1, transform: "none"}], 240);
      }
      composer.focus();
      activity();
      return loadThread();
    }
  };
  pop.addEventListener("click", function (event) {
    if (draggedClick) { draggedClick = false; return; }
    const button = event.target.closest("[data-pop-action]");
    if (!button) return;
    switch (button.dataset.popAction) {
      case "pin": state.pinned = !state.pinned; draw(); announce(state.pinned ? "Pinned" : "Unpinned"); activity(); break;
      case "collapse": collapse(false); break;
      case "expand": openExpanded(); break;
      case "close": close(); break;
      case "move": moving = !moving; grip.setAttribute("aria-pressed", String(moving)); announce(moving ? "Move with arrow keys. Home resets. Enter or Escape finishes." : "Move finished"); break;
      case "context":
        form.elements.page_route.value = "";
        form.elements.page_query.value = "";
        subject = "No page context";
        pop.querySelector("[data-pop-context]").hidden = true;
        pillStatus(); announce("Context removed"); break;
    }
  });
  pop.addEventListener("keydown", function (event) {
    if (moving) {
      const step = event.shiftKey ? 64 : 16;
      const offsets = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]};
      if (offsets[event.key]) { state.x += offsets[event.key][0]; state.y += offsets[event.key][1]; }
      else if (event.key === "Home") { state.x = bounds().left; state.y = 80; state.dragged = false; }
      else if (event.key === "Enter" || event.key === "Escape") { moving = false; grip.setAttribute("aria-pressed", "false"); announce("Move finished"); event.preventDefault(); return; }
      else return;
      event.preventDefault();
      if (event.key !== "Home") state.dragged = true;
      position(true); activity(); announce("Moved to " + Math.round(state.x) + ", " + Math.round(state.y));
    } else if (event.key === "Escape") { event.preventDefault(); close(); }
  });
  grip.addEventListener("pointerdown", function (event) {
    if (event.button !== 0) return;
    drag = {id: event.pointerId, x: event.clientX - state.x, y: event.clientY - state.y, moved: false};
    grip.setPointerCapture(event.pointerId);
  });
  grip.addEventListener("pointermove", function (event) {
    if (!drag || drag.id !== event.pointerId) return;
    state.x = event.clientX - drag.x; state.y = event.clientY - drag.y;
    state.dragged = true; drag.moved = true; position(false); activity();
  });
  function drop() {
    if (!drag) return;
    const old = {x: state.x, y: state.y};
    const moved = drag.moved;
    drag = null; position(true);
    if (moved) {
      draggedClick = true;
      moving = false;
      grip.setAttribute("aria-pressed", "false");
      animate([{transform: "translate(" + (old.x - state.x) + "px," + (old.y - state.y) + "px)"}, {transform: "none"}], 200);
      announce("Moved to " + Math.round(state.x) + ", " + Math.round(state.y));
    }
    activity();
  }
  grip.addEventListener("pointerup", drop);
  grip.addEventListener("pointercancel", drop);
  ["pointerdown", "keydown", "wheel", "scroll", "focusin"].forEach(function (name) {
    pop.addEventListener(name, activity, {capture: true, passive: true});
  });
  composer.addEventListener("input", activity);
  pop.querySelectorAll("[data-pop-followup]").forEach(function (button) {
    button.addEventListener("click", function () {
      composer.value = button.textContent.trim(); composer.focus(); activity();
    });
  });
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (sending || restoring) return;
    sending = true;
    error.textContent = "";
    form.querySelector('[type="submit"]').disabled = true;
    activity();
    const version = generation;
    try {
      const response = await fetch(form.action, {method: "POST", body: new FormData(form),
        headers: {Accept: "application/json", "X-CSRFToken": form.elements.csrfmiddlewaretoken.value},
        credentials: "same-origin", redirect: "error"});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "Chat could not send your question.");
      if (version === generation) { composer.value = ""; install(data); }
    } catch (failure) { error.textContent = failure.message || "Chat could not send your question."; }
    finally { sending = false; form.querySelector('[type="submit"]').disabled = false; activity(); }
  });
  thread.addEventListener("chat:answered", function () {
    thread.setAttribute("aria-busy", String(!!thread.querySelector("[data-chat-turn-url]")));
    if (state.collapsed) answerReady = true;
    count = thread.querySelectorAll("article").length;
    position(true); pillStatus(); activity();
  });
  thread.addEventListener("chat:gone", unavailable);
  thread.addEventListener("chat:proposals", function () { loadThread(); });
  window.addEventListener("resize", function () { draw(); schedule(); });
  skip.addEventListener("click", function (event) { event.preventDefault(); (state.collapsed ? expand : composer).focus(); });
  setContext();
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY));
    if (saved && typeof saved.open === "boolean" && Number.isFinite(saved.x) && Number.isFinite(saved.y)
      && Number.isFinite(saved.lastActivityAt)
      && (saved.conversationId === null || Number.isSafeInteger(saved.conversationId))) {
      Object.keys(state).forEach(function (key) { if (typeof saved[key] === typeof state[key] || key === "conversationId") state[key] = saved[key]; });
    }
  } catch (_) { clearStored(); }
  if (state.open) {
    draw();
    pop.querySelector("[data-page-divider]").hidden = false;
    pop.querySelector("[data-page-divider]").textContent = "Now about: " + subject;
    // Resolve pending turns before deciding whether an old idle window should roll up.
    loadThread().then(function () {
      if (!state.pinned && !pending() && Date.now() - state.lastActivityAt >= IDLE_MS) {
        state.collapsed = true; draw();
      }
      announce("Restored"); schedule();
    });
  }
})();
