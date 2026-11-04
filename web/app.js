// HighGround SF front end. No framework; DOM is built with text nodes only (no innerHTML from data).
const $ = (s) => document.querySelector(s);
const LV = ["low", "elevated", "high", "severe"];
const ICON = { low: "✓", elevated: "!", high: "‼", severe: "⛔" };
const DEFAULT_PLACE = { lat: 37.76369683, lon: -122.41515522, label: "17TH ST & FOLSOM ST" };

const I18N = {
  en: {
    tab_brief: "My block", tab_ask: "Ask", tab_report: "Report", tab_alerts: "Alerts", tab_replay: "Storm replay",
    where: "Where are you?", check: "Check", where_help: "An address or an intersection in San Francisco.", use_location: "Use my location",
    lv_low: "Low risk", lv_elevated: "Elevated risk", lv_high: "High risk", lv_severe: "Severe risk",
    why: "Why", todo: "What to do", nearby: "Near you (last 6 hours)", nothing_nearby: "No hazard reports within 300 m in the last 6 hours.",
    history: "Worst flooding days nearby (311 reports)", sources: "Data freshness", read: "Read aloud", ask_btn: "Ask", speak: "Speak",
    draft: "Draft 311 report", play: "Play", pause: "Pause", subscribe: "Turn on alerts", phone_title: "Alerts for", how_well: "How well would it have worked?",
    loading: "Checking…", in_zone: "Inside SFPUC's 100-year storm flood risk zone", not_in_zone: "Outside SFPUC's 100-year storm flood risk zone",
  },
  es: {
    tab_brief: "Mi cuadra", tab_ask: "Preguntar", tab_report: "Reportar", tab_alerts: "Alertas", tab_replay: "Repetir tormenta",
    where: "¿Dónde está?", check: "Revisar", where_help: "Una dirección o un cruce en San Francisco.", use_location: "Usar mi ubicación",
    lv_low: "Riesgo bajo", lv_elevated: "Riesgo elevado", lv_high: "Riesgo alto", lv_severe: "Riesgo grave",
    why: "Por qué", todo: "Qué hacer", nearby: "Cerca de usted (últimas 6 horas)", nothing_nearby: "No hay reportes a menos de 300 m en las últimas 6 horas.",
    history: "Peores días de inundación cerca (reportes al 311)", sources: "Actualización de datos", read: "Leer en voz alta", ask_btn: "Preguntar", speak: "Hablar",
    draft: "Preparar reporte al 311", play: "Reproducir", pause: "Pausa", subscribe: "Activar alertas", phone_title: "Alertas para", how_well: "¿Qué tan bien habría funcionado?",
    loading: "Revisando…", in_zone: "Dentro de la zona de riesgo de inundación de 100 años de la SFPUC", not_in_zone: "Fuera de la zona de riesgo de inundación de 100 años de la SFPUC",
  },
  zh: {
    tab_brief: "我的街區", tab_ask: "提問", tab_report: "報告", tab_alerts: "警報", tab_replay: "暴雨重播",
    where: "你在哪裡？", check: "查詢", where_help: "三藩市的地址或路口。", use_location: "使用我的位置",
    lv_low: "低風險", lv_elevated: "風險升高", lv_high: "高風險", lv_severe: "極高風險",
    why: "原因", todo: "應該怎樣做", nearby: "附近（過去 6 小時）", nothing_nearby: "過去 6 小時 300 米內沒有報告。",
    history: "附近水浸最嚴重的日子（311 報告）", sources: "數據更新", read: "朗讀", ask_btn: "提問", speak: "說話",
    draft: "草擬 311 報告", play: "播放", pause: "暫停", subscribe: "開啟警報", phone_title: "警報：", how_well: "效果如何？",
    loading: "查詢中…", in_zone: "位於 SFPUC 百年一遇暴雨水浸風險區內", not_in_zone: "不在 SFPUC 百年一遇暴雨水浸風險區內",
  },
};
const state = { lang: "en", point: null, map: null, marker: null, replay: null, timer: null, step: 0 };
const t = (k) => (I18N[state.lang] && I18N[state.lang][k]) || I18N.en[k] || k;

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}
const link = (url, text) => (url ? h("a", { href: url, target: "_blank", rel: "noopener" }, text || url) : text);
const ago = (iso) => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 60 ? `${m} min` : `${Math.round(m / 60)} h`; };

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (r.status === 204) return null;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `Error ${r.status}`);
  return body;
}

// ---------------------------------------------------------------- i18n + tabs
function applyI18n() {
  document.documentElement.lang = state.lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => { const v = t(el.dataset.i18n); if (v !== el.dataset.i18n) el.textContent = v; });
}
function setupTabs() {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    tabs.forEach((b) => { const on = b === tab; b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1; $("#" + b.getAttribute("aria-controls")).hidden = !on; });
    tab.focus();
    if (tab.id === "tab-replay") enterReplay(); else exitReplay();
  };
  tabs.forEach((b, i) => {
    b.addEventListener("click", () => select(b));
    b.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); select(tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]); }
    });
  });
}

// ---------------------------------------------------------------- map
function initMap() {
  if (!window.maplibregl) return;
  const map = new maplibregl.Map({ container: "map", style: "https://tiles.openfreemap.org/styles/positron", center: [-122.437, 37.76], zoom: 12, attributionControl: { compact: true } });
  state.map = map;
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.on("load", () => {
    const origin = location.origin;
    map.addSource("zones", { type: "vector", tiles: [`${origin}/tiles/zones/{z}/{x}/{y}.pbf`], minzoom: 10, maxzoom: 16 });
    map.addLayer({ id: "zones", type: "fill", source: "zones", "source-layer": "hits", paint: { "fill-color": "#2a7fff", "fill-opacity": 0.22 } });
    // H3 density of recent reports, aggregated inside Elasticsearch (geohex grid in vector tiles)
    map.addSource("density", { type: "vector", tiles: [`${origin}/tiles/density/{z}/{x}/{y}.pbf?hours=24`], minzoom: 9, maxzoom: 15 });
    map.addLayer({ id: "density", type: "fill", source: "density", "source-layer": "aggs",
      paint: { "fill-color": "#0a84ff", "fill-opacity": ["interpolate", ["linear"], ["get", "_count"], 1, 0.12, 5, 0.3, 20, 0.5] } });
    map.addSource("risk", { type: "vector", tiles: [`${origin}/tiles/risk/{z}/{x}/{y}.pbf`], minzoom: 10, maxzoom: 16 });
    map.addLayer({ id: "risk", type: "fill", source: "risk", "source-layer": "hits",
      paint: { "fill-color": ["match", ["get", "level_rank"], 1, "#b26a00", 2, "#c0392b", 3, "#6b2c91", "#2f7d4f"], "fill-opacity": 0.45 } });
    map.addSource("reports", { type: "vector", tiles: [`${origin}/tiles/reports/{z}/{x}/{y}.pbf?hours=24`], minzoom: 10, maxzoom: 16 });
    map.addLayer({ id: "reports", type: "circle", source: "reports", "source-layer": "hits",
      paint: { "circle-radius": 5, "circle-color": ["match", ["get", "category"], "tree", "#2f7d4f", "street", "#666", "#0a84ff"], "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } });
    map.addSource("rcells", { type: "geojson", data: { type: "FeatureCollection", features: [] }, promoteId: "cell" });
    map.addLayer({ id: "rcells", type: "fill", source: "rcells", layout: { visibility: "none" },
      paint: { "fill-color": ["match", ["feature-state", "lv"], 1, "#b26a00", 2, "#c0392b", 3, "#6b2c91", "rgba(0,0,0,0)"], "fill-opacity": 0.5 } });
    map.addSource("rreports", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "rreports", type: "circle", source: "rreports", layout: { visibility: "none" },
      paint: { "circle-radius": 4, "circle-color": "#0a84ff", "circle-stroke-color": "#fff", "circle-stroke-width": 1 } });
    map.on("click", "reports", (e) => {
      const p = e.features[0].properties;
      new maplibregl.Popup().setLngLat(e.lngLat).setDOMContent(h("div", {}, h("b", {}, p.category), h("br"), p.address || "", h("br"), p.requested_at ? ago(p.requested_at) + " ago" : "")).addTo(map);
    });
  });
}
function setMarker(lat, lon) {
  if (!state.map) return;
  if (!state.marker) {
    state.marker = new maplibregl.Marker({ color: "#14212b" });
    const el = state.marker.getElement();
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", "Selected place");
  }
  state.marker.setLngLat([lon, lat]).addTo(state.map);
  state.map.flyTo({ center: [lon, lat], zoom: 15, essential: true });
}

// ---------------------------------------------------------------- briefing
async function brief(params) {
  const box = $("#brief");
  box.setAttribute("aria-busy", "true");
  box.replaceChildren(h("p", { class: "muted" }, t("loading")));
  try {
    const q = new URLSearchParams({ ...params, lang: state.lang });
    const b = await api(`/api/briefing?${q}`);
    state.point = { lat: b.location.lat, lon: b.location.lon, label: b.location.label || params.address || "" };
    $("#rloc").textContent = `Location: ${state.point.label || `${state.point.lat.toFixed(5)}, ${state.point.lon.toFixed(5)}`}`;
    $("#saddr").value ||= state.point.label || "";
    setMarker(b.location.lat, b.location.lon);
    renderBrief(b);
  } catch (e) {
    box.replaceChildren(h("p", { class: "err", role: "alert" }, e.message));
  } finally {
    box.setAttribute("aria-busy", "false");
  }
}
function renderBrief(b) {
  const lv = b.risk.level;
  const zoneLine = b.location.exact_point && b.location.exact_point.in_100yr ? t("in_zone") : b.zone.in_100yr ? t("in_zone") + " (part of this block)" : t("not_in_zone");
  const kids = [
    h("div", { class: "row" }, h("span", { class: `level ${lv}` }, h("span", { "aria-hidden": "true" }, ICON[lv]), t("lv_" + lv)),
      h("span", { class: "tag" }, b.location.label || b.location.neighborhood || ""),
      b.narrative.generated_by === "mistral" ? h("span", { class: "tag", title: "Written by Mistral from the facts below; numbers checked" }, "AI-written · checked") : null),
    h("p", { class: "narr", lang: b.narrative.lang }, b.narrative.text),
    h("button", { class: "secondary", type: "button", onclick: () => speak(b.narrative.text, b.narrative.lang) }, "🔊 ", t("read")),
    h("div", { class: "sec" }, h("h2", {}, t("why")), h("ul", {}, h("li", {}, zoneLine),
      b.risk.reasons.map((r) => h("li", {}, r.text, " ", h("span", { class: "src" }, "— ", link(r.source_url, r.source)))))),
    h("div", { class: "sec" }, h("h2", {}, t("todo")), h("ol", {}, b.actions.map((a) => h("li", {}, a.url ? link(a.url, a.text) : a.text)))),
    h("div", { class: "sec" }, h("h2", {}, t("nearby")), b.nearby.length
      ? h("ul", {}, b.nearby.map((e) => h("li", {}, `${e.kind.replace("_", " ")} · ${e.distance_m} m · ${ago(e.at)} ago`, e.label ? ` · ${e.label}` : "", " ", e.kind === "street_signal" ? h("span", { class: "tag" }, "unverified") : null)))
      : h("p", { class: "muted" }, t("nothing_nearby"))),
  ];
  const days = (b.history.top_dates || []);
  if (days.length) kids.push(h("div", { class: "sec" }, h("h2", {}, t("history")), h("p", {}, days.join(" · "))));
  const al = b.conditions.alerts || [];
  if (al.length) kids.push(h("div", { class: "sec" }, h("h2", {}, "NWS"), h("ul", {}, al.map((a) => h("li", {}, link(a.url, a.event), a.ends ? ` → ${new Date(a.ends).toLocaleString()}` : "")))));
  const fr = Object.entries(b.freshness || {});
  if (fr.length) kids.push(h("details", { class: "sec" }, h("summary", {}, t("sources")), h("ul", {}, fr.map(([k, v]) => h("li", {}, `${k}: ${v.age_s == null ? "never" : ago(new Date(Date.now() - v.age_s * 1000).toISOString())} ago`, v.stale ? h("span", { class: "tag err" }, " stale") : null)))));
  kids.push(h("p", { class: "hint" }, b.disclaimer));
  $("#brief").replaceChildren(...kids);
}
async function speak(text, lang) {
  try {
    const r = await fetch("/api/speak", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ text }) });
    if (r.status === 200) { new Audio(URL.createObjectURL(await r.blob())).play(); return; }
  } catch (_) {}
  if ("speechSynthesis" in window) { const u = new SpeechSynthesisUtterance(text); u.lang = lang || state.lang; speechSynthesis.cancel(); speechSynthesis.speak(u); }
}

// ---------------------------------------------------------------- ask + voice
async function ask(question) {
  const box = $("#answer");
  box.replaceChildren(h("p", { class: "muted" }, t("loading")));
  try {
    const body = { question, lang: state.lang === "en" ? "auto" : state.lang, ...(state.point ? { lat: state.point.lat, lon: state.point.lon } : {}),
      ...(state.conversation ? { conversation_id: state.conversation } : {}) };
    const a = await api("/api/ask", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (a.conversation_id) state.conversation = a.conversation_id;  // Mistral Conversations: follow-ups keep context
    box.replaceChildren(h("p", { class: "narr", lang: a.lang }, a.answer),
      a.citations && a.citations.length ? h("ul", {}, a.citations.map((c) => h("li", {}, link(c.url, c.title || c.url), c.source === "web_search" ? h("span", { class: "tag" }, " web") : null))) : null,
      a.web_sourced ? h("p", { class: "hint" }, "Parts of this answer come from a web search; check the linked sources.") : null,
      h("button", { class: "secondary", type: "button", onclick: () => speak(a.answer, a.lang) }, "🔊 ", t("read")));
  } catch (e) { box.replaceChildren(h("p", { class: "err", role: "alert" }, e.message)); }
}
let recorder = null;
async function toggleMic() {
  const btn = $("#mic");
  if (recorder) { recorder.stop(); return; }
  if (!navigator.mediaDevices) return;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((tr) => tr.stop());
    btn.setAttribute("aria-pressed", "false"); recorder = null;
    const fd = new FormData(); fd.append("audio", new Blob(chunks, { type: "audio/webm" }), "question.webm");
    try { const r = await api("/api/voice", { method: "POST", body: fd }); $("#question").value = r.text; ask(r.text); }
    catch (e) { $("#answer").replaceChildren(h("p", { class: "err", role: "alert" }, e.message)); }
  };
  recorder.start(); btn.setAttribute("aria-pressed", "true");
}

// ---------------------------------------------------------------- report
async function draftReport(form) {
  const box = $("#draft");
  if (!state.point) { box.replaceChildren(h("p", { class: "err", role: "alert" }, "Choose a location first (My block tab).")); return; }
  const fd = new FormData(form);
  fd.append("lat", state.point.lat); fd.append("lon", state.point.lon);
  if (!fd.get("photo") || !fd.get("photo").size) fd.delete("photo");
  box.replaceChildren(h("p", { class: "muted" }, t("loading")));
  try { renderDraft(await api("/api/report", { method: "POST", body: fd })); }
  catch (e) { box.replaceChildren(h("p", { class: "err", role: "alert" }, e.message)); }
}
function renderDraft(r) {
  const box = $("#draft");
  if (!r.ok) { box.replaceChildren(h("p", { class: "err", role: "alert" }, "We can't send this report.")); return; }
  if (r.needs_choice) {
    box.replaceChildren(h("p", {}, "What kind of hazard is it?"), h("div", { class: "row" }, r.choices.map((c) => h("button", { class: "secondary", type: "button", onclick: () => { const f = $("#report"); const i = h("input", { type: "hidden", name: "kind", value: c }); f.append(i); draftReport(f); i.remove(); } }, c.replace("_", " ")))));
    return;
  }
  const d = r.draft;
  const text = `${d.service_name}\n${d.description}\nLocation: ${d.address || `${d.lat}, ${d.lon}`}`;
  box.replaceChildren(
    r.duplicates.length ? h("p", { role: "status" }, `Already reported nearby ${ago(r.duplicates[0].requested_at)} ago (${r.duplicates[0].distance_m} m away). Crews may already know; you can still add your report.`) : null,
    h("h2", {}, d.service_name), h("p", {}, d.description), h("p", { class: "muted" }, d.address || `${d.lat}, ${d.lon}`),
    r.assessment ? h("p", { class: "src" }, `Photo check: ${r.assessment.kind.replace("_", " ")}${r.assessment.water_visible ? ", water visible" : ""} (confidence ${Math.round(r.assessment.confidence * 100)}%)`) : null,
    h("div", { class: "row" },
      h("button", { class: "secondary", type: "button", onclick: () => navigator.clipboard && navigator.clipboard.writeText(text) }, "Copy report text"),
      r.can_submit_directly ? h("button", { class: "primary", type: "button", onclick: () => submitDraft(d) }, "Send to 311") : h("a", { class: "primary", href: r.handoff_url, target: "_blank", rel: "noopener" }, "Open SF311 to send")),
    h("p", { class: "hint" }, "Nothing is sent until you confirm."));
}
async function submitDraft(d) {
  try { const r = await api("/api/report/submit", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ draft: d }) });
    $("#draft").append(h("p", { role: "status" }, r.ok ? `Sent. 311 case: ${r.service_request_id}` : "Please send it on SF311.")); }
  catch (e) { $("#draft").append(h("p", { class: "err", role: "alert" }, e.message)); }
}

// ---------------------------------------------------------------- alerts
async function subscribe(form) {
  const box = $("#subresult");
  const channel = form.channel.value;
  const body = { channel, address: $("#saddr").value, lang: state.lang, min_level: $("#minlevel").value };
  box.replaceChildren(h("p", { class: "muted" }, t("loading")));
  try {
    if (channel === "webpush") {
      const { publicKey } = await api("/api/push/key");
      if (!publicKey || !("serviceWorker" in navigator) || !("PushManager" in window)) throw new Error("Notifications aren't available here. Try Telegram or iMessage.");
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64(publicKey) });
      body.push_subscription = sub.toJSON();
      const r = await api("/api/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      try { localStorage.setItem("hg-sub", JSON.stringify({ id: r.subscription_id, token: r.unsubscribe_token })); } catch (_) {}
      box.replaceChildren(h("p", { role: "status" }, `Alerts are on for ${r.label}.`));
    } else if (channel === "telegram") {
      const r = await api("/api/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      box.replaceChildren(h("p", {}, "Open Telegram and tap Start:"), h("p", {}, link(r.url, "Open HighGround on Telegram")));
    } else {
      body.phone = $("#phone").value;
      const r = await api("/api/subscribe", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
      box.replaceChildren(h("p", {}, `Text “${r.message}” to ${r.number}.`), h("p", {}, link(r.sms_link, "Open Messages")));
    }
  } catch (e) { box.replaceChildren(h("p", { class: "err", role: "alert" }, e.message)); }
}
function urlB64(s) { const p = "=".repeat((4 - (s.length % 4)) % 4); const b = atob((s + p).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from([...b].map((c) => c.charCodeAt(0))); }

// ---------------------------------------------------------------- replay
async function enterReplay() {
  const sel = $("#rdate");
  if (!sel.options.length) {
    const idx = await api("/api/replay");
    idx.dates.slice().reverse().forEach((d) => sel.append(h("option", { value: d }, d === "2022-12-31" ? `${d} (record storm)` : d)));
    sel.value = idx.dates.includes("2022-12-31") ? "2022-12-31" : idx.dates[idx.dates.length - 1];
    state.calibration = idx.calibration;
  }
  if (state.map && state.map.getLayer("rcells")) ["risk", "reports", "density"].forEach((l) => state.map.setLayoutProperty(l, "visibility", "none"));
  await loadReplay(sel.value);
}
function exitReplay() {
  stopReplay();
  if (state.map && state.map.getLayer("rcells")) {
    ["rcells", "rreports"].forEach((l) => state.map.setLayoutProperty(l, "visibility", "none"));
    ["risk", "reports", "density"].forEach((l) => state.map.setLayoutProperty(l, "visibility", "visible"));
  }
}
async function loadReplay(date) {
  stopReplay();
  const p = await api(`/api/replay/${date}`);
  state.replay = p;
  const place = state.point || DEFAULT_PLACE;
  state.replay.placeCell = window.h3 ? h3.latLngToCell(place.lat, place.lon, 9) : null;
  $("#rplace").textContent = place.label || "your place";
  if (state.map && window.h3 && state.map.getSource("rcells")) {
    const feats = Object.keys(p.levels).map((c) => ({ type: "Feature", properties: { cell: c }, geometry: { type: "Polygon", coordinates: [h3.cellToBoundary(c, true)] } }));
    state.map.getSource("rcells").setData({ type: "FeatureCollection", features: feats });
    ["rcells", "rreports"].forEach((l) => state.map.setLayoutProperty(l, "visibility", "visible"));
    state.map.flyTo({ center: [-122.437, 37.76], zoom: 12 });
  }
  $("#rslider").max = p.steps.length - 1;
  renderBacktest(p);
  showStep(0);
}
function showStep(i) {
  const p = state.replay; if (!p) return;
  state.step = i; $("#rslider").value = i;
  const c = p.city[i];
  $("#rclock").textContent = c.local;
  if (state.map && state.map.getSource("rcells")) {
    for (const [cell, s] of Object.entries(p.levels)) state.map.setFeatureState({ source: "rcells", id: cell }, { lv: Number(s[i]) });
    const tt = new Date(p.steps[i]);
    state.map.getSource("rreports").setData({ type: "FeatureCollection", features: p.reports.filter((r) => new Date(r.t) <= tt && new Date(r.t) > tt - 6 * 3600e3).map((r) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [r.lon, r.lat] } })) });
  }
  const maxRain = Math.max(...p.city.map((x) => x.rain_1h_max_mm), 1);
  const reportsSoFar = p.city.slice(0, i + 1).reduce((a, x) => a + x.reports_15m, 0);
  $("#rstats").replaceChildren(
    h("p", {}, h("b", {}, `${c.levels[2] + c.levels[3]}`), " blocks at High or Severe · ", h("b", {}, `${reportsSoFar}`), " flood reports so far"),
    h("p", {}, `Rain, last hour (max across SF): ${(c.rain_1h_max_mm / 25.4).toFixed(2)} in · Tide: ${c.tide_ft == null ? "–" : c.tide_ft.toFixed(1) + " ft"}`),
    h("div", { class: "bars", "aria-hidden": "true" }, p.city.map((x, j) => h("span", { class: j === i ? "now" : "", style: `height:${Math.round((x.rain_1h_max_mm / maxRain) * 100)}%` }))),
    c.alerts.length ? h("p", {}, "NWS: ", c.alerts.join(", ")) : h("p", { class: "muted" }, "No NWS flood products in effect."),
  );
  renderPhone(i);
}
function renderPhone(i) {
  const p = state.replay, cell = p.placeCell, s = cell && p.levels[cell];
  const out = [];
  if (s) {
    let last = -1, lastStep = -99;
    for (let j = 0; j <= i; j++) {
      const lv = Number(s[j]);
      if (lv >= 2 && (lv > last || j - lastStep >= 12)) { out.push({ j, lv }); last = lv; lastStep = j; }
      if (lv < 2) last = Math.min(last, lv);
    }
  }
  $("#rmsgs").replaceChildren(...(out.length ? out.map(({ j, lv }) => h("li", {}, h("time", {}, p.city[j].local), `HighGround: ${t("lv_" + LV[lv])} near ${$("#rplace").textContent}. Move cars from low spots, put out sandbags, avoid flooded streets.`))
    : [h("li", { class: "muted" }, "No alerts yet.")]));
}
function renderBacktest(p) {
  const b = p.backtest, cal = state.calibration;
  const hrs = (m) => (m == null ? "–" : `${(m / 60).toFixed(1)} h`);
  $("#rbacktest").replaceChildren(
    h("p", {}, `${b.flooded_cells} blocks had flooding reports. HighGround reached High before the first report on ${b.warned_before_first_report} of them (median lead ${hrs(b.median_lead_min)}).`),
    h("p", {}, `${b.cells_ever_high} blocks reached High; ${b.false_alarm_cells} of those had no flooding report nearby that day (precision ${b.precision}).`),
    h("p", { class: "src" }, `First 311 flood report: ${new Date(b.first_report_citywide).toLocaleTimeString()} · First High: ${new Date(b.first_high_citywide).toLocaleTimeString()}`),
    cal ? h("p", { class: "src" }, `Thresholds calibrated on ${cal.train_days.join(", ")}; ${cal.holdout ? cal.holdout.date + " held out." : ""}`) : null,
    h("p", { class: "src" }, `Weather source: ${[].concat(p.sources.weather_obs || []).join(", ")}`),
  );
}
function stopReplay() { if (state.timer) clearInterval(state.timer); state.timer = null; const b = $("#rplay"); b.setAttribute("aria-pressed", "false"); b.textContent = t("play"); }
function togglePlay() {
  if (state.timer) { stopReplay(); return; }
  const b = $("#rplay"); b.setAttribute("aria-pressed", "true"); b.textContent = t("pause");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  state.timer = setInterval(() => { const n = state.step + 1; if (n >= state.replay.steps.length) { stopReplay(); return; } showStep(n); }, reduce ? 1500 : 450);
}

// ---------------------------------------------------------------- boot
function boot() {
  try { state.lang = localStorage.getItem("hg-lang") || (navigator.language || "en").slice(0, 2); } catch (_) {}
  if (![...$("#lang").options].some((o) => o.value === state.lang)) state.lang = "en";
  $("#lang").value = state.lang;
  applyI18n(); setupTabs(); initMap();
  $("#lang").addEventListener("change", (e) => { state.lang = e.target.value; try { localStorage.setItem("hg-lang", state.lang); } catch (_) {} applyI18n(); if (state.point) brief({ lat: state.point.lat, lon: state.point.lon }); });
  $("#search").addEventListener("submit", (e) => { e.preventDefault(); brief({ address: $("#q").value.trim() }); });
  $("#locate").addEventListener("click", () => navigator.geolocation && navigator.geolocation.getCurrentPosition((p) => brief({ lat: p.coords.latitude, lon: p.coords.longitude }), (err) => $("#brief").replaceChildren(h("p", { class: "err", role: "alert" }, err.message))));
  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask($("#question").value.trim()); });
  $("#mic").addEventListener("click", () => toggleMic().catch((e) => $("#answer").replaceChildren(h("p", { class: "err", role: "alert" }, e.message))));
  $("#report").addEventListener("submit", (e) => { e.preventDefault(); draftReport(e.target); });
  $("#subscribe").addEventListener("submit", (e) => { e.preventDefault(); subscribe(e.target); });
  document.querySelectorAll('input[name="channel"]').forEach((r) => r.addEventListener("change", () => document.querySelectorAll(".phone").forEach((el) => (el.hidden = $("#subscribe").channel.value !== "imessage"))));
  $("#rdate").addEventListener("change", (e) => loadReplay(e.target.value));
  $("#rslider").addEventListener("input", (e) => { stopReplay(); showStep(Number(e.target.value)); });
  $("#rplay").addEventListener("click", togglePlay);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  const params = new URLSearchParams(location.search);
  if (params.get("q")) { $("#q").value = params.get("q"); brief({ address: params.get("q") }); }
  else if (params.get("cell") && window.h3 && h3.isValidCell(params.get("cell"))) {
    const [lat, lon] = h3.cellToLatLng(params.get("cell"));
    brief({ lat, lon });
  }
}
boot();
