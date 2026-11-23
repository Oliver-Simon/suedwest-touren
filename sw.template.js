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
  "tile.waymarkedtrails.org"
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
      .filter(k => k.startsWith(PREFIX) && k !== VERSION && k !== TILE_CACHE)
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

self.addEventListener("fetch", e => {
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
