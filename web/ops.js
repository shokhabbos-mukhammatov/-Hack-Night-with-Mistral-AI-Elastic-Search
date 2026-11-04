// HighGround Ops: plain DOM, text nodes only.
const $ = (s) => document.querySelector(s);
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const get = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error(`${p}: ${r.status}`); return r.json(); };
const ago = (iso) => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`; };
const hours = () => $("#hours").value;

function table(cols, rows) {
  return h("table", {}, h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col", class: c.num ? "num" : null }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, cols.map((c) => h("td", { class: c.num ? "num" : null }, c.fmt ? c.fmt(r[c.key], r) : r[c.key] ?? "–"))))));
}

async function overview() {
  const o = await get("/api/ops/overview");
  const stale = Object.entries(o.feeds).filter(([k, v]) => v.stale && k !== "datasf").map(([k]) => k);
  $("#overview").replaceChildren(
    h("p", {}, `Blocks: ${["low", "elevated", "high", "severe"].map((l) => `${o.risk_levels[l] || 0} ${l}`).join(" · ")}`),
    h("p", {}, `Alerts outbox: ${Object.entries(o.outbox).map(([k, v]) => `${v} ${k}`).join(" · ") || "empty"}`),
    h("p", {}, stale.length ? `Stale feeds: ${stale.join(", ")}` : "All live feeds fresh."));
  const sel = $("#nh");
  if (!sel.options.length) { o.neighborhoods.forEach((n) => sel.append(h("option", { value: n }, n))); sel.value = o.neighborhoods.includes("Mission") ? "Mission" : o.neighborhoods[0]; }
}
async function unusual() {
  const u = await get(`/api/ops/unusual?hours=${hours()}`);
  const list = (items) => items.length ? h("ul", {}, items.map((i) => h("li", {}, `${i.key}: ${i.count} now vs ${i.background} in 30 days`, h("span", { class: "pill" }, ` score ${i.score}`)))) : h("p", { class: "muted" }, "Nothing unusual.");
  $("#unusual").replaceChildren(h("h3", {}, "Neighborhoods"), list(u.neighborhoods), h("h3", {}, "Report types"), list(u.categories));
}
async function neighborhood() {
  const n = await get(`/api/ops/neighborhood/${encodeURIComponent($("#nh").value)}?hours=${hours()}`);
  $("#nhout").replaceChildren(h("p", {}, n.summary), h("p", { class: "src" }, `${n.total} reports · ${n.summary_by}`));
}
async function hotspots() {
  const d = await get(`/api/ops/hotspots?hours=${hours()}`);
  $("#hot").replaceChildren(d.items.length ? table([
    { key: "neighborhood", label: "Neighborhood" }, { key: "reports", label: "Reports", num: true },
    { key: "in_100yr", label: "100-yr zone", fmt: (v) => (v ? "yes" : "no") }, { key: "storm_reports_5y", label: "Storm-day history", num: true },
    { key: "priority", label: "Priority", num: true }, { key: "latest", label: "Latest", fmt: (v) => ago(v) },
    { key: "h3_9", label: "Block", fmt: (v) => h("a", { href: `/?cell=${v}` }, v.slice(0, 10) + "…") },
  ], d.items) : h("p", { class: "muted" }, "No flood reports in this window."));
}
async function refresh() {
  for (const f of [overview, unusual, hotspots, neighborhood]) {
    try { await f(); } catch (e) { console.error(e); }
  }
}
$("#refresh").addEventListener("click", refresh);
$("#hours").addEventListener("change", refresh);
$("#nh").addEventListener("change", neighborhood);
refresh();
setInterval(refresh, 120000);
