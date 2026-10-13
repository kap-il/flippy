// The Appearance page: window mode, the theme gallery (drawn by the themes), the pointer gallery and studio, cards.
(() => {
"use strict";
const F = window.F, el = F.el;
const TINTS = { smoke: "#6e6e78", blue: "#3d7bf2", purple: "#8f5cf0", pink: "#ef5fa7", red: "#ec4b43", orange: "#f08a2c", green: "#3fbf6a", teal: "#2fb5b0" };
let gallery = null;         // {themes, pointers} from Python, cached while the window is open
let loading = null;

const loadGallery = () => (loading = loading || F.call("gallery").then((g) => { gallery = g; loading = null; return g; }));
const refreshPointers = async () => { if (gallery) gallery.pointers = await F.call("pointers"); };
const refresh = async () => { await refreshPointers(); F.rerender(); };
const onPage = () => location.hash.endsWith("/appearance") && !document.querySelector(".studio");

// ---- mode thumbnails: little pictures of this window in light and dark
function modeShot(kind) {
  const L = { bg: "#f6f6f7", side: "#ececef", card: "#ffffff", line: "#dcdce2", text: "#b9b9c2" };
  const D = { bg: "#141416", side: "#1c1c1f", card: "#202024", line: "#2e2e33", text: "#4a4a52" };
  const pane = (c, x, w, clip) => `<g ${clip ? `clip-path="url(#${clip})"` : ""}>
    <rect x="${x}" width="${w}" height="72" fill="${c.bg}"/><rect x="${x}" width="30" height="72" fill="${c.side}"/>
    <rect x="${x + 6}" y="10" width="16" height="3" rx="1.5" fill="${c.text}"/><rect x="${x + 6}" y="18" width="12" height="3" rx="1.5" fill="${c.text}"/>
    <rect x="${x + 38}" y="12" width="28" height="5" rx="2" fill="${c.text}"/>
    <rect x="${x + 38}" y="24" width="66" height="36" rx="5" fill="${c.card}" stroke="${c.line}"/>
    <rect x="${x + 45}" y="32" width="34" height="3" rx="1.5" fill="${c.text}"/><rect x="${x + 45}" y="42" width="48" height="3" rx="1.5" fill="${c.text}"/>
    <rect x="${x + 86}" y="49" width="12" height="6" rx="3" fill="#7c83f2"/></g>`;
  const body = kind === "light" ? pane(L, 0, 112) : kind === "dark" ? pane(D, 0, 112)
    : `<defs><clipPath id="half"><rect x="56" width="56" height="72"/></clipPath></defs>${pane(L, 0, 112)}${pane(D, 0, 112, "half")}`;
  const wrap = document.createElement("span");
  wrap.innerHTML = `<svg class="shot" viewBox="0 0 112 72" aria-hidden="true">${body}</svg>`;
  return wrap.firstChild;
}
function modeRow() {
  const f = F.fields.get("window.mode");
  const row = el("div", { class: "row stack", "data-mode-row": "" });
  const pick = (v) => F.undoable({ "window.mode": v }, `Window set to ${F.label("window.mode", v).toLowerCase()}`);
  row.append(el("div", { class: "text" }, el("span", { class: "label" }, f.label), el("div", { class: "help" }, f.help)),
    el("div", { class: "modes", role: "radiogroup", "aria-label": "Window mode" }, f.choices.map((c) =>
      el("button", { class: "mode", role: "radio", "aria-checked": String(f.value === c.value), onclick: () => pick(c.value) }, modeShot(c.value), c.label))));
  return row;
}

// ---- theme gallery
function themeTiles() {
  const current = F.value("look.theme");
  const grid = el("div", { class: "gallery", role: "radiogroup", "aria-label": "Theme" });
  const items = gallery ? gallery.themes : (F.fields.get("look.theme").choices.map((c) => ({ key: c.value, name: c.label })));
  for (const t of items) {
    grid.append(el("button", { class: "tile", role: "radio", "aria-checked": String(t.key === current), "data-key": t.key,
      onclick: () => t.key !== F.value("look.theme") && F.undoable({ "look.theme": t.key }, `Theme set to ${t.name}`) },
      el("span", { class: "art" }, t.image ? el("img", { src: t.image, alt: "" }) : el("span", { class: "skeleton" })),
      el("span", { class: "name" }, t.name, F.svg("check", "check"))));
  }
  return grid;
}

function tintRow() {
  const color = F.fields.get("look.glass_color"), strength = F.fields.get("look.glass_tint");
  const row = el("div", { class: "row stack", "data-id": "tint", "data-for": "look.glass_color look.glass_tint" });
  const mac = F.state.platform === "darwin";
  if (!mac && !["glass", "mediaplayer"].includes(F.value("look.theme"))) row.hidden = true;
  const fill = (v) => v * 100 + "%";
  const out = el("span", { class: "pill" }, el("span", {}, Math.round(strength.value * 100)), el("span", { class: "unit" }, "%"));
  row.append(
    el("div", { class: "text" }, el("span", { class: "label" }, "Tint"),
      el("div", { class: "help" }, mac ? "Glass and Media Player cards, and the ask box." : "Glass and Media Player cards.")),
    el("div", { class: "control tint-row" },
      el("div", { class: "tints", role: "radiogroup", "aria-label": "Tint color" }, color.choices.map((c) =>
        el("button", { class: "tint", role: "radio", title: c.label, "aria-label": c.label, "aria-checked": String(c.value === color.value),
          style: `background:${TINTS[c.value] || "#888"}`, onclick: () => F.save(color, c.value, row) }))),
      el("div", { class: "slider" },
        el("input", { type: "range", min: strength.min, max: strength.max, step: strength.step, value: strength.value, "aria-label": "Tint strength",
          style: `--fill:${fill(strength.value)}`,
          oninput: (e) => { out.firstChild.textContent = Math.round(e.target.value * 100); e.target.style.setProperty("--fill", fill(e.target.value)); },
          onchange: (e) => F.save(strength, Number(e.target.value), row) }), out)));
  return row;
}

// ---- pointer gallery
function pointerTiles(host) {
  const current = F.value("look.pointer");
  const grid = el("div", { class: "pointers", role: "radiogroup", "aria-label": "Pointer" });
  const list = gallery ? gallery.pointers : [];
  for (const p of list) {
    const tile = el("div", { class: "ptr", role: "radio", tabindex: "0", "aria-checked": String(p.value === current), title: p.name,
      onclick: (e) => { if (!e.target.closest(".more") && p.value !== F.value("look.pointer")) F.undoable({ "look.pointer": p.value }, `Pointer set to ${p.name}`); },
      onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.currentTarget.click(); } } },
      el("span", { class: "art" }, el("img", { src: p.image, alt: "" })), el("span", { class: "name" }, p.name));
    if (p.custom) {
      const more = el("button", { class: "more", "aria-label": `More for ${p.name}`, onclick: (e) => { e.stopPropagation(); pointerMenu(more, p, host); } }, F.svg("dots"));
      tile.append(more);
    }
    grid.append(tile);
  }
  const add = (label, ic, fn) => el("button", { class: "ptr", onclick: fn }, el("span", { class: "art add", html: icon(ic) }), el("span", { class: "name" }, label));
  grid.append(
    add("Draw new", "plus", async () => openStudio(host, await F.call("studio_open", {}))),
    add("From image", "image", async () => {
      const r = await F.call("pointer_import").catch((e) => (F.toast(e.message), null));
      if (!r) return;
      if (r.saved) { await refresh(); F.toast("Pointer imported"); } else openStudio(host, r.studio);
    }));
  return grid;
}

function pointerMenu(anchor, p, host) {
  const name = p.value.slice("custom:".length);
  F.menu(anchor, [
    ["Edit", "pencil", async () => openStudio(host, await F.call("studio_open", { name }))],
    ["Duplicate", "copy", async () => { await F.call("pointer_duplicate", { name }); await refresh(); }],
    ["Rename", "rename", () => F.dialog((sheet, close) => {
      const input = el("input", { type: "text", value: name, maxlength: "40", spellcheck: "false", style: "width:100%" });
      const err = el("div", { class: "error" });
      const go = async () => {
        try { await F.call("pointer_rename", { name, new: input.value }); close(); await refresh(); }
        catch (e) { err.textContent = e.message; }
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      sheet.append(el("h3", {}, "Rename pointer"), input, err, F.actions(close, "Rename", go));
    })],
    ["Export as PNG", "export", async () => { if (await F.call("pointer_export", { name })) F.toast(`Saved “${name}.png” with its tip`); }],
    ["Delete", "trash", () => F.confirm(`Delete “${name}”?`, "The pointer file is removed. Export it first to keep a copy.", "Delete",
      async () => { await F.call("pointer_delete", { name }); await refresh(); F.toast(`Deleted “${name}”`); }), true],
  ]);
}

// The scripted pointer demo (flippy/demos.py): draw pixelart.DEMO_SPRITE like a person would, then save.
F.runPointerDemo = async (demo) => {
  F.go("settings/appearance");
  await new Promise((r) => setTimeout(r, 400));
  const host = document.getElementById("studio-host");
  if (!host) return;
  openStudio(host, await F.call("studio_open", {}), (value) => F.call("demo_saved", { value }));
  await new Promise((r) => setTimeout(r, 300));
  const d = F.studioDemo, [ox, oy] = [1, 1], wait = (ms) => new Promise((r) => setTimeout(r, ms)), ms = 34;
  document.querySelector(".studio").scrollIntoView({ block: "center" });
  const cells = (k) => demo.sprite.flatMap((row, r) => [...row].map((ch, c) => (ch === k ? [ox + c, oy + r] : null)).filter(Boolean));
  await wait(700);
  d.tool("pencil"); d.color(demo.colors.o);
  for (const [x, y] of cells("o")) { d.paint(x, y); await wait(ms); }
  await wait(ms * 6); d.tool("fill"); d.color(demo.colors.f); await wait(ms * 4); d.fill(ox + 2, oy + 6); await wait(ms * 8);
  d.tool("pencil"); d.color(demo.colors.h);
  for (const [x, y] of cells("h")) { d.paint(x, y); await wait(ms); }
  d.color(demo.colors.s);
  for (const [x, y] of cells("s")) { d.paint(x, y); await wait(ms); }
  await wait(ms * 6); d.tool("hotspot"); await wait(ms * 4); d.tip(ox, oy); await wait(ms * 16);
  d.name(demo.name); d.save();
};
F.on("demo-pointer", (demo) => F.runPointerDemo(demo));

let closeStudio = null;
function openStudio(host, data, after) {
  if (closeStudio) closeStudio();
  closeStudio = F.openStudio(data, host, async (value, name) => {
    if (after) after(value);
    await refresh();
    F.toast(`Saved “${name.trim() || "My pointer"}” and set it as your pointer`);
  });
  host.addEventListener("studio-closed", () => (closeStudio = null), { once: true });
}

// ---- looks: copy and import, like a theme file
function importLook() {
  F.dialog((sheet, close) => {
    const text = el("textarea", { placeholder: '{\n  "format": "flippy-look/1",\n  "theme": "glass",\n  ...\n}', spellcheck: "false", "aria-label": "Look" });
    const err = el("div", { class: "error" });
    const apply = async () => {
      try {
        const r = await F.call("look_import", { text: text.value });
        close();
        await F.reload();
        gallery = await F.call("gallery");
        F.rerender();
        const before = {};
        for (const [k, v] of Object.entries(r.before)) before["look." + k] = v;
        F.toast(r.skipped.length ? `Look applied, skipped ${r.skipped.join(", ")}` : "Look applied",
          async () => { await F.call("set_many", { values: before }); await F.reload(); });
      } catch (e) { err.textContent = e.message; }
    };
    sheet.append(el("h3", {}, "Import a look"),
      el("p", {}, "Paste a look someone copied from Flippy: theme, pointer (with its drawing), tint and card settings."),
      text, err, F.actions(close, "Apply", apply));
  });
}
async function copyLook() {
  const r = await F.call("look_copy");
  if (r.copied) return F.toast("Look copied. Paste it into Import on another Flippy.");
  F.dialog((sheet, close) => sheet.append(el("h3", {}, "Your look"), el("p", {}, "Copy this text:"),
    el("textarea", { readonly: true }, r.text), el("div", { class: "actions" }, el("button", { class: "btn primary", onclick: close }, "Done"))));
}

// ---- the page
F.renderers.appearance = (p) => {
  const wrap = el("div", { class: "content" }, el("h1", {}, "Appearance"));
  const rows = (ids) => ids.map((id) => F.fields.get(id)).filter(Boolean).map(F.fieldRow);

  wrap.append(el("h2", {}, "Window"), el("div", { class: "card", "data-for": "window.mode" }, modeRow()));

  wrap.append(el("h2", {}, "Look", el("span", { class: "spacer" }),
    el("button", { class: "icon-btn", title: "Import a look", "aria-label": "Import a look", onclick: importLook, html: icon("import") }),
    el("button", { class: "icon-btn", title: "Copy this look", "aria-label": "Copy this look", onclick: copyLook, html: icon("copy") })));
  wrap.append(el("div", { class: "card" },
    el("div", { class: "block", "data-for": "look.theme" }, el("div", { class: "head" }, el("div", {}, el("div", { class: "label" }, "Theme"),
      el("div", { class: "help" }, "How answer cards look. Each one is drawn by the theme itself."))), themeTiles()),
    tintRow(), ...rows(["look.player_shine", "look.frosted"])));

  const studioHost = el("div", { id: "studio-host" });
  wrap.append(el("h2", {}, "Pointer"), el("div", { class: "card" },
    el("div", { class: "block", "data-for": "look.pointer" }, el("div", { class: "head" }, el("div", {}, el("div", { class: "label" }, "Pointer"),
      el("div", { class: "help" }, "What points at things. Draw your own, or start from any image."))), pointerTiles(studioHost)),
    ...rows(["look.pointer_size"])), studioHost);

  wrap.append(el("h2", {}, "Cards"), el("div", { class: "card" }, ...rows(["look.text_size", "look.card_opacity", "look.controls"])));

  // anything else under look.* (added later) still shows up
  const known = new Set(["window.mode", "look.theme", "look.pointer", "look.glass_color", "look.glass_tint", "look.player_shine", "look.frosted",
    "look.pointer_size", "look.text_size", "look.card_opacity", "look.controls"]);
  const extra = p.fields.filter((f) => !known.has(f.id) && !f.advanced);
  if (extra.length) wrap.append(el("h2", {}, "More"), el("div", { class: "card" }, extra.map(F.fieldRow)));

  const [foot] = F.foot(Object.assign({}, p, { fields: [] }));
  foot.firstChild.replaceWith(el("button", { class: "btn", onclick: () => F.call("command", { name: "preview" }).then(() => F.toast("Showing the look on screen"), () => F.toast("Preview works inside Flippy")) },
    F.svg("play"), "Preview on screen"));
  foot.lastChild.onclick = () => F.resetPage(p).then(refresh);
  wrap.append(foot);

  if (!gallery) loadGallery().then(() => { if (location.hash.endsWith("/appearance")) F.rerender(); });
  return wrap;
};

F.rerender = () => {
  const y = document.getElementById("page").scrollTop;
  F.render(F.here());
  document.getElementById("page").scrollTop = y;
};

// keep the page in step with changes from anywhere (undo, another window, the daemon)
const sync = (sel, attr, value) => document.querySelectorAll(sel).forEach((n) => n.setAttribute("aria-checked", String(n.dataset[attr] === value)));
F.on("field:look.theme", async (f) => {
  sync(".tile[data-key]", "key", f.value);
  const tint = document.querySelector('[data-id="tint"]');
  if (tint && F.state.platform !== "darwin") tint.hidden = !["glass", "mediaplayer"].includes(f.value);
  await refreshPointers();  // "Theme default" shows the theme's own pointer
  if (onPage()) F.rerender();
});
F.on("field:look.pointer", () => { if (onPage()) F.rerender(); });
F.on("field:window.mode", () => { const r = document.querySelector("[data-mode-row]"); if (r) r.replaceWith(modeRow()); });
for (const id of ["look.glass_color", "look.glass_tint"]) F.on("field:" + id, () => { const r = document.querySelector('[data-id="tint"]'); if (r) r.replaceWith(tintRow()); });
let redraw = null;
for (const id of ["look.card_opacity", "look.controls", "look.player_shine"]) F.on("field:" + id, () => {
  clearTimeout(redraw);
  redraw = setTimeout(async () => { if (!gallery) return; gallery.themes = (await F.call("gallery")).themes;
    document.querySelectorAll(".tile[data-key]").forEach((t) => { const g = gallery.themes.find((x) => x.key === t.dataset.key); if (g) t.querySelector("img").src = g.image; }); }, 250);
});
})();
