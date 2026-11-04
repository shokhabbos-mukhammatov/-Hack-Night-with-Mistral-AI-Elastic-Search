// HighGround service worker: offline shell + last briefing, and Web Push alerts.
const SHELL = "hg-shell-v1";
const ASSETS = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/briefing")) {
    // network first; keep the last briefing for offline viewing
    e.respondWith(
      fetch(e.request)
        .then((r) => { const copy = r.clone(); caches.open(SHELL).then((c) => c.put("/last-briefing", copy)); return r; })
        .catch(() => caches.match("/last-briefing"))
    );
    return;
  }
  if (ASSETS.includes(url.pathname)) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  }
});

self.addEventListener("push", (e) => {
  let data = { title: "HighGround SF", body: "Flood risk changed near your place.", url: "/" };
  try { data = { ...data, ...e.data.json() }; } catch (_) {}
  e.waitUntil(self.registration.showNotification(data.title, { body: data.body, icon: "/static/icon.svg", data: { url: data.url } }));
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.openWindow((e.notification.data && e.notification.data.url) || "/"));
});
