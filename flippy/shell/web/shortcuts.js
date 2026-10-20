// Shortcuts. macOS: Flippy's own hotkeys, recorded on the page. COSMIC runs Flippy's shortcuts itself, so there
// the page lists what COSMIC has (flippy/linux/setup.py), adds the missing ones and opens COSMIC Settings.
(() => {
"use strict";
const F = window.F, el = F.el;

async function act(name, list) {
  try { fill(list, await F.call("setup_action", { name })); } catch (e) { F.toast(e.message); }
}

async function fill(list, s) {
  try { s = s || await F.call("setup_state"); } catch (e) { list.replaceChildren(F.empty(null, "Couldn\u2019t read COSMIC\u2019s shortcuts", e.message)); return; }
  const rows = (s.shortcuts || []).map((k) => el("div", { class: "row" },
    el("div", { class: "text" }, el("span", { class: "label" }, k.label), el("div", { class: "help" }, k.command)),
    el("div", { class: "control" }, F.tokens(k.keys))));
  const missing = s.shortcuts_missing || [];
  list.replaceChildren(
    rows.length ? el("div", { class: "card" }, rows) : null,
    missing.length ? el("div", { class: "card", style: "margin-top:16px" }, el("div", { class: "row" },
      el("div", { class: "text" }, el("span", { class: "label" }, "Not in COSMIC yet"),
        el("div", { class: "help" }, s.shortcuts_note || missing.join(", "))),
      el("div", { class: "control" }, el("button", { class: "btn primary", onclick: () => act("shortcuts", list) }, "Add them")))) : null,
    el("div", { class: "foot" }, el("span", { class: "sub", style: "margin:0" }, "COSMIC runs these. Change the keys in COSMIC Settings."),
      el("button", { class: "btn", onclick: () => act("keyboard", list) }, "Open COSMIC Settings")));
}

F.renderers.shortcuts = (p) => {
  if (F.state.platform === "darwin") return F.renderGeneric(p);
  const list = el("div");
  fill(list);
  return el("div", { class: "content" }, el("h1", {}, "Shortcuts"), list);
};
})();
