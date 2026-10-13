// Flippy's app window: the shell, navigation, search and the generic settings pages.
// Talks to Python only through window.webkit.messageHandlers.flippy (flippy/shell/bridge.py).
// Classic scripts sharing window.F, not modules: WebKit refuses module scripts from file URLs.
(() => {
"use strict";
const F = (window.F = {});
const pending = new Map();
let nextId = 1;
const handlers = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.flippy;
const listeners = {};

F.call = (op, args = {}) => {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    if (handlers) handlers.postMessage(JSON.stringify({ id, op, args }));
    else reject(new Error("no bridge"));
  });
};
F.on = (name, fn) => {
  (listeners[name] = listeners[name] || []).push(fn);
  return () => (listeners[name] = listeners[name].filter((x) => x !== fn));
};

// Python calls these.
window.flippy = {
  reply(msg) {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error));
  },
  event(name, data) {
    if (name === "setting") F.updateField(data);
    if (name === "route") F.go(data);
    (listeners[name] || []).forEach((fn) => fn(data));
  },
};

// ---- small DOM helper
F.el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "style") { if (v) node.style.cssText = v; }  // through CSSOM: the CSP forbids style attributes
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
};
const el = F.el;
F.svg = (name, cls) => { const s = document.createElement("span"); s.innerHTML = icon(name, cls); return s.firstChild; };
F.here = () => location.hash.slice(1) || "settings/general";

// ---- state
F.state = { pages: [] };
F.fields = new Map();          // field id -> field data, kept current by pushes
F.renderers = {};              // page id -> custom renderer (appearance.js)
const nav = document.getElementById("nav");
const page = document.getElementById("page");
const search = document.getElementById("search");

F.value = (id) => (F.fields.get(id) || {}).value;
F.label = (id, value) => {
  const f = F.fields.get(id);
  const c = f && f.choices && f.choices.find((x) => x.value === value);
  return c ? c.label : String(value);
};

// ---- window mode (light, dark, or the system's)
const media = window.matchMedia("(prefers-color-scheme: dark)");
F.applyMode = () => {
  const m = F.value("window.mode") || "system";
  const mode = m === "system" ? (media.matches ? "dark" : "light") : m;
  document.documentElement.dataset.mode = mode;
  F.call("chrome", { mode }).catch(() => {});
};
media.addEventListener("change", F.applyMode);

// ---- toast with undo
let toastTimer = null;
F.toast = (text, undo) => {
  const t = document.getElementById("toast");
  t.replaceChildren(el("span", {}, text));
  if (undo) t.append(el("button", { onclick: () => { t.hidden = true; undo(); } }, "Undo"));
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), undo ? 6000 : 2600);
};
// Set values and offer to put the old ones back.
F.undoable = async (values, message) => {
  const before = {};
  for (const id of Object.keys(values)) before[id] = F.value(id);
  const fields = await F.call("set_many", { values });
  fields.forEach(F.updateField);
  F.toast(message, async () => (await F.call("set_many", { values: before })).forEach(F.updateField));
};

// ---- dialogs and menus
F.dialog = (build) => {
  const scrim = document.getElementById("dialog");
  const close = () => { scrim.hidden = true; scrim.replaceChildren(); document.removeEventListener("keydown", esc); };
  const esc = (e) => { if (e.key === "Escape") close(); };
  const sheet = el("div", { class: "sheet", role: "dialog", "aria-modal": "true" });
  scrim.replaceChildren(sheet);
  scrim.onclick = (e) => { if (e.target === scrim) close(); };
  document.addEventListener("keydown", esc);
  build(sheet, close);
  scrim.hidden = false;
  const first = sheet.querySelector("textarea, input, button.primary, button");
  if (first) first.focus();
  return close;
};
// The usual dialog: a title, a line of text, Cancel and one action.
F.confirm = (title, text, label, fn) => F.dialog((sheet, close) => sheet.append(el("h3", {}, title), el("p", {}, text),
  F.actions(close, label, () => { close(); fn(); })));
F.actions = (close, label, fn) => el("div", { class: "actions" },
  el("button", { class: "btn", onclick: close }, "Cancel"), el("button", { class: "btn primary", onclick: fn }, label));
let openMenu = null;
F.menu = (anchor, items) => {
  if (openMenu) openMenu.remove();
  const r = anchor.getBoundingClientRect();
  const m = el("div", { class: "menu", role: "menu" }, items.map(([label, ic, fn, danger]) =>
    el("button", { role: "menuitem", class: danger ? "danger" : "", onclick: () => { close(); fn(); } }, F.svg(ic), label)));
  const close = () => { m.remove(); openMenu = null; document.removeEventListener("mousedown", away, true); };
  const away = (e) => { if (!m.contains(e.target)) close(); };
  document.body.append(m);
  m.style.left = Math.min(r.left, innerWidth - m.offsetWidth - 8) + "px";
  m.style.top = (r.bottom + 6 + m.offsetHeight > innerHeight ? r.top - m.offsetHeight - 6 : r.bottom + 6) + "px";
  setTimeout(() => document.addEventListener("mousedown", away, true));
  m.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); anchor.focus(); } });
  m.querySelector("button").focus();
  openMenu = m;
};

// ---- saving one setting
F.save = async (f, value, row) => {
  try {
    F.updateField(await F.call("set", { id: f.id, value }));
    if (row) row.classList.remove("error");
    return true;
  } catch (e) {
    if (row) {
      row.classList.add("error");
      const h = row.querySelector(".help");
      if (h) h.textContent = e.message;
    }
    return false;
  }
};

// ---- controls
const SYMBOLS = { cmd: "⌘", shift: "⇧", option: "⌥", opt: "⌥", alt: "⌥", ctrl: "⌃", control: "⌃" };
const ORDER = ["ctrl", "control", "option", "opt", "alt", "shift", "cmd"];
F.keycaps = (combo) => {
  const parts = String(combo).split("+").map((p) => p.trim().toLowerCase()).filter(Boolean);
  const key = parts.pop() || "";
  parts.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
  const name = key === "space" ? "Space" : key.length === 1 ? key.toUpperCase() : key[0].toUpperCase() + key.slice(1);
  return el("span", { class: "keys", "aria-label": combo }, parts.map((p) => el("kbd", {}, SYMBOLS[p] || p)), el("kbd", {}, name));
};
// Keycaps from ready-made tokens (COSMIC's accelerators: ["Super", "Shift", "Space"]).
F.tokens = (tokens) => el("span", { class: "keys", "aria-label": tokens.join(" ") }, tokens.map((t) => el("kbd", {}, t)));
const KEYNAMES = { " ": "space", Enter: "return", Tab: "tab", Escape: "escape" };
function recordShortcut(f, row, button) {
  let caps = row.querySelector(".keys");
  caps.classList.add("recording");
  caps.replaceChildren(el("kbd", {}, "Type a shortcut"));
  button.textContent = "Cancel";
  const stop = () => {
    document.removeEventListener("keydown", onKey, true);
    button.textContent = "Record";
    button.onclick = () => recordShortcut(f, row, button);
  };
  const onKey = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (["Meta", "Shift", "Alt", "Control"].includes(e.key)) return;
    const key = KEYNAMES[e.key] || (/^F\d+$/.test(e.key) ? e.key.toLowerCase() : e.code.startsWith("Key") ? e.code.slice(3).toLowerCase()
      : e.code.startsWith("Digit") ? e.code.slice(5) : e.key.toLowerCase());
    const mods = [e.ctrlKey && "ctrl", e.altKey && "option", e.shiftKey && "shift", e.metaKey && "cmd"].filter(Boolean);
    stop();
    if (!(await F.save(f, [...mods, key].join("+"), row))) caps.replaceWith(F.keycaps(F.value(f.id)));
  };
  button.onclick = () => { stop(); caps.replaceWith(F.keycaps(F.value(f.id))); };
  document.addEventListener("keydown", onKey, true);
}

F.controlFor = (f, row) => {
  const id = "f-" + f.id;
  switch (f.control) {
    case "choice":
      if (f.choices.length <= 3 && f.choices.every((c) => c.label.length <= 14))
        return el("div", { class: "seg", role: "radiogroup", "aria-label": f.label }, f.choices.map((c) =>
          el("button", { role: "radio", "aria-checked": String(c.value === f.value), onclick: () => F.save(f, c.value, row) }, c.label)));
      return el("div", { class: "select" },
        el("select", { id, onchange: (e) => F.save(f, f.choices[e.target.selectedIndex].value, row) },
          f.choices.map((c) => el("option", { selected: c.value === f.value }, c.label))), F.svg("chevron"));
    case "switch": {
      const on = f.on !== undefined ? f.value === f.on : f.value;
      return el("input", { id, type: "checkbox", role: "switch", class: "switch", checked: on,
        onchange: (e) => F.save(f, f.on !== undefined ? (e.target.checked ? f.on : f.off) : e.target.checked, row) });
    }
    case "slider": {
      const fmt = (v) => Number(v).toFixed(f.digits);
      const fill = (v) => ((v - f.min) / (f.max - f.min)) * 100 + "%";
      const out = el("span", { class: "pill" }, el("span", {}, fmt(f.value)), f.unit ? el("span", { class: "unit" }, f.unit) : null);
      const input = el("input", { id, type: "range", min: f.min, max: f.max, step: f.step, value: f.value, style: `--fill:${fill(f.value)}`,
        oninput: (e) => { out.firstChild.textContent = fmt(e.target.value); e.target.style.setProperty("--fill", fill(e.target.value)); },
        onchange: (e) => F.save(f, f.digits ? Number(e.target.value) : Math.round(e.target.value), row) });
      return el("div", { class: "slider" }, input, out);
    }
    case "list":
      return el("div", { class: "chips" }, f.value.length ? f.value.map((v) => el("span", { class: "chip" }, v,
        el("button", { "aria-label": "Remove " + v, onclick: () => F.save(f, f.value.filter((x) => x !== v), row) }, F.svg("close"))))
        : el("span", { class: "none" }, "None yet"));
    case "shortcut": {
      const caps = F.keycaps(f.value);
      if (F.state.platform !== "darwin") return caps;
      const button = el("button", { class: "btn quiet" }, "Record");
      button.onclick = () => recordShortcut(f, row, button);
      return [caps, button];
    }
    default:
      return el("input", { id, type: "text", value: f.value, spellcheck: "false", onchange: (e) => F.save(f, e.target.value, row) });
  }
};

// A setting shown only for some values of another (a theme's own options), or only where the desktop can do it.
const hidden = (f) => Boolean((f.when && !f.when.values.includes(F.value(f.when.id))) || (f.needs && (F.state.caps || {})[f.needs] === false));
F.fieldRow = (f) => {
  F.fields.set(f.id, f);
  const row = el("div", { class: "row", "data-id": f.id });
  row.hidden = hidden(f);
  const labelled = ["choice", "slider", "text", "switch"].includes(f.control) && !(f.control === "choice" && f.choices.length <= 3);
  row.append(
    el("div", { class: "text" },
      labelled ? el("label", { class: "label", for: "f-" + f.id }, f.label) : el("span", { class: "label" }, f.label),
      el("div", { class: "help" }, f.help || "")),
    el("div", { class: "control" }, F.controlFor(f, row)));
  return row;
};

F.updateField = (f) => {
  if (!f) return;
  F.fields.set(f.id, f);
  if (f.id === "window.mode") F.applyMode();
  page.querySelectorAll(`.row[data-id="${CSS.escape(f.id)}"]`).forEach((row) => {
    if (!row.contains(document.activeElement) || row.querySelector(".seg, .switch")) {
      const fresh = F.fieldRow(f);
      fresh.hidden = row.hidden;
      row.replaceWith(fresh);
    }
  });
  page.querySelectorAll(".row[data-id]").forEach((row) => {  // rows shown only for some values of this one
    const g = F.fields.get(row.dataset.id);
    if (g && g.when && g.when.id === f.id) row.hidden = hidden(g);
  });
  (listeners["field:" + f.id] || []).forEach((fn) => fn(f));
};

// ---- pages
F.foot = (p) => {
  const adv = p.fields.filter((f) => f.advanced);
  const foot = el("div", { class: "foot" });
  let box = null;
  if (adv.length) {
    const btn = el("button", { class: "disclosure", "aria-expanded": "false" }, "Advanced", F.svg("right"));
    box = el("div", { class: "card", hidden: true, style: "margin-top:12px" }, adv.map(F.fieldRow));
    btn.onclick = () => { const open = btn.getAttribute("aria-expanded") !== "true"; btn.setAttribute("aria-expanded", String(open)); box.hidden = !open; };
    foot.append(btn);
  } else foot.append(el("span"));
  foot.append(el("button", { class: "link", onclick: () => F.resetPage(p) }, "Reset to default"));
  return [foot, box];
};
F.resetPage = async (p) => {
  const before = await F.call("reset", { page: p.id });
  await F.reload();
  F.toast(`${p.title} reset to default`, async () => { await F.call("set_many", { values: before }); await F.reload(); });
};
F.load = async () => {
  F.state = await F.call("state");
  for (const p of F.state.pages) for (const f of p.fields) F.fields.set(f.id, f);
  F.applyMode();
};
F.reload = async () => { await F.load(); F.render(F.here()); };

// A row that does something instead of holding a setting (schema.ACTIONS): a button, or a key to know.
const actionRow = (a) => el("div", { class: "row" },
  el("div", { class: "text" }, el("span", { class: "label" }, a.label), el("div", { class: "help" }, a.help || "")),
  el("div", { class: "control" }, a.keys ? F.tokens([a.keys]) : el("button", { class: "btn", onclick: () =>
    F.call(a.op, a.args || {}).then(() => a.done && F.toast(a.done), (e) => F.toast(e.message)) }, a.button)));
F.renderGeneric = renderGeneric;
function renderGeneric(p) {
  const wrap = el("div", { class: "content" }, el("h1", {}, p.title));
  const actions = p.actions || [];
  const titles = [...new Set([...p.groups.map((g) => g.title), ...actions.map((a) => a.group)])];
  for (const title of titles) {
    const g = p.groups.find((x) => x.title === title);
    const rows = [...(g ? g.fields.filter((f) => !f.advanced).map(F.fieldRow) : []), ...actions.filter((a) => a.group === title).map(actionRow)];
    if (!rows.length) continue;
    if (title) wrap.append(el("h2", {}, title));
    wrap.append(el("div", { class: "card" }, rows));
  }
  wrap.append(...F.foot(p).filter(Boolean));
  return wrap;
}

F.empty = (glyph, heading, ...text) => el("div", { class: "empty" }, el("div", {},
  glyph ? el("div", { class: "glyph", html: icon(glyph) }) : null, el("strong", {}, heading), text.map((t) => el("p", {}, t))));

function renderSearch(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hits = [];
  for (const p of F.state.pages) for (const f of p.fields) {
    const hay = `${p.title} ${f.label} ${f.help || ""} ${(f.choices || []).map((c) => c.label).join(" ")}`.toLowerCase();
    if (words.every((w) => hay.includes(w))) hits.push([p, f]);
  }
  const wrap = el("div", { class: "content" }, el("h1", {}, "Search"));
  if (!hits.length) {
    wrap.append(F.empty(null, "No settings match", `Nothing for “${q}”.`));
    return wrap;
  }
  wrap.append(el("div", { class: "card" }, hits.map(([p, f]) => el("div", { class: "result", tabindex: "0",
    onclick: () => { search.value = ""; F.go(`settings/${p.id}`, f.id); },
    onkeydown: (e) => { if (e.key === "Enter") e.currentTarget.click(); } },
    el("div", {}, el("div", { class: "label" }, f.label), el("div", { class: "where" }, p.title)), F.svg("right")))));
  return wrap;
}

F.renderNav = renderNav;
function renderNav(route) {
  const item = (id, title, ic, extra) => el("li", {}, el("a", { href: "#" + id, "aria-current": route === id ? "page" : null }, F.svg(ic), title, extra || null));
  nav.replaceChildren(
    item("history", "History", "history"),
    item("setup", "Setup", "setup", F.setupPending ? el("span", { class: "badge", title: "Setup isn\u2019t finished" }) : null),
    el("li", { class: "group", role: "presentation" }, "Settings"),
    ...F.state.pages.map((p) => item("settings/" + p.id, p.title, p.icon)));
}

F.render = (route, focusId) => {
  const q = search.value.trim();
  renderNav(q ? "" : route);
  let view;
  if (q) view = renderSearch(q);
  else {
    const [kind, id] = route.split("/");
    if (kind === "settings") {
      const p = F.state.pages.find((x) => x.id === id) || F.state.pages[0];
      view = (F.renderers[p.id] || renderGeneric)(p);
    } else if (kind === "setup" && F.renderSetup) {
      view = F.renderSetup();
    } else {
      view = F.renderHistory();
    }
  }
  page.replaceChildren(view);
  F.call("route", { route: q ? "search" : route }).catch(() => {});
  if (focusId) {
    const row = page.querySelector(`[data-id="${CSS.escape(focusId)}"], [data-for~="${CSS.escape(focusId)}"]`);
    if (row) { row.hidden = false; row.scrollIntoView({ block: "center" }); row.classList.add("flash"); }
  } else page.scrollTop = 0;
};

F.go = (route, focusId) => {
  if (location.hash.slice(1) !== route) { F.pendingFocus = focusId; location.hash = route; }
  else F.render(route, focusId);
};
window.addEventListener("hashchange", () => {
  const f = F.pendingFocus;
  F.pendingFocus = null;
  F.render(F.here(), f);
  page.focus({ preventScroll: true });
});
search.addEventListener("input", () => F.render(F.here()));
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "f") { e.preventDefault(); search.focus(); search.select(); }
  if (e.key === "Escape" && document.activeElement === search && search.value) { search.value = ""; F.render(F.here()); }
});
})();
