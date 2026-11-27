// Südwest-Touren service worker
// - App shell + all tour photos precached once per version (cache-first, instant offline start)
// - index.html served cache-first and revalidated in the background (stale-while-revalidate)
// - Map tiles: CORS requests only (no opaque responses → no quota padding), up to 3000 tiles
// - Only caches with the "suedwest-" prefix are ever deleted (the origin is shared with other PWAs)
const VERSION = "suedwest-20261127-171605";
const PREFIX = "suedwest-";
const SHELL = ["./", "./manifest.json", "./icon-192.png", "./icon-512.png", "./icon-maskable-512.png"];
const IMAGES = ["img/w/ahlmuehle-aussichtspunkt-schuhmacherfelsen.webp", "img/w/alb-murg-bus-baden-baden-gaggenau-ettlingen.webp", "img/w/albtal-falkenstein-bad-herrenalb.webp", "img/w/allerheiligen-wasserfaelle-oppenau.webp", "img/w/almersberg-eusserthal-albersweiler.webp", "img/w/annweiler-rehbergturm.webp", "img/w/annweiler-sarnstall-adelberg-aussicht.webp", "img/w/aussicht-bingen-klopp-rochusberg.webp", "img/w/aussicht-burg-hornberg-neckarzimmern.webp", "img/w/aussicht-burg-steinsberg-kraichgau.webp", "img/w/aussicht-ehrenbreitstein-koblenz.webp", "img/w/aussicht-freiburg-schlossbergturm.webp", "img/w/aussicht-rotenfels-bastei-nahe.webp", "img/w/bad-bergzabern-kurpark-runde.webp", "img/w/bad-herrenalb-kullenmuehle.webp", "img/w/bad-wildbad-sommerberg-baumwipfelpfad.webp", "img/w/bahnrunde-grosse-schwarzwald-achterbahn.webp", "img/w/bahnrunde-hoellental-dreiseen-schwarzwaldbahn.webp", "img/w/bahnrunde-mittelrhein-hunsrueckbahn-rheinhessen.webp", "img/w/bahnrunde-nagoldtal-hermann-hesse-bahn.webp", "img/w/bahnrunde-nordpfalz-lautertal-glan-alsenz.webp", "img/w/bahnrunde-oberes-donautal-schwarzwaldbahn.webp", "img/w/bahnrunde-odenwaldbahn-neckartal-bergstrasse.webp", "img/w/bahnrunde-westfranken-main-madonnenland-neckar.webp", "img/w/bauland-franken-bus-buchen-odenw-wuerzburg-hbf.webp", "img/w/bergstrasse-auerbacher-schloss-weinlagen.webp", "img/w/bergstrasse-starkenburg-heppenheim.webp", "img/w/boppard-buchholz-vierseenblick-mittelrhein.webp", "img/w/buchen-odenwald-bus-rueckfahrt.webp", "img/w/buchklingen-weinheim-schlossbergterrasse.webp", "img/w/burg-trifels-leinsweiler.webp", "img/w/burg-windeck-buehlertal-bosch.webp", "img/w/busrunde-albtal-kaeppele-murgtal-x44.webp", "img/w/busrunde-elztal-simonswald-furtwangen-triberg.webp", "img/w/busrunde-enztal-x78-murgtal.webp", "img/w/busrunde-kandel-glottertal-st-maergen-120-112.webp", "img/w/busrunde-nationalpark-erbeskopf-800-890.webp", "img/w/busrunde-rheinhessen-hiwwel-schweiz-660-440.webp", "img/w/busrunde-schauinsland-notschrei-feldberg-140-7300.webp", "img/w/busrunde-suedpfalz-weintor-wissembourg-lauterbourg.webp", "img/w/busrunde-taunus-x72-idstein-lahn.webp", "img/w/busrunde-ueberwald-neckartal-681-685.webp", "img/w/busrunde-wolftal-kniebis-murgtal-f13.webp", "img/w/calw-kloster-hirsau-nagoldtal.webp", "img/w/chain-drei-buchen-orensfelsen-godramstein.webp", "img/w/chain-edenkoben-rietburgbahn-ludwigsturm-weyher.webp", "img/w/chain-gertelbach-sand-schwarzenbach.webp", "img/w/chain-korsika-eiterbach-siedelsbrunn.webp", "img/w/chain-rhodt-rietburg-loop.webp", "img/w/chain-rietburg-weyher-loop.webp", "img/w/cramerhaus-burg-lindelbrunn.webp", "img/w/deidesheim-wachtenburg-wachenheim.webp", "img/w/dimbach-darstein-wasgau-felsenweg.webp", "img/w/doerrenbach-staeffelsberg-runde.webp", "img/w/dossenheim-strahlenburg-schriesheim.webp", "img/w/drei-buchen-burg-meistersel.webp", "img/w/drei-buchen-orensfelsen.webp", "img/w/drei-eichen-burg-guttenberg.webp", "img/w/eberbach-ledigsberg-neckarblick.webp", "img/w/ebersteinburg-battertfelsen-altes-schloss-westrunde.webp", "img/w/ebersteinburg-nordost-rundtour.webp", "img/w/edenkoben-ludwigshoehe-rietburgbahn.webp", "img/w/enz-albtal-verbindung-pforzheim-bad-herrenalb.webp", "img/w/enztal-schwarzwaldtour-bad-wildbad-via-calw.webp", "img/w/erlenbach-bei-dahn-niederschlettenbach.webp", "img/w/eschbach-burg-madenburg.webp", "img/w/eschkopfturm-elmstein-pfalz.webp", "img/w/freiolsheim-mahlberg-rundtour.webp", "img/w/gengenbach-altstadt-kastelberg.webp", "img/w/geroldsauer-wasserfall-baden-baden.webp", "img/w/gmaps-fuerth-odenwald-weschnitztal.webp", "img/w/gmaps-grosse-wiese-gernsbach.webp", "img/w/gmaps-historische-walddusche-gleisweiler.webp", "img/w/gmaps-kallstadt-bismarckturm-weinstrasse.webp", "img/w/gmaps-rosskopf-freiburg-schwarzwald.webp", "img/w/gmaps-xxl-liegebank-goetzenstein-birkenau.webp", "img/w/godramstein-frankweiler-orensfelsen.webp", "img/w/hambacher-schloss-neustadt.webp", "img/w/hauenstein-felsenrunde-ortsmitte.webp", "img/w/hauenstein-schwanheim.webp", "img/w/heidelberg-heiligenberg-handschuhsheim-neuenheim.webp", "img/w/heidelberger-hoehenbus-neuenheim-ziegelhausen-langer.webp", "img/w/herrenwies-badener-hoehe-friedrichsturm-rundtour.webp", "img/w/hoeningen-sonnenwende-bad-duerkheim.webp", "img/w/hornisgrinde-turm-kaltenbrunnen.webp", "img/w/idar-oberstein-markt-felsenkirche-schloss.webp", "img/w/kaiserslautern-betzenberg-moelschbach.webp", "img/w/kalmit-gipfel-maikammer.webp", "img/w/kaltenbronn-gaistal-talwiese.webp", "img/w/kaltenbronn-teufelsmuehle-gaistal-bernbach.webp", "img/w/karlstalschlucht-trippstadt-pavillon.webp", "img/w/katzenbuckel-gipfelrunde-waldkatzenbach.webp", "img/w/klingenmuenster-burg-landeck-pfalzklinikum.webp", "img/w/kraichgau-panoramatour-nach-karlsruhe-ueber-eppingen.webp", "img/w/kurpfalz-bus-s6-grosssachsen-schwetzingen-wiesloch.webp", "img/w/limburg-hardenburg-bad-duerkheim.webp", "img/w/loehrbach-unter-abtsteinach.webp", "img/w/loffenau-hoerden-murgtal.webp", "img/w/ludwigsdorf-korsika-eiterbach.webp", "img/w/ludwigsturm-weyher-altes-rathaus.webp", "img/w/lug-wernersberger-geierstein.webp", "img/w/main-tauber-tour-wertheim-wuerzburg-hbf.webp", "img/w/mainviereck-bus-miltenberg-wuerzburg-hbf.webp", "img/w/mummelsee-hornisgrinde-rundtour.webp", "img/w/mummelsee-hornisgrinde-unterstmatt.webp", "img/w/nationalpark-querung-ottenhoefen-hornisgrinde-oppena.webp", "img/w/neckartal-heidelberg-koenigstuhl-schloss.webp", "img/w/neckartal-ladenburg-altstadt-neckarwiese.webp", "img/w/neckartal-stauferstadt-bad-wimpfen-via-heinsheim.webp", "img/w/neckartal-vierburgensteig-dilsberg.webp", "img/w/nieder-liebersbach-weinheim-bergstrasse.webp", "img/w/nordpfalz-bahn-bus-bad-duerkheim-kirchheimbolanden.webp", "img/w/not-gottes-kapelle-melibokus-turm.webp", "img/w/nothweiler-rundtour-weglenburg-felsland.webp", "img/w/oberzent-kortelshuette-hirschhorn-bf.webp", "img/w/oberzent-rothenberg-hirschhorn-bf.webp", "img/w/odenwald-felsenmeer-lautertal.webp", "img/w/odenwald-franken-tour-mosbach-wuerzburg-hbf.webp", "img/w/odenwald-kraehberg-kailbach.webp", "img/w/odenwald-main-buslinie-eberbach-wuerzburg-hbf.webp", "img/w/odenwald-michelstadt-erbach-fachwerk.webp", "img/w/odenwald-ohrsberg-tower-eberbach.webp", "img/w/ortenau-korridor-kehl-rathaus-offenburg-buehl.webp", "img/w/pair-gertelbachschlucht-sand-kapelle-buehlertal.webp", "img/w/pair-loreley-plateau-taunus-runde-st-goarshausen-nas.webp", "img/w/pair-neckar-odenwald-runde-eberbach-mosbach-aglaster.webp", "img/w/pair-odenwald-dreilaender-runde-eulbach-amorbach-mud.webp", "img/w/pair-odenwald-main-querung-mudau-miltenberg-moemling.webp", "img/w/pair-pfaelzerwald-busrunde-ramstein-fischbach-franke.webp", "img/w/pair-rastatt-rhein-runde-via-blankenloch-illingen.webp", "img/w/pair-renchtal-ortenau-runde-sasbachried-oppenau-offe.webp", "img/w/pair-spessart-main-runde-rothenbuch-lohr-am-main-wue.webp", "img/w/r360-achern-freudenstadt.webp", "img/w/r360-bad-bergzabern-annweiler.webp", "img/w/r360-bad-herrenalb-buehl-rastatt.webp", "img/w/r360-bad-herrenalb-buehl.webp", "img/w/r360-baumholder-idar-oberstein.webp", "img/w/r360-bd-gruenstadt-wo.webp", "img/w/r360-eberbach-miltenberg.webp", "img/w/r360-forbach-buehl.webp", "img/w/r360-grosser-feldberg.webp", "img/w/r360-homburg-kusel.webp", "img/w/r360-k-town-trippstadt-waldfischbach.webp", "img/w/r360-karlsruhe-heilbronn.webp", "img/w/r360-koblenz-limburg.webp", "img/w/r360-lambrecht.webp", "img/w/r360-landau-germersheim.webp", "img/w/r360-neue-buslinie-gross-rohrheim-huettenfeld.webp", "img/w/r360-neue-runde-gernsheim.webp", "img/w/r360-nothweiler-oder-dahn-stunde-pause-tour.webp", "img/w/r360-rammstein-landstuhl.webp", "img/w/ranschbach-schuhmacherfelsen.webp", "img/w/rhodt-mitte-rietburgbahn.webp", "img/w/ried-bergstrasse-bus-610-worms-huettenfeld-bensheim.webp", "img/w/rietburg-ludwigsturm.webp", "img/w/rietburg-rhodt-unter-rietburg.webp", "img/w/sand-friedrichsturm-schwarzenbachtalsperre.webp", "img/w/sankenbach-wasserfall-baiersbronn.webp", "img/w/sasbachwalden-alde-gott-gaisholl.webp", "img/w/schwarzwald-querspange-karlsruhe-gruenwettersbach-pf.webp", "img/w/schwarzwaldhochstrasse-achern-oppenau-ruhestein.webp", "img/w/siedelsbrunn-buddhas-weg-eiterbach.webp", "img/w/spessart-jaegerwiese-rothenbuch.webp", "img/w/spessart-odenwald-korridor-miltenberg-wildensee-heid.webp", "img/w/stadt-esslingen-ludwigsburg-barock.webp", "img/w/stadt-maulbronn-bretten-kraichgau.webp", "img/w/stadt-seligenstadt-aschaffenburg-main.webp", "img/w/stadt-speyer-schwetzingen-kurpfalz.webp", "img/w/stadt-staufen-breisach-kaiserstuhl.webp", "img/w/suedpfalz-buslinie-wiesloch-walldorf-klingenmuenster.webp", "img/w/teufelstisch-hinterweidenthal.webp", "img/w/triberger-wasserfaelle-schwarzwaldbahn.webp", "img/w/varnhalt-yburg.webp", "img/w/voelkersbach-malsch.webp", "img/w/wattkopf-bismarckturm-ettlingen.webp", "img/w/weinbiet-gimmeldingen-haardt.webp", "img/w/weinstrasse-bus-500-st-martin-burrweiler-landau.webp", "img/w/wenschdorf-haagsaussicht-miltenberg.webp", "img/w/weyher-rietburgbahn.webp", "img/w/winterkasten-bismarckwarte-lindenfels.webp", "img/w/zwingenberg-baden-wolfsschlucht.webp"];
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
