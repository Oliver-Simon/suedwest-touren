// Südwest-Touren service worker
// - App shell + all tour photos precached once per version (cache-first, instant offline start)
// - index.html served cache-first and revalidated in the background (stale-while-revalidate)
// - Map tiles: CORS requests only (no opaque responses → no quota padding), up to 3000 tiles
// - Only caches with the "suedwest-" prefix are ever deleted (the origin is shared with other PWAs)
const VERSION = "__VERSION__";
const PREFIX = "suedwest-";
const SHELL = ["./", "./manifest.json", "./icon-192.png", "./icon-512.png", "./icon-maskable-512.png"];
const IMAGES = __IMAGES__;
const TILE_CACHE = "suedwest-tiles-v2";
const TILE_HOSTS = [
  "tile.openstreetmap.org",
  "a.tile.opentopomap.org",
  "b.tile.opentopomap.org",
  "c.tile.opentopomap.org",
  "tile.waymarkedtrails.org",
  "a.tiles.openrailwaymap.org",
  "b.tiles.openrailwaymap.org",
  "c.tiles.openrailwaymap.org",
  "tile.memomaps.de",
  "a.tile-cyclosm.openstreetmap.fr",
  "b.tile-cyclosm.openstreetmap.fr",
  "c.tile-cyclosm.openstreetmap.fr",
  "server.arcgisonline.com"
];
const MAX_TILES = 3000;
const SHELL_KEY = new URL("./", self.location).href;

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(VERSION)
      .then(c => c.addAll(SHELL.concat(IMAGES).map(u => new Request(u, {cache: "reload"}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys
      .filter(k => k.startsWith(PREFIX) && k !== VERSION && k !== TILE_CACHE && k !== SHARE_CACHE)
      .map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

let tilePuts = 0;
async function trimTiles(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - MAX_TILES; i++) {
    await cache.delete(keys[i]);
  }
}

async function tileResponse(req) {
  const cache = await caches.open(TILE_CACHE);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok && res.type !== "opaque") {
      await cache.put(req, res.clone());
      if (++tilePuts % 50 === 0) trimTiles(cache);
    }
    return res;
  } catch (err) {
    return new Response("", {status: 504, statusText: "Offline"});
  }
}

async function shellResponse(e) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(SHELL_KEY);
  const refresh = fetch(SHELL_KEY, {cache: "no-cache"})
    .then(res => {
      if (res.ok) return cache.put(SHELL_KEY, res.clone()).then(() => res);
      return res;
    })
    .catch(() => null);
  if (cached) {
    e.waitUntil(refresh);
    return cached;
  }
  return (await refresh) || new Response("Offline", {status: 503, headers: {"Content-Type": "text/plain; charset=utf-8"}});
}

async function assetResponse(req) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(req, {ignoreSearch: true});
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok && res.type === "basic") cache.put(req, res.clone());
    return res;
  } catch (err) {
    return (await caches.match(req, {ignoreSearch: true})) || new Response("", {status: 504, statusText: "Offline"});
  }
}

// Web Share Target (manifest.share_target): RegioNavigator text / Mapy GPX → stored, page picks it up via ?share=1
const SHARE_CACHE = "suedwest-share";
async function handleShareTarget(req) {
  try {
    const fd = await req.formData();
    const file = fd.get("file");
    let payload;
    if (file && typeof file === "object" && file.size) {
      payload = {kind: "file", name: file.name || "", text: await file.text(), note: String(fd.get("text") || "")};
    } else {
      const text = [fd.get("title"), fd.get("text"), fd.get("url")].filter(Boolean).join("\n");
      payload = {kind: "text", name: String(fd.get("title") || ""), text};
    }
    const cache = await caches.open(SHARE_CACHE);
    await cache.put(new URL("./__shared__", self.location).href, new Response(JSON.stringify(payload), {headers: {"Content-Type": "application/json"}}));
  } catch (err) {}
  return Response.redirect(new URL("./?share=1", self.location).href, 303);
}

self.addEventListener("fetch", e => {
  if (e.request.method === "POST" && new URL(e.request.url).pathname.endsWith("/share-target")) {
    e.respondWith(handleShareTarget(e.request));
    return;
  }
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (TILE_HOSTS.includes(url.hostname)) {
    e.respondWith(tileResponse(req));
    return;
  }
  if (url.origin !== self.location.origin) return;
  const scope = new URL("./", self.location).pathname;
  if (!url.pathname.startsWith(scope)) return;
  const rel = url.pathname.slice(scope.length);
  if (req.mode === "navigate" || rel === "" || rel === "index.html") {
    e.respondWith(shellResponse(e));
    return;
  }
  if (rel === "sw.js") return;
  e.respondWith(assetResponse(req));
});

// Reminder notifications (shown via registration.showNotification): tap brings the app to the front.
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) if ("focus" in c) return c.focus();
    return self.clients.openWindow ? self.clients.openWindow("./") : undefined;
  }));
});
