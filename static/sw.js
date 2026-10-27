/* PowerSOC service worker. Served from /sw.js (root scope). The cache name is
   versioned from the app version (injected server-side, replacing __VERSION__)
   so a deploy invalidates stale caches. Strategy: cache-first for /static/
   assets; network-first for navigations + API (so data is fresh online and
   falls back to the last-known response offline). */
"use strict";
const VERSION = "__VERSION__";
const CACHE = "powersoc-" + VERSION;
// Static URLs are versioned (?v=APP_VERSION) by the server so they can be
// cached immutably; precache exactly the URLs the pages will request.
const V = "?v=" + VERSION;
const SHELL = [
  "/", "/static/css/main.css" + V, "/static/css/themes.css" + V,
  "/static/js/main.js" + V, "/static/js/theme.js" + V, "/static/js/chart.min.js" + V,
  "/static/icon.svg", "/static/manifest.webmanifest",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Never cache these: they carry credentials metadata, usernames, 2FA state
// and tokens — Cache Storage is readable from DevTools by anyone on the
// device, and persists after logout.
const SENSITIVE = [
  "/api/settings", "/api/users", "/api/2fa", "/api/sessions",
  "/api/account", "/api/reconcile", "/api/export.csv",   // balance, bills, full history
  "/api/backup", "/api/audit", "/api/brief",             // DB file, audit trail, receipts
  "/auth/", "/login", "/setup",
];

// A response answered from the cache because the network failed: re-wrap it
// with a marker header so the page shows it as stale ("updated <now>" used to
// appear over hours-old data during an outage).
function offline(hit) {
  const headers = new Headers(hit.headers);
  headers.set("X-PowerSOC-Offline", "1");
  return hit.blob().then((body) => new Response(body, {
    status: hit.status, statusText: hit.statusText, headers }));
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Logout: purge everything we've cached for this session (authed pages and
  // API payloads must not be readable offline after the user logs out), then
  // let the navigation proceed normally.
  if (url.pathname === "/auth/logout" || url.pathname === "/logout") {
    e.respondWith(caches.delete(CACHE).then(() => fetch(req)));
    return;
  }

  if (SENSITIVE.some((p) => url.pathname.startsWith(p))) return;

  // Static assets: cache-first (they're versioned/vendored, rarely change).
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // Navigations + API: network-first; fall back to the cached copy when
  // offline so the dashboard shows last-known data instead of failing. Only
  // cache OK responses — a transient 500/401 must not overwrite the last
  // good payload. The "/" fallback applies to page navigations only (an
  // uncached API request must reject as a network error, not return HTML).
  e.respondWith(
    fetch(req).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      } else if ((res.status === 401 || res.status === 403)
                 && url.pathname.startsWith("/api/")) {
        // Session expired or was revoked (not only an explicit /logout):
        // cached authed pages + payloads must not stay readable offline.
        caches.delete(CACHE);
      }
      return res;
    }).catch(() => caches.match(req).then((hit) => {
      if (hit) return url.pathname.startsWith("/api/") ? offline(hit) : hit;
      if (req.mode === "navigate") return caches.match("/");
      return Response.error();
    }))
  );
});
