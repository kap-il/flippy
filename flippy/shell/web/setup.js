// The Setup page: connect a subscription, grant permissions, learn the shortcut. Re-checked every second while
// it's showing, so a permission granted in System Settings ticks off here by itself.
(() => {
"use strict";
const F = window.F, el = F.el;
let last = null, timer = null;

const onSetup = () => location.hash.slice(1) === "setup";
const step = (ok, n, title) => el("h2", {}, el("span", { class: "step" + (ok ? " done" : "") }, ok ? F.svg("check") : n), title);
const status = (ok, yes, no) => el("span", { class: "status" + (ok ? " ok" : "") }, F.svg(ok ? "check" : "dots"), ok ? yes : no);

function providerRow(key, title, connected) {
  const logo = last.logos && last.logos[key];
  return el("div", { class: "row" },
    el("div", { class: "text with-icon" },
      logo ? el("img", { class: "logo " + key, src: logo, alt: "" }) : null,
      el("div", {}, el("span", { class: "label" }, title), status(connected, "Connected", "Not connected"))),
    el("div", { class: "control" }, el("button", { class: "btn", onclick: () => act(key) },
      connected ? "Reconnect…" : key === "claude" ? "Log in…" : "Sign in…")));
}

function permissionRow(key, title, help, ok) {
  return el("div", { class: "row" },
    el("div", { class: "text" }, el("span", { class: "label" }, title), el("div", { class: "help" }, help)),
    el("div", { class: "control" }, status(ok, "Allowed", "Not yet"), el("button", { class: "btn", onclick: () => act(key) }, "Open Settings")));
}

async function act(name) {
  try { show(await F.call("setup_action", { name })); } catch (e) { F.toast(e.message); }
}

function build(s) {
  const mac = F.state.platform === "darwin";
  const use = F.fields.get("provider.mode");
  const useRow = F.fieldRow(use);
  const note = useRow.querySelector(".help");
  note.textContent = s.note;
  if (s.attention) useRow.classList.add("attention");

  const wrap = el("div", { class: "content" },
    el("div", { class: "hero" }, el("img", { class: "mark", src: s.mark, alt: "" }),
      el("div", {}, el("h1", {}, "Hi, I’m Flippy."), el("p", { class: "lede" }, "Ask about anything on your screen. I’ll point it out."))));

  wrap.append(step(s.connected, "1", "Connect"),
    el("p", { class: "sub" }, "Use the subscription you’ve already got."),
    el("div", { class: "card" }, providerRow("claude", "Claude", s.claude), providerRow("codex", "ChatGPT", s.codex), useRow));

  if (s.act_note) wrap.append(el("h2", {}, "Desktop tasks"), el("div", { class: "card" }, el("div", { class: "row" },
    el("div", { class: "text" }, el("span", { class: "label" }, "When Flippy works in an app for you"), el("div", { class: "help" }, s.act_note)))));

  if (mac) {
    const perms = el("div", { class: "card" });
    if (s.app) perms.append(el("div", { class: "row" },
      el("div", { class: "text with-icon" }, el("div", { class: "drag-app", title: "Drag into the list in System Settings" }),
        el("div", {}, el("span", { class: "label" }, s.app_name), el("div", { class: "help" }, "Drag me into the list in System Settings, or use + there."))),
      el("div", { class: "control" }, el("button", { class: "btn quiet", onclick: () => act("reveal") }, "Show in Finder"))));
    perms.append(
      permissionRow("screen", "Screen Recording", "So I can see what you’re asking about.", s.screen),
      permissionRow("accessibility", "Accessibility", "For desktop actions and double-tap shortcuts.", s.accessibility),
      el("div", { class: "row" }, el("div", { class: "text" }, el("span", { class: "label" }, "Restart Flippy"),
        el("div", { class: "help" }, "macOS applies new permissions after a restart. In System Settings, add Flippy with + if dragging it in doesn’t work.")),
        el("div", { class: "control" }, el("button", { class: "btn", onclick: () => act("restart") }, "Restart"))));
    wrap.append(step(s.screen && s.accessibility, "2", "Permissions"),
      el("p", { class: "sub" }, "Let me see your screen and lend a hand."), perms);
  }

  // macOS: Flippy's own hotkeys. COSMIC: the shortcuts COSMIC runs for Flippy (flippy/linux/setup.py).
  const first = !mac && s.shortcuts && s.shortcuts.length ? s.shortcuts[0] : null;
  const shortcutsOk = mac || !(s.shortcuts_missing || []).length;
  wrap.append(step(shortcutsOk, mac ? "3" : "2", "Your shortcut"),
    el("p", { class: "sub" }, "That’s how you call me, from any app."),
    el("div", { class: "card" }, el("div", { class: "row" },
      el("div", { class: "text" }, el("span", { class: "label" }, mac ? "Ask about the screen" : first ? first.label : "Flippy’s shortcuts"),
        el("div", { class: "help" }, mac ? "There are more, like circling something or checking a video."
          : shortcutsOk ? "COSMIC runs these. Change them in COSMIC Settings." : "Some of Flippy\u2019s shortcuts aren\u2019t in COSMIC yet.")),
      el("div", { class: "control" }, mac ? F.keycaps(s.ask) : first ? F.tokens(first.keys) : null,
        !shortcutsOk ? el("button", { class: "btn", onclick: () => act("shortcuts") }, "Add them") : null,
        el("button", { class: "btn quiet", onclick: () => F.go("settings/shortcuts") }, mac ? "Change" : "All shortcuts")))));

  wrap.append(el("h2", {}, "Startup"), el("div", { class: "card" }, el("div", { class: "row" },
    el("div", { class: "text" }, el("label", { class: "label", for: "login-item" }, "Launch at login"),
      el("div", { class: "help" }, s.login_item_allowed ? `Flippy starts in the ${mac ? "menu bar" : "panel"} when you log in.`
        : mac ? "Run the installed Flippy.app to turn this on." : "Not available in the development profile.")),
    el("div", { class: "control" }, el("input", { type: "checkbox", role: "switch", class: "switch", id: "login-item", checked: s.login_item,
      disabled: !s.login_item_allowed, onchange: (e) => act(e.target.checked ? "login_on" : "login_off") })))));

  wrap.append(el("div", { class: "foot" }, el("span", { class: "sub", style: "margin:0" }, s.done ? "All set. You stay in control." : "You stay in control."),
    el("button", { class: "btn primary", onclick: () => F.call("close").catch(() => {}) }, "Done")));
  return wrap;
}

// The native, draggable app icon sits over .drag-app (flippy/mac/shell_window.py drag_target).
function placeDrag() {
  const spot = onSetup() && document.querySelector(".drag-app");
  if (!spot) return F.call("drag_target", { rect: null }).catch(() => {});
  const r = spot.getBoundingClientRect(), page = document.getElementById("page").getBoundingClientRect();
  const visible = r.bottom > page.top && r.top < page.bottom;
  F.call("drag_target", { rect: visible ? { x: r.left, y: r.top, w: r.width, h: r.height } : null }).catch(() => {});
}
document.getElementById("page").addEventListener("scroll", () => requestAnimationFrame(placeDrag), { passive: true });
window.addEventListener("resize", () => requestAnimationFrame(placeDrag));
window.addEventListener("hashchange", () => requestAnimationFrame(placeDrag));

function show(s) {
  const pending = !s.done;
  if (pending !== F.setupPending) { F.setupPending = pending; F.renderNav(location.hash.slice(1)); }
  const changed = JSON.stringify(s) !== JSON.stringify(last);
  last = s;
  if (!onSetup() || !changed) return;
  const page = document.getElementById("page");
  if (page.querySelector("select:focus")) return;  // don't close an open menu under the pointer
  const y = page.scrollTop;
  page.replaceChildren(build(s));
  page.scrollTop = y;
  requestAnimationFrame(placeDrag);
}

F.checkSetup = () => F.call("setup_state").then(show).catch(() => {});

F.renderSetup = () => {
  clearInterval(timer);
  timer = setInterval(() => (onSetup() ? F.checkSetup() : clearInterval(timer)), 1000);
  if (!last) {  // show() builds the page when the first state arrives
    F.checkSetup();
    return el("div", { class: "content" }, el("h1", {}, "Setup"));
  }
  requestAnimationFrame(placeDrag);
  return build(last);
};
F.on("field:provider.mode", () => { if (onSetup()) F.checkSetup(); });
})();
