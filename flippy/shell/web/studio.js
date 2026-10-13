// The pointer studio: Kap's pointer editor (flippy/mac/pointer_editor.py, flippy/linux/pointer_editor.py) in the page,
// with the same tools and look. Python keeps the files: this only sends pixels, a smooth canvas or an image, and the tip.
(() => {
"use strict";
const F = window.F, el = F.el;
const CELL = 18;
const SPRITE_PX = 4;          // themes.sprite_px(1.0): screen px per pixel-art cell
const IMAGE_PX = 64;          // pointers.IMAGE_PX: an imported image's long edge at size 1.0
const CHECK_A = "#525259", CHECK_B = "#6b6b73";
const KEYS = { p: "pencil", f: "fill", t: "hotspot" };  // the studio's tool keys

const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);
const rgbToHex = (rgb) => "#" + rgb.map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255).toString(16).padStart(2, "0")).join("");
function rgbToHsv([r, g, b]) {
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let h = 0;
  if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [((h / 6) + 1) % 1, mx ? d / mx : 0, mx];
}
function hsvToRgb([h, s, v]) {
  const i = Math.floor(h * 6), f = h * 6 - i, p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][((i % 6) + 6) % 6];
}
function parseHex(text) {
  const m = String(text).trim().replace(/^#/, "");
  if (/^[0-9a-f]{3}$/i.test(m)) return "#" + m.split("").map((c) => c + c).join("").toLowerCase();
  return /^[0-9a-f]{6}$/i.test(m) ? "#" + m.toLowerCase() : null;
}
function checker(ctx, w, h, sq) {
  ctx.fillStyle = CHECK_A;
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = CHECK_B;
  for (let j = 0; j * sq < h; j++) for (let i = 0; i * sq < w; i++) if ((i + j) % 2) ctx.fillRect(i * sq, j * sq, sq, sq);
}
function sharpCanvas(cls, w, h) {
  const c = el("canvas", { class: cls });
  const dpr = window.devicePixelRatio || 1;
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  c.style.width = w + "px";
  c.style.height = h + "px";
  const ctx = c.getContext("2d");
  ctx.scale(dpr, dpr);
  return [c, ctx];
}

F.openStudio = (data, host, onDone) => {
  const [GW, GH] = data.grid, RES = data.res, PALETTE = data.palette, BRUSHES = data.brushes;
  const blank = () => Array.from({ length: GH }, () => Array(GW).fill(null));
  const S = {
    mode: data.mode, tool: data.mode === "image" ? "hotspot" : "pencil", mirror: false, brush: BRUSHES[1],
    color: "#000000", hsv: [0, 0, 0], sliders: "hsb", hotspot: data.hotspot.slice(), undo: [],
    cells: data.cells ? data.cells.map((r) => r.slice()) : blank(),
    smooth: null, image: null, crisp: !!data.crisp, flip: data.flip !== false, last: null, erasing: false,
  };
  const smoothCanvas = () => { const c = document.createElement("canvas"); c.width = GW * RES; c.height = GH * RES; return c; };
  const loadImage = (url) => new Promise((ok) => { const i = new Image(); i.onload = () => ok(i); i.src = url; });

  // ---- model (mirrors flippy/pixelart.py)
  const snapshot = () => {
    S.undo.push({ cells: S.cells.map((r) => r.slice()), smooth: S.smooth ? copyCanvas(S.smooth) : null, mode: S.mode, hotspot: S.hotspot.slice() });
    S.undo = S.undo.slice(-50);
  };
  const copyCanvas = (src) => { const c = smoothCanvas(); c.getContext("2d").drawImage(src, 0, 0); return c; };
  const undo = () => {
    const u = S.undo.pop();
    if (!u) return;
    Object.assign(S, { cells: u.cells, smooth: u.smooth, mode: u.mode, hotspot: u.hotspot });
    showStyle(); redraw();
  };
  const cellsToSmooth = () => {
    const c = smoothCanvas(), ctx = c.getContext("2d");
    S.cells.forEach((row, y) => row.forEach((col, x) => { if (col) { ctx.fillStyle = col; ctx.fillRect(x * RES, y * RES, RES, RES); } }));
    return c;
  };
  const smoothToCells = () => {
    const d = S.smooth.getContext("2d").getImageData(0, 0, GW * RES, GH * RES).data;
    return Array.from({ length: GH }, (_, cy) => Array.from({ length: GW }, (_, cx) => {
      const i = ((cy * RES + (RES >> 1)) * GW * RES + cx * RES + (RES >> 1)) * 4;
      return d[i + 3] > 127 ? rgbToHex([d[i] / 255, d[i + 1] / 255, d[i + 2] / 255]) : null;
    }));
  };
  const setMode = (mode) => {
    if (mode === S.mode || S.mode === "image") return;
    snapshot();
    if (mode === "pixel") { S.cells = smoothToCells(); S.smooth = null; } else S.smooth = cellsToSmooth();
    S.mode = mode;
    showStyle(); redraw();
  };
  const paint = (cx, cy, col) => {
    S.cells[cy][cx] = col;
    if (S.mirror) S.cells[cy][GW - 1 - cx] = col;
  };
  const fill = (cx, cy, col) => {
    const target = S.cells[cy][cx];
    if (target === col) return;
    const stack = [[cx, cy]];
    while (stack.length) {
      const [x, y] = stack.pop();
      if (x < 0 || y < 0 || x >= GW || y >= GH || S.cells[y][x] !== target) continue;
      S.cells[y][x] = col;
      stack.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
    }
  };
  const strokeTo = (x, y, col) => {
    const k = RES / CELL, p = [x * k, y * k], s = S.last || p;
    const ctx = S.smooth.getContext("2d");
    ctx.save();
    ctx.lineCap = ctx.lineJoin = "round";
    ctx.lineWidth = S.brush;
    ctx.globalCompositeOperation = col ? "source-over" : "destination-out";
    ctx.strokeStyle = col || "#000";
    for (const flip of S.mirror ? [false, true] : [false]) {
      const fx = (v) => (flip ? GW * RES - v : v);
      ctx.beginPath();
      ctx.moveTo(fx(s[0]), s[1]);
      ctx.lineTo(fx(p[0]) + (p[0] === s[0] && p[1] === s[1] ? 0.01 : 0), p[1]);
      ctx.stroke();
    }
    ctx.restore();
    S.last = p;
  };
  const fillSmooth = (x, y, col) => {  // tolerant of soft brush edges (a 48 per channel match)
    const k = RES / CELL, sx = Math.floor(x * k), sy = Math.floor(y * k), W = GW * RES, H = GH * RES;
    if (sx < 0 || sy < 0 || sx >= W || sy >= H) return;
    const ctx = S.smooth.getContext("2d"), img = ctx.getImageData(0, 0, W, H), d = img.data;
    const t = d.slice((sy * W + sx) * 4, (sy * W + sx) * 4 + 4);
    const nw = col ? [...hexToRgb(col).map((v) => Math.round(v * 255)), 255] : [0, 0, 0, 0];
    if (nw.every((v, i) => v === t[i])) return;
    const seen = new Uint8Array(W * H), stack = [sx, sy];
    while (stack.length) {
      const py = stack.pop(), px = stack.pop();
      if (px < 0 || py < 0 || px >= W || py >= H) continue;
      const n = py * W + px, i = n * 4;
      if (seen[n]) continue;
      if (Math.abs(d[i] - t[0]) > 48 || Math.abs(d[i + 1] - t[1]) > 48 || Math.abs(d[i + 2] - t[2]) > 48 || Math.abs(d[i + 3] - t[3]) > 48) continue;
      seen[n] = 1;
      d.set(nw, i);
      stack.push(px + 1, py, px - 1, py, px, py + 1, px, py - 1);
    }
    ctx.putImageData(img, 0, 0);
  };

  // ---- the board
  let boardW = GW * CELL, boardH = GH * CELL, imgK = 1;
  if (S.mode === "image") {
    imgK = Math.min((GW * CELL) / data.size[0], (GH * CELL) / data.size[1]);
    boardW = Math.round(data.size[0] * imgK);
    boardH = Math.round(data.size[1] * imgK);
  }
  const [board, bctx] = sharpCanvas("board", boardW, boardH);
  function drawBoard() {
    const ctx = bctx, w = boardW, h = boardH;
    ctx.clearRect(0, 0, w, h);
    checker(ctx, w, h, CELL / 2);
    ctx.imageSmoothingEnabled = !(S.mode === "image" && S.crisp);
    if (S.mode === "image" && S.image) ctx.drawImage(S.image, 0, 0, w, h);
    if (S.mode === "smooth") ctx.drawImage(S.smooth, 0, 0, w, h);
    if (S.mode === "pixel") S.cells.forEach((row, y) => row.forEach((c, x) => { if (c) { ctx.fillStyle = c; ctx.fillRect(x * CELL, y * CELL, CELL, CELL); } }));
    if (S.mode !== "image") {
      ctx.strokeStyle = `rgba(0,0,0,${S.mode === "pixel" ? 0.18 : 0.08})`;  // the grid stays: the tip snaps to it
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let x = 0; x <= GW; x++) { ctx.moveTo(x * CELL + 0.5, 0); ctx.lineTo(x * CELL + 0.5, h); }
      for (let y = 0; y <= GH; y++) { ctx.moveTo(0, y * CELL + 0.5); ctx.lineTo(w, y * CELL + 0.5); }
      ctx.stroke();
      if (S.mirror) {
        ctx.strokeStyle = "rgba(77,179,255,0.6)";
        ctx.beginPath(); ctx.moveTo((GW * CELL) / 2, 0); ctx.lineTo((GW * CELL) / 2, h); ctx.stroke();
      }
    }
    const [hx, hy] = S.hotspot;  // the tip: red circle
    const unit = S.mode === "image" ? imgK : CELL;
    ctx.strokeStyle = "#ff3333";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(hx * unit + unit / 2, hy * unit + unit / 2, Math.max(CELL * 0.45, unit * 0.45), 0, Math.PI * 2);
    ctx.stroke();
  }
  const cellAt = (x, y) => {
    const cx = Math.floor(x / CELL), cy = Math.floor(y / CELL);
    return cx >= 0 && cy >= 0 && cx < GW && cy < GH ? [cx, cy] : null;
  };
  const pos = (e) => { const r = board.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  board.addEventListener("contextmenu", (e) => e.preventDefault());
  board.addEventListener("pointerdown", (e) => {
    board.setPointerCapture(e.pointerId);
    const [x, y] = pos(e);
    S.erasing = e.button === 2 || e.ctrlKey;   // right-click (or ⌃-click) erases
    const col = S.erasing ? null : S.color;
    if (S.tool === "hotspot") {
      snapshot();
      if (S.mode === "image") S.hotspot = [Math.min(Math.floor(x / imgK), data.size[0] - 1), Math.min(Math.floor(y / imgK), data.size[1] - 1)];
      else { const c = cellAt(x, y); if (c) S.hotspot = c; }
    } else if (S.mode === "pixel") {
      const c = cellAt(x, y);
      if (!c) return;
      snapshot();
      S.tool === "fill" ? fill(c[0], c[1], col) : paint(c[0], c[1], col);
    } else if (S.mode === "smooth") {
      snapshot();
      if (S.tool === "fill") fillSmooth(x, y, col);
      else strokeTo(x, y, col);
    }
    redraw();
  });
  board.addEventListener("pointermove", (e) => {
    if (!board.hasPointerCapture(e.pointerId)) return;
    const [x, y] = pos(e);
    const col = S.erasing ? null : S.color;
    if (S.tool === "hotspot") return;
    if (S.mode === "pixel" && S.tool === "pencil") { const c = cellAt(x, y); if (c) paint(c[0], c[1], col); }
    else if (S.mode === "smooth" && S.tool === "pencil") strokeTo(x, y, col);
    else return;
    redraw();
  });
  board.addEventListener("pointerup", () => { S.last = null; });

  // ---- side column
  const sbtn = (title, fn, attrs = {}) => el("button", Object.assign({ class: "sbtn", type: "button", onclick: fn }, attrs), title);
  const toolButtons = [["Pencil", "pencil", "Click paints, right-click (or ⌃-click) erases"], ["Fill", "fill", "Fill an area"],
    ["Tip", "hotspot", "Click the pixel that should touch the target"]]
    .filter(([, k]) => S.mode !== "image" || k === "hotspot")
    .map(([t, k, tip]) => sbtn(t, () => setTool(k), { title: tip, "data-k": k, "aria-pressed": "false" }));
  const setTool = (k) => { S.tool = k; toolButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === k))); };
  const modeButtons = [["Pixels", "pixel"], ["Smooth", "smooth"]].map(([t, m]) => sbtn(t, () => setMode(m), { "data-m": m }));
  const brushButtons = BRUSHES.map((w, i) => sbtn(["Thin", "Medium", "Thick"][i], () => { S.brush = w; showStyle(); }, { "data-w": w, title: "Brush width for smooth drawing" }));
  function showStyle() {
    modeButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.m === S.mode)));
    brushButtons.forEach((b) => { b.disabled = S.mode !== "smooth"; b.setAttribute("aria-pressed", String(S.mode === "smooth" && +b.dataset.w === S.brush)); });
  }

  const swatches = PALETTE.map((c) => el("button", { class: "sw" + (c ? "" : " clear"), type: "button", style: c ? `background:${c}` : null,
    title: c ? c : "Eraser (transparent)", "aria-label": c ? `Color ${c}` : "Eraser", onclick: () => pick(c) }));
  const current = el("div", { class: "current" });
  const hex = el("input", { class: "hex", type: "text", placeholder: "#RRGGBB", "aria-label": "Hex color", spellcheck: "false",
    onchange: () => { const h = parseHex(hex.value); h ? pick(h) : (hex.value = S.color || ""); } });
  const chans = [0, 1, 2].map((i) => {
    const [c, ctx] = sharpCanvas("", 200, 22);
    c.style.width = "100%";
    const letter = el("span", {}, "H"), out = el("output", {}, "0");
    const slide = (e) => {
      const r = c.getBoundingClientRect(), f = Math.max(0, Math.min(1, (e.clientX - r.left - 7) / (r.width - 14)));
      if (S.sliders === "rgb") { const rgb = S.color ? hexToRgb(S.color) : [0, 0, 0]; rgb[i] = f; pick(rgbToHex(rgb)); }
      else { S.hsv[i] = f; pick(rgbToHex(hsvToRgb(S.hsv)), true); }
    };
    c.addEventListener("pointerdown", (e) => { c.setPointerCapture(e.pointerId); slide(e); });
    c.addEventListener("pointermove", (e) => { if (c.hasPointerCapture(e.pointerId)) slide(e); });
    return { c, ctx, letter, out, row: el("div", { class: "chan" }, letter, c, out) };
  });
  const rgbButton = sbtn("RGB", () => { S.sliders = S.sliders === "hsb" ? "rgb" : "hsb"; rgbButton.textContent = S.sliders === "hsb" ? "RGB" : "HSB"; showColor(); },
    { title: "Switch between hue/saturation/brightness and red/green/blue (0-255) sliders" });
  function drawChan({ c, ctx }, i) {
    const w = c.getBoundingClientRect().width || 200, h = 22;
    if (c.width !== Math.round(w * devicePixelRatio)) { c.width = Math.round(w * devicePixelRatio); ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0); }
    ctx.clearRect(0, 0, w, h);
    const g = ctx.createLinearGradient(7, 0, w - 7, 0);
    let at;
    if (S.sliders === "rgb") {
      const rgb = S.color ? hexToRgb(S.color) : [0, 0, 0];
      for (const f of [0, 1]) g.addColorStop(f, rgbToHex(rgb.map((v, k) => (k === i ? f : v))));
      at = rgb[i];
    } else {
      const [hu, sa, va] = S.hsv;
      const n = i === 0 ? 12 : 1;
      for (let k = 0; k <= n; k++) { const f = k / n; g.addColorStop(f, rgbToHex(hsvToRgb(i === 0 ? [f, 1, 1] : i === 1 ? [hu, f, va] : [hu, sa, f]))); }
      at = S.hsv[i];
    }
    const css = getComputedStyle(document.documentElement);
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.roundRect(7, h / 2 - 4, w - 14, 8, 4); ctx.fill();
    ctx.strokeStyle = "rgba(127,127,127,0.35)"; ctx.lineWidth = 1; ctx.stroke();
    const kx = 7 + (w - 14) * at;
    ctx.beginPath(); ctx.arc(kx, h / 2, 8, 0, Math.PI * 2);
    ctx.fillStyle = "#fff"; ctx.shadowColor = "rgba(0,0,0,0.35)"; ctx.shadowBlur = 3; ctx.fill(); ctx.shadowBlur = 0;
    ctx.strokeStyle = css.getPropertyValue("--field-line"); ctx.stroke();
  }
  function showColor() {
    current.className = "current" + (S.color ? "" : " clear");
    current.style.background = S.color || "";
    swatches.forEach((sw, i) => sw.setAttribute("aria-pressed", String(PALETTE[i] === S.color)));
    const rgb = S.color ? hexToRgb(S.color) : [0, 0, 0];
    chans.forEach((ch, i) => {
      ch.letter.textContent = (S.sliders === "rgb" ? "RGB" : "HSB")[i];
      ch.out.textContent = S.sliders === "rgb" ? Math.round(rgb[i] * 255) : Math.round(S.hsv[i] * (i === 0 ? 360 : 100));
      drawChan(ch, i);
    });
  }
  function pick(col, keepHsv) {
    S.color = col;
    if (col && !keepHsv) S.hsv = rgbToHsv(hexToRgb(col));
    if (document.activeElement !== hex) hex.value = col || "";
    showColor();
  }
  const offColor = F.on("color", (h) => { if (h && studio.isConnected) pick(h); });

  const [preview, pctx] = sharpCanvas("preview", 292, 110);
  function drawPreview() {
    const w = preview.getBoundingClientRect().width || 292, h = 110;
    if (preview.width !== Math.round(w * devicePixelRatio)) { preview.width = Math.round(w * devicePixelRatio); preview.height = Math.round(h * devicePixelRatio); pctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0); }
    let src, k, hx, hy;
    if (S.mode === "pixel") {
      src = document.createElement("canvas"); src.width = GW; src.height = GH;
      const c = src.getContext("2d");
      S.cells.forEach((row, y) => row.forEach((col, x) => { if (col) { c.fillStyle = col; c.fillRect(x, y, 1, 1); } }));
      k = SPRITE_PX; [hx, hy] = S.hotspot;
    } else if (S.mode === "smooth") {
      src = S.smooth; k = SPRITE_PX / RES; hx = S.hotspot[0] * RES + RES / 2; hy = S.hotspot[1] * RES + RES / 2;
    } else {
      src = S.image; [hx, hy] = S.hotspot; k = S.crisp ? SPRITE_PX : IMAGE_PX / Math.max(data.size[0], data.size[1]);
    }
    ["#1f1f24", "#ebebf0"].forEach((bg, i) => {
      pctx.fillStyle = bg;
      pctx.fillRect((i * w) / 2, 0, w / 2, h);
      if (!src) return;
      pctx.save();
      pctx.imageSmoothingEnabled = !(S.mode === "pixel" || (S.mode === "image" && S.crisp));
      pctx.translate((i * w) / 2 + w / 4 - hx * k, 10 - hy * k);
      pctx.scale(k, k);
      pctx.drawImage(src, 0, 0);
      pctx.restore();
    });
  }
  function redraw() { drawBoard(); drawPreview(); }

  const name = el("input", { class: "name", type: "text", value: data.name, "aria-label": "Pointer name", maxlength: "40", spellcheck: "false" });
  const err = el("div", { class: "err", role: "alert" });
  const check = (label, on, fn) => el("label", { class: "check" }, el("input", { type: "checkbox", checked: on, onchange: (e) => fn(e.target.checked) }), label);
  const save = sbtn("Save", async () => {
    err.textContent = "";
    const payload = { mode: S.mode, name: name.value, hotspot: S.hotspot, flip: S.flip, editing: data.editing };
    if (S.mode === "pixel") payload.cells = S.cells;
    else if (S.mode === "smooth") payload.image = S.smooth.toDataURL("image/png");
    else { payload.image = data.image; payload.crisp = S.crisp; }
    try { const value = await F.call("studio_save", payload); close(); onDone(value, payload.name); }
    catch (e) { err.textContent = e.message; }
  }, { class: "sbtn primary" });

  const mirrorBox = check("Mirror \u2194", false, (on) => { S.mirror = on; redraw(); });
  const side = el("div", { class: "side" });
  if (S.mode === "image") {
    side.append(
      el("div", {}, el("h4", {}, "Tip"), el("div", { class: "line" }, el("span", { style: "color:var(--s-muted)" }, "Click the pixel that should touch the target."))),
      check("Pixel art (keep it crisp)", S.crisp, (on) => { S.crisp = on; redraw(); }));
  } else {
    side.append(
      el("div", {}, el("h4", {}, "Tools"), el("div", { class: "line" }, el("div", { class: "joined" }, toolButtons), sbtn("Undo", undo, { title: "⌘Z" }))),
      mirrorBox,
      el("div", {}, el("h4", {}, "Style"), el("div", { class: "line" }, el("div", { class: "joined" }, modeButtons), el("div", { class: "joined" }, brushButtons))),
      el("div", {}, el("h4", {}, "Color"), el("div", { class: "swatches" }, swatches)),
      el("div", { class: "line" }, current, hex, sbtn("Eyedropper", () => F.call("eyedropper"), { title: "Pick a color from anywhere on the screen" })),
      el("div", {}, chans.map((c) => c.row), el("div", { class: "line", style: "justify-content:flex-end;margin-top:2px" }, rgbButton)),
      el("div", {}, el("h4", {}, "Start from"), el("div", { class: "line", style: "justify-content:flex-start;gap:6px" },
        sbtn("Blank", () => { snapshot(); S.cells = blank(); if (S.mode === "smooth") S.smooth = smoothCanvas(); redraw(); }),
        ...["hand", "arrow"].map((k) => sbtn(k[0].toUpperCase() + k.slice(1), async () => {
          const t = await F.call("studio_template", { kind: k });
          snapshot(); S.cells = t.cells; S.hotspot = t.hotspot; if (S.mode === "smooth") S.smooth = cellsToSmooth(); redraw();
        })))));
  }
  side.append(
    el("div", {}, el("h4", {}, "Preview"), preview),
    el("div", { class: "line" }, name),
    el("div", { class: "line" }, check("Flip near bottom", S.flip, (on) => (S.flip = on)), save),
    err);

  const title = data.editing ? `Edit “${data.editing}”` : S.mode === "image" ? "Pick the tip" : "Draw a pointer";
  const studio = el("section", { class: "studio", "aria-label": "Pointer studio" },
    el("div", { class: "bar" }, el("strong", {}, title),
      S.mode === "image" ? null : el("span", { class: "keys-hint" }, `P pencil, F fill, T tip, M mirror, ${F.state.platform === "darwin" ? "\u2318Z" : "Ctrl+Z"} undo`),
      sbtn("Close", () => close(), { class: "sbtn quiet" })),
    el("div", { class: "body" }, el("div", { class: "board-wrap" }, board), side));
  studio.tabIndex = -1;
  studio.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" && e.target.type !== "checkbox") return;
    if ((e.metaKey || e.ctrlKey) && e.key === "z") { e.preventDefault(); undo(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === "escape") close();
    else if (S.mode !== "image" && KEYS[k]) setTool(KEYS[k]);
    else if (S.mode !== "image" && k === "m") { const box = mirrorBox.querySelector("input"); box.checked = !box.checked; S.mirror = box.checked; redraw(); }
    else return;
    e.preventDefault();
  });
  function close() { studio.remove(); offColor(); F.studioDemo = null; host.dispatchEvent(new Event("studio-closed")); }

  host.replaceChildren(studio);
  // for the scripted demo (flippy-ask demo-pointer): the same steps a person would take
  F.studioDemo = {
    tool: setTool, color: (h) => pick(h),
    paint: (x, y) => { snapshot(); paint(x, y, S.color); redraw(); },
    fill: (x, y) => { snapshot(); fill(x, y, S.color); redraw(); },
    tip: (x, y) => { snapshot(); S.hotspot = [x, y]; redraw(); },
    name: (n) => (name.value = n), save: () => save.click(),
  };
  (async () => {
    if (S.mode === "smooth") { S.smooth = smoothCanvas(); S.smooth.getContext("2d").drawImage(await loadImage(data.image), 0, 0); }
    if (S.mode === "image") S.image = await loadImage(data.image);
    setTool(S.tool); showStyle(); pick("#000000"); redraw();
    studio.scrollIntoView({ block: "nearest", behavior: "smooth" });
    studio.focus({ preventScroll: true });
  })();
  return close;
};
})();
