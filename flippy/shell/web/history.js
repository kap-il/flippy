// History: what you asked and what Flippy answered or did, newest first, grouped by day (flippy/history_store.py).
(() => {
"use strict";
const F = window.F, el = F.el;
let entries = [], keep = true, days = 30, more = false, query = "", open = new Set(), list = null, typing = null;

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
    oninput: (e) => { query = e.target.value.trim(); clearTimeout(typing); typing = setTimeout(load, 180); } });
  const clear = el("button", { class: "btn quiet", onclick: () => F.confirm("Clear all history?", "Every saved question and answer is deleted from this computer.",
    "Clear all", async () => { await F.call("history_clear"); open.clear(); load(); F.toast("History cleared"); }) }, F.svg("trash"), "Clear all");
  list = el("div", { class: "history" });
  const wrap = el("div", { class: "content" },
    el("div", { class: "page-head" }, el("h1", {}, "History"), el("div", { class: "head-tools" }, search, clear)),
    list,
    el("p", { class: "fineprint" }, "Saved on this computer as text, never screenshots. ",
      el("a", { href: "#settings/general", onclick: (ev) => { ev.preventDefault(); F.go("settings/general", "history.keep"); } }, "History settings")));
  load();
  return wrap;
};
const reload = () => { if (location.hash.slice(1) === "history") load(); };
F.on("history", reload);
F.on("field:history.keep", reload);
})();
