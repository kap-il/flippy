// History: what you asked and what Flippy answered or did, newest first, grouped by day (flippy/history_store.py).
(() => {
"use strict";
const F = window.F, el = F.el;
let entries = [], keep = true, days = 30, more = false, query = "", open = new Set(), list = null, typing = null;
let tasks = [], remember = true, openTasks = new Set(), taskBox = null;  // the context folder (#22)

const DAY = 86400000;
function dayLabel(ts) {
  const d = new Date(ts * 1000), today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / DAY);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}
const time = (ts) => new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const PROVIDERS = { claude: "Claude", codex: "ChatGPT", auto: "" };

async function load(append) {
  const r = await F.call("history_list", { offset: append ? entries.length : 0, limit: 50, query: query || null });
  entries = append ? entries.concat(r.entries) : r.entries;
  keep = r.keep; days = r.days; more = r.entries.length === 50;
  draw();
}

// Ongoing tasks Flippy remembers across sessions (flippy/context_store.py): read, rename, delete.
async function loadTasks() {
  const r = await F.call("context_list");
  tasks = r.tasks; remember = r.keep;
  drawTasks();
}

function renameTask(t) {
  F.dialog((sheet, close) => {
    const input = el("input", { type: "text", class: "find", value: t.title, maxlength: "80", style: "width:100%", "aria-label": "Task name",
      onkeydown: (e) => { if (e.key === "Enter") save(); } });
    const save = async () => {
      try { await F.call("context_rename", { id: t.id, title: input.value }); close(); loadTasks(); }
      catch (err) { F.toast(err.message); }
    };
    sheet.append(el("h3", {}, "Rename task"), input,
      el("div", { class: "actions" }, el("button", { class: "btn quiet", onclick: close }, "Cancel"),
        el("button", { class: "btn primary", onclick: save }, "Rename")));
  });
}

function taskRow(t) {
  const isOpen = openTasks.has(t.id);
  const day = new Date(t.updated.slice(0, 10) + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const meta = [t.apps.join(", "), `last ${day}`].filter(Boolean).join("  ·  ");
  const row = el("div", { class: "entry" + (isOpen ? " open" : "") });
  row.append(el("button", { class: "entry-head", "aria-expanded": String(isOpen),
    onclick: () => { isOpen ? openTasks.delete(t.id) : openTasks.add(t.id); row.replaceWith(taskRow(t)); } },
    el("span", { class: "kind", html: icon("history") }),
    el("span", { class: "q" }, el("span", { class: "label" }, t.title),
      el("span", { class: "meta" }, t.compacted ? meta : (t.where || meta))),
    t.compacted ? el("span", { class: "tag", title: "Not picked up for a while, so shrunk to a few lines" }, "Shrunk") : null,
    F.svg("right")));
  if (isOpen) {
    const body = el("div", { class: "entry-body" });
    if (!t.compacted && t.where) body.append(el("p", { class: "answer" }, el("b", {}, "Where you are: "), t.where));
    const lines = t.compacted ? t.summary : t.log.slice().reverse().map((l) => `${l.day}: ${l.text}`);
    if (lines.length) body.append(el("ol", { class: "steps" }, lines.map((s) => el("li", {}, s))));
    if (t.apps.length) body.append(el("p", { class: "answer muted" }, t.apps.join(", ")));
    body.append(el("div", { class: "entry-actions" },
      el("button", { class: "btn quiet", onclick: () => renameTask(t) }, F.svg("rename"), "Rename"),
      el("button", { class: "btn quiet danger", onclick: async () => { await F.call("context_delete", { id: t.id }); loadTasks(); } },
        F.svg("trash"), "Forget")));
    row.append(body);
  }
  return row;
}

function drawTasks() {
  if (!taskBox || !taskBox.isConnected) return;
  taskBox.replaceChildren();
  if (query || !remember || !tasks.length) return;  // (searching is for questions; the switch and empty say nothing)
  const forget = el("button", { class: "btn quiet", style: "font-weight:400", onclick: () => F.confirm("Forget every task?",
    "Flippy deletes its notes on your ongoing tasks. Your question history stays.", "Forget all",
    async () => { await F.call("context_clear"); openTasks.clear(); loadTasks(); F.toast("Tasks forgotten"); }) }, "Forget all");
  taskBox.append(el("h2", {}, "Ongoing tasks", el("span", { class: "spacer" }), forget),
    el("div", { class: "card entries" }, tasks.map(taskRow)));
}

function entryRow(e) {
  const isOpen = open.has(e.id);
  const meta = [time(e.ts), e.app, PROVIDERS[e.provider] || e.provider, e.kind === "task" && e.detail].filter(Boolean).join("  ·  ");
  const badge = e.outcome !== "completed" ? el("span", { class: "tag " + e.outcome }, e.outcome === "error" ? "Didn’t finish" : "Stopped") : null;
  const row = el("div", { class: "entry" + (isOpen ? " open" : "") });
  const head = el("button", { class: "entry-head", "aria-expanded": String(isOpen),
    onclick: () => { isOpen ? open.delete(e.id) : open.add(e.id); row.replaceWith(entryRow(e)); } },
    el("span", { class: "kind", html: icon(e.kind === "task" ? "cursor" : "sparkle"), title: e.kind === "task" ? "Desktop task" : "Question" }),
    el("span", { class: "q" }, el("span", { class: "label" }, e.question), el("span", { class: "meta" }, meta)), badge, F.svg("right"));
  row.append(head);
  if (isOpen) {
    const body = el("div", { class: "entry-body" });
    if (e.steps.length) body.append(el("ol", { class: "steps" }, e.steps.map((s) => el("li", {}, s))));
    else if (e.answer) body.append(el("p", { class: "answer" }, e.answer));
    if (e.outcome === "error" && e.detail) body.append(el("p", { class: "answer muted" }, e.detail));
    if (!e.answer && !e.steps.length && e.outcome === "completed") body.append(el("p", { class: "answer muted" }, "Done, with nothing to say."));
    body.append(el("div", { class: "entry-actions" },
      el("button", { class: "btn quiet", onclick: () => {
        const text = `${e.question}\n\n${e.steps.length ? e.steps.join("\n") : e.answer}`.trim();
        F.call("copy", { text }).then(() => F.toast("Copied"), (err) => F.toast(err.message));
      } }, F.svg("copy"), "Copy"),
      el("button", { class: "btn quiet danger", onclick: async () => { await F.call("history_delete", { id: e.id }); entries = entries.filter((x) => x.id !== e.id); draw(); } },
        F.svg("trash"), "Delete")));
    row.append(body);
  }
  return row;
}

function draw() {
  if (!list || !list.isConnected) return;
  list.replaceChildren();
  if (!keep) {
    list.append(F.empty("history", "History is off", "Flippy isn’t saving what you ask.",
      el("button", { class: "btn", style: "margin-top:16px", onclick: async () => { await F.call("set", { id: "history.keep", value: true }); load(); } }, "Turn on history")));
    return;
  }
  if (!entries.length) {
    list.append(F.empty("history", query ? "Nothing matches" : "Nothing yet",
      query ? `No question or answer has “${query}”.` : `Ask Flippy something and it shows up here. Kept for ${days} days, as text only.`));
    return;
  }
  let current = null, card = null;
  for (const e of entries) {
    const label = dayLabel(e.ts);
    if (label !== current) {
      current = label;
      card = el("div", { class: "card entries" });
      list.append(el("h2", {}, label), card);
    }
    card.append(entryRow(e));
  }
  if (more) list.append(el("div", { class: "foot" }, el("span"), el("button", { class: "btn", onclick: () => load(true) }, "Show older")));
}

F.renderHistory = () => {
  const search = el("input", { type: "text", class: "find", placeholder: "Search history", value: query, spellcheck: "false", "aria-label": "Search history",
    oninput: (e) => { query = e.target.value.trim(); clearTimeout(typing); typing = setTimeout(() => { load(); drawTasks(); }, 180); } });
  const clear = el("button", { class: "btn quiet", onclick: () => F.confirm("Clear all history?", "Every saved question and answer is deleted from this computer.",
    "Clear all", async () => { await F.call("history_clear"); open.clear(); load(); F.toast("History cleared"); }) }, F.svg("trash"), "Clear all");
  list = el("div", { class: "history" });
  taskBox = el("div", { class: "history tasks" });
  const wrap = el("div", { class: "content" },
    el("div", { class: "page-head" }, el("h1", {}, "History"), el("div", { class: "head-tools" }, search, clear)),
    taskBox, list,
    el("p", { class: "fineprint" }, "Saved on this computer as text, never screenshots. ",
      el("a", { href: "#settings/general", onclick: (ev) => { ev.preventDefault(); F.go("settings/general", "history.keep"); } }, "History settings")));
  load();
  loadTasks();
  return wrap;
};
const reload = () => { if (location.hash.slice(1) === "history") load(); };
const reloadTasks = () => { if (location.hash.slice(1) === "history") loadTasks(); };
F.on("history", reload);
F.on("field:history.keep", reload);
F.on("context", reloadTasks);
F.on("field:context.keep", reloadTasks);
})();
