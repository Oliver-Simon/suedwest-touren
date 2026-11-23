# Project: Südwest-Touren (Jugenheim & Karlsruhe)

## Architecture
A 100% offline-capable Progressive Web App (PWA) and standalone single-file HTML (`/home/oliver/suedwest_touren.html`) for Deutschlandticket-only transit + Mapy hiking day tours from dual home bases (`Jugenheim` and `Karlsruhe`), deployed to GitHub Pages (`Oliver-Simon/suedwest-touren`).

```
data/ (shortcuts, raw dumps, 68 GPX)
  └─► [M1: scripts/build_catalog.py] ──► data/tours_catalog.json + data/excluded_shortcuts.json
          └─► [M2: scripts/curate_and_media.py] ──► data/tours_catalog_full.json + img/*.jpg + img_src/credits.json
                  └─► [M3: planner.py + planer/slim.py + planer/validate.py] ──► data/data.json + data/routing_notes.json
                          └─► [M4: template.html + build_pwa.py + sw.template.js] ──► index.html + sw.js + manifest.json + /home/oliver/suedwest_touren.html
                                  └─► [M5: Final E2E Pass (Tiers 1-4 + Tier 5) + gh deploy] ──► https://oliver-simon.github.io/suedwest-touren/

[Parallel E2E Track: E2E Testing Orchestrator] ──► tests/ (Tiers 1-4 opaque-box suite) + TEST_INFRA.md + TEST_READY.md
```

## Code Layout
```
/home/oliver/suedwest_touren/
├── PROJECT.md                     # Global architecture, milestones, contracts (Project Orchestrator)
├── TEST_INFRA.md                  # E2E testing architecture & thresholds (E2E Testing Orchestrator)
├── TEST_READY.md                  # Signal that opaque-box E2E suite is ready
├── .gitignore                     # Exclude cache/, raw dumps/backups if needed, .agents/, __pycache__/
├── index.html                     # Built PWA entry point (GitHub Pages root)
├── sw.js                          # Built Service Worker (3000-tile cache)
├── manifest.json                  # PWA Web Manifest
├── icon-192.png / icon-512.png / icon-maskable-512.png
├── template.html                  # MVV-style SPA template (/*LEAFLET_CSS*/, /*LEAFLET_JS*/, /*DATA*/)
├── sw.template.js                 # SW template (__VERSION__, TILE_HOSTS, MAX_TILES=3000)
├── build_pwa.py                   # Builds index.html, sw.js, and /home/oliver/suedwest_touren.html
├── make_icons.py                  # Generates PWA icons
├── planner.py                     # Dual-base D-Ticket timetable engine (Transitous/MOTIS 2 + VRN-EFA)
├── planer/
│   ├── __init__.py
│   ├── slim.py                    # Itinerary dedupe, capping, polyline compaction
│   └── validate.py                # Strict D-Ticket, ≥4 min transfer, walk+30 min stay, midnight validator
├── scripts/
│   ├── build_catalog.py           # M1: Shortcut + GPX parser, deduplicator, ≤800m stop linker
│   ├── curate_and_media.py        # M2: 20-30 curated highlight tours + Wikimedia Commons 800x450 image pipeline
│   └── validate_catalog.py        # M1/M2 schema & GPX geometry validator
├── vendor/
│   ├── leaflet.css                # Local Leaflet 1.9.4 CSS (copied from fjord_touren_webapp)
│   └── leaflet.js                 # Local Leaflet 1.9.4 JS
├── data/
│   ├── phone_shortcuts.json       # Input (read-only)
│   ├── phone_shortcuts.txt        # Input (read-only)
│   ├── android_shortcuts_raw.txt  # Input (read-only)
│   ├── app_manifests.txt          # Input (read-only)
│   ├── mapy/                      # Input 68 GPX files (read-only)
│   ├── excluded_shortcuts.json    # M1 output: filtered commute/utility shortcuts with reasons
│   ├── tours_catalog.json         # M1 output: consolidated shortcuts + GPX + nearest stops + waypoints
│   ├── tours_catalog_full.json    # M2 output: M1 catalog + 20-30 curated highlight tours
│   ├── routing_notes.json         # M3 output: unroutable tours / missing day types with reasons
│   └── data.json                  # M3 output: complete dual-base timetable & polyline payload
├── img/                           # M2 output: 800x450 JPEG photos (<tour_id>.jpg)
├── img_src/
│   └── credits.json               # M2 output: Wikimedia artist, license, licenseUrl, page
├── cache/                         # Persistent API response cache (Transitous, VRN-EFA, Wikimedia)
└── tests/                         # E2E & unit tests (Tiers 1-4 + Tier 5 adversarial + Playwright 412x915)
```
External deliverable outside repo: `/home/oliver/suedwest_touren.html` (standalone single-file HTML).

## Feature Inventory
Every feature from Survey (R1–R5) assigned to a milestone (zero unassigned features):

| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Shortcut Raw Recovery | Parse `android_shortcuts_raw.txt` + `phone_shortcuts.json` to recover `extId`, `remoteId`, `requestTime`, `type`, `productsField` for all 85 entries (fixing 135 `id: null` items) | M1 | R1 / Survey 1 |
| 2 | Commute & Utility Filtering | Exclude commute/daily shortcuts (`Hochschule Süd Dieburg`, `Moltkestraße 30 / HAK`, `Clay Kaserne`, `Stuttgart Universität`, `Waiblingen`, `Marienplatz→Stuttgart Hbf`, `Buy tickets`, etc.) into `data/excluded_shortcuts.json` with reasons | M1 | R1 / Survey 1 |
| 3 | Shortcut Pairing & 360° Loops | Consolidate outbound/return pairs into round/day tours; retain 19 `start == target == Jugenheim` with `viaLocation.0/.1` as 360° ring tours | M1 | R1 / Survey 1 |
| 4 | GPX Track Deduplication & Metrics | Analyze all 66 tracks, deduplicate exact SHA-256 duplicate (`65` unique), smooth `<ele>` jitter, compute distance (km), ascent/descent (m), Swiss-formula/Mapy hiking time (`walkMin`), elevation profile, encoded `1e5` polyline | M1 | R1 / Survey 1 |
| 5 | GPX ↔ Shortcut & ≤800 m Stop Linking | Link matching GPX tracks to shortcuts; query Transitous/VRN-EFA for nearest transit stops `<= 800 m` from GPX start/end for unmatched tracks (plus chain segments & expose 19 Mapy waypoints) | M1 | R1 / Survey 1, 3 |
| 6 | Curated Highlight Tours (20–30) | Hand-curate 20–30 new highlight tours across Pfälzerwald & Weinstraße, Nordschwarzwald & Mittlerer Schwarzwald, and Odenwald, Bergstraße & Neckartal (leveraging 19 Mapy waypoints like Eschkopfturm, Krähberg, Ohrsberg Tower) with bilingual metadata, difficulty, sun/weather tips | M2 | R3 / Survey 1, 3 |
| 7 | Wikimedia Commons Media Pipeline | Fetch freely licensed Wikimedia Commons photos (`CC-BY`/`CC-BY-SA`/`CC0`/`PD`/`GFDL`) for 100% of tours, center-crop 16:9 `800×450` JPEG in `img/<id>.jpg`, record license credits in `img_src/credits.json` | M2 | R3 / Survey 2, 3 |
| 8 | Dual-Engine Cached Routing (`planner.py`) | Implement Transitous/MOTIS 2 (`precision: 7` → `5`, UTC `Z` → `Europe/Berlin`, `de:06432:24355` fix) + VRN-EFA rapidJSON (`coordOutputFormat=WGS84[dd.ddddd]`, `exclMOT_0=on`, platform & `coords`), rate limit `≤1 req/s`, persistent cache in `cache/` | M3 | R2 / Survey 3 |
| 9 | Dual-Base Schedules & Windows | Compute real offline timetables for BOTH `base: "jugenheim"` (`de:06432:24355`) and `base: "karlsruhe"` (`de:08212:89`) across `wd` (Mo–Fr), `sa`, `so` with ≥3 departure windows + `lastSafeReturn` | M3 | R2 / Survey 3 |
| 10 | 100% D-Ticket & Transfer Buffer Validation | Reject all `ICE`/`IC`/`EC`/`FlixTrain`/paid special trains (`0` violations); enforce `>= 4 min` every transfer (prefer `>= 5–6 min` at major hubs in Python since MOTIS ignores `minTransferTime`) | M3 | R2 / Survey 3 |
| 11 | Hike Sync, Ruftaxi Warnings & Routing Notes | Enforce `dep_B - arr_A >= walkMin + 30 min`; auto-detect Ruftaxi/ALT/VRN-Flexline (`class: 10`, `ODM`/`FLEX`) & seasonal buses as `warnings[]`; log unroutable tours/day types in `data/routing_notes.json` | M3 | R2 / Survey 3 |
| 12 | Offline PWA & Standalone HTML Build | Adapt `build_pwa.py`, `sw.template.js` (`MAX_TILES=3000`, OSM/OpenTopoMap/WaymarkedTrails), `manifest.json`, `make_icons.py`, `vendor/leaflet.*` to output `index.html`, `sw.js`, icons, and `/home/oliver/suedwest_touren.html` (Base64-inline images) | M4 | R4 / Survey 2 |
| 13 | MVV-Style UI, Timeline & Leaflet Map | MVV product color badges (S-Bahn, RE/RB, KVV-Stadtbahn, Tram, Bus, Hike), beaded timeline with platforms, transfer pills, expandable `intermediateStops`, `lastSafeReturn`, warnings, and Leaflet map separating rail/bus/hike + elevation badge | M4 | R4 / Survey 2 |
| 14 | Dual-Base Switcher, Home Modal, Filters & i18n | Prominent `🏠 Jugenheim` ↔ `❤️ Karlsruhe` switcher updating all schedules/maps immediately, Home Modal (7 presets + Nominatim + GPS), all R4 filters (*Heute noch machbar ab*, *Abendsonne*, regions, source), DE/EN toggle, Dark Mode | M4 | R4 / Survey 2 |
| 15 | Deep-Links & GPX Export | Client-side DB (`bahn.de/buchung/fahrplan/suche#sts=true...` with `remoteId`/IBNR & D-Ticket filter), MVV/HAFAS (`efa.mvv-muenchen.de` / `s-bahn-muenchen.hafas.de`), Google Maps, and `🥾 GPX für Mapy` full-track GPX 1.1 export | M4 | R4 / Survey 2, 3 |
| 16 | Opaque-Box E2E Test Suite (Tiers 1–4) | Independent requirement-driven validator + Playwright (`412×915`) test suite covering all features across Tiers 1–4 (`TEST_INFRA.md` + `TEST_READY.md`) | E2E Track | Project Pattern / R5 |
| 17 | Final E2E Pass, Tier 5 Hardening & GH Pages Deploy | Pass 100% E2E Tiers 1–4 sequentially, execute Tier 5 white-box adversarial coverage hardening, initialize git, create & push `Oliver-Simon/suedwest-touren`, publish live GitHub Pages (`main`/`/`), verify live URL & standalone HTML | M5 (Final) | R5 / Project Pattern |

## Milestones

| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Data & GPX Engineering | Shortcut parsing/recovery (`android_shortcuts_raw.txt`), commute exclusion (`data/excluded_shortcuts.json`), 360° & round-trip consolidation, GPX deduplication & elevation/Swiss-time analysis, ≤800 m stop lookup, `data/tours_catalog.json` (Features #1–#5) | none | DONE (117 tours, 19 waypoints, 13 excluded, 64+1 GPX) |
| M2 | Curated Highlight Tours & Media | 20–30 curated new highlight tours across 3 Southwest regions + 19 Mapy waypoints + Wikimedia Commons 800×450 JPEG & license pipeline for 100% of tours, `data/tours_catalog_full.json`, `img/`, `img_src/credits.json` (Features #6–#7) | M1 | IN_PROGRESS |
| M3 | Transit Routing Engine (`planner.py`) | Transitous/MOTIS 2 + VRN-EFA rapidJSON cached dual-base router (`jugenheim` & `karlsruhe`), D-Ticket & ≥4 min transfer enforcement, `walkMin+30` sync, ≥3 windows + `lastSafeReturn`, Ruftaxi/seasonal warnings, `data/data.json`, `data/routing_notes.json`, `planer/validate.py` (Features #8–#11) | M1, M2 | PLANNED |
| M4 | Frontend & Offline PWA (`template.html` / `build_pwa.py`) | MVV-style timeline & product badges, Leaflet rail/bus/hike map + elevation badge, `🏠 Jugenheim` ↔ `❤️ Karlsruhe` switcher & Home Modal, R4 filters, DE/EN, Dark Mode, DB/MVV/GMaps/GPX deep-links, `sw.js` (3000 tiles), `index.html`, `/home/oliver/suedwest_touren.html` (Features #12–#15) | M3 | PLANNED |
| E2E | E2E Testing Track (Opaque-Box) | Independent requirement-driven E2E harness & test cases (Tiers 1–4, ~11×N + max(5,N/2) cases), `TEST_INFRA.md`, `TEST_READY.md` (Feature #16) | none (runs in parallel) | DONE (289 tests, TEST_READY.md published) |
| M5 | Final Milestone: E2E Pass, Tier 5 Hardening & GitHub Pages Deploy | Phase 1: pass 100% E2E Tiers 1→2→3→4; Phase 2: Tier 5 white-box Challenger→Worker→Reviewer coverage hardening; deploy `Oliver-Simon/suedwest-touren` to GitHub Pages & verify live URL + `/home/oliver/suedwest_touren.html` (Feature #17) | M4, E2E | PLANNED |

## Interface Contracts

### 1. M1 → M2 (`data/tours_catalog.json`) & M2 → M3 (`data/tours_catalog_full.json`)
```json
{
  "generated": "ISO-8601",
  "waypoints": [{"name": "...", "lat": 49.0, "lon": 8.0, "ele": 500, "region": "pfaelzerwald"}],
  "tours": [
    {
      "id": "slug-id",
      "source": "shortcut | mapy | curated",
      "region": "pfaelzerwald | schwarzwald | odenwald | weitere",
      "category": "wanderung | berg | see | burg | wein | stadt | panoramatour",
      "difficulty": "leicht | mittel | schwer",
      "ring": true,
      "title": {"de": "...", "en": "..."},
      "subtitle": {"de": "...", "en": "..."},
      "sun": {"de": "...", "en": "..."},
      "tips": {"de": "...", "en": "..."},
      "eveningSun": false,
      "places": ["Stop A", "POI", "Stop B"],
      "transit": {
        "kind": "shortcut_360 | shortcut_pair | shortcut_standalone | gpx_linked | curated",
        "startStop": {"name": "...", "lat": 49.0, "lon": 8.0, "dhid": "...", "ibnr": "...", "remoteId": "..."},
        "endStop": {"name": "...", "lat": 49.0, "lon": 8.0, "dhid": "...", "ibnr": "...", "remoteId": "..."},
        "vias": [{"name": "...", "lat": 49.0, "lon": 8.0, "dhid": "...", "ibnr": "...", "remoteId": "...", "durationMin": 60}],
        "walkToStartM": 120,
        "walkFromEndM": 180,
        "unlinkedReason": null
      },
      "hike": {
        "hasGpx": true,
        "gpxFiles": ["Route ...gpx"],
        "distKm": 11.4,
        "elevGainM": 420,
        "elevLossM": 410,
        "walkMin": 205,
        "startCoord": [49.0, 8.0],
        "endCoord": [49.0, 8.0],
        "poly5": "encoded_polyline_1e5",
        "elevProfile": [[0.0, 210], [0.5, 245]]
      }
    }
  ]
}
```

### 2. M2 → M4 (`img/<id>.jpg` & `img_src/credits.json`)
- `img/<id>.jpg`: `800×450` 16:9 JPEG (`quality=85`) for every tour `id` in `tours_catalog_full.json`.
- `img_src/credits.json`: `{ "<tour_id>": { "file": "File:...", "artist": "...", "license": "CC BY-SA 4.0", "licenseUrl": "https://...", "page": "https://commons.wikimedia.org/wiki/File:..." } }`.

### 3. M3 → M4 (`data/data.json` & `data/routing_notes.json`)
- Top-level `data.json`:
  ```json
  {
    "generated": "YYYY-MM-DDTHH:MM",
    "bases": {
      "jugenheim": {"name": "Jugenheim, Bickenbacher Straße 24g", "stopName": "Jugenheim Bickenbacher Straße, Seeheim-Jugenheim", "lat": 49.753891, "lon": 8.629438, "dhid": "de:06432:24355", "vrnId": "124355", "rmvId": "3024355", "nearestRail": {"name": "Bickenbach (Bergstr)", "ibnr": "8000956"}},
      "karlsruhe": {"name": "Karlsruhe Hbf", "stopName": "Karlsruhe Hbf", "lat": 48.993530, "lon": 8.401939, "dhid": "de:08212:89", "ibnr": "8000191"}
    },
    "days": {"wd": "2026-11-24", "sa": "2026-11-28", "so": "2026-11-29"},
    "tours": [
      {
        "...catalog_fields...",
        "durationMin": 380,
        "plans": {
          "jugenheim": { "wd": {"..."}, "sa": {"..."}, "so": {"..."} },
          "karlsruhe": { "wd": {"..."}, "sa": {"..."}, "so": {"..."} }
        },
        "unroutable": {"jugenheim": {}, "karlsruhe": {}}
      }
    ],
    "freq": {},
    "polys": ["encoded_polyline_1e5", "..."]
  }
  ```
- Each `plans[base][day]` object:
  - `fwd`: array of segments for default/recommended window (`{kind: "ride"|"walk"|"hike", mode: "sbahn"|"re"|"rb"|"stadtbahn"|"tram"|"bus"|"ferry"|"foot"|"hike", line, lineName, op, dep, arr, depIso, arrIso, from: {name, lat, lon, dhid, ibnr, remoteId, platform}, to: {...}, dest, transfer, minStay, stayNote, intermediateStops: [{name, arr, dep, platform, lat, lon}], warnings: [{type: "ruftaxi"|"seasonal"|"hub_tight", text}], legs: [{m, l, d, a, dist, p}]}`)
  - `rev`: array of return segments
  - `alts_fwd`: `[[...segments...], ...]` (≥ 3 departure windows: ~07:30–08:30, ~09:00–10:30, ~11:30–14:00 where available)
  - `alts_rev`: `[[...segments...], ...]` (matching valid returns satisfying `stay >= walkMin + 30`, with `lastSafeReturn` marked)
  - `lastSafeReturn`: `{"dep": "HH:MM", "arr": "HH:MM", "minStayAchieved": 250, "segs": [...]}` (or inside `alts_rev` flagged `isLastSafe: true`)
  - `fallback`: `false | true` with reason if unroutable for that `(base, day)`.
- All `polys[p]` encoded at **precision 5 (`1e5`)** for `template.html` `decodePoly()`.

### 4. M4 → M5 (`build_pwa.py` & Output Artifacts)
- `python3 build_pwa.py template.html data/data.json /home/oliver/suedwest_touren.html`
- Emits `/home/oliver/suedwest_touren/index.html`, `/home/oliver/suedwest_touren/sw.js`, and `/home/oliver/suedwest_touren.html`.
- `sw.js` caches app shell + map tiles up to `MAX_TILES = 3000`.

## v4 (2026-11-24): Bundle-Diät, UI-Überarbeitung, native Android-App

- **Daten**: `planer/appdata.py` interniert Halte/Segmente/Verbindungen (`DATA.stops/segs/its`, Pläne als Indexlisten mit `fi`/`ri` = Planer-Empfehlung). `index.html` 69 MB → 4,7 MB, Standalone 66 MB → 10,1 MB. `data/data.json` bleibt unverändert (Tests lesen es).
- **Polylinien-Fix**: `planer/slim.py` hat Legs, die `fwd`/`rev` und einem `alts_*`-Eintrag gemeinsam gehören, doppelt umindiziert (84–92 % falsche Strecken bei den empfohlenen Verbindungen). Root-Cause behoben; `appdata.PolyFixer` repariert die Verweise der bestehenden `data.json` beim Build (0 falsche Segmente danach).
- **Bilder**: `img/w/<id>.webp` (720×405) für PWA/APK, 560×315 inline für die Standalone-Datei; Encodes gecacht (`cache/webp_inline/`).
- **Service Worker**: Cache-first + Stale-while-revalidate für die Shell (nur `./`, kein doppeltes `index.html`), alle Fotos vorab gecacht, Kacheln nur als CORS-Antworten (keine opaque-Quota), löscht nur eigene `suedwest-*`-Caches (Origin wird mit der Fjord-PWA geteilt).
- **UI**: `color-scheme: only light` (Chrome-Auto-Dark), empfohlene Verbindung als Standard, „Max“ = letzte sichere Rückfahrt, 360°-Ringe ≠ Rundwanderung, Deep-Link-Datum rollt auf den nächsten passenden Tag, Filter-Panel (Machbar ab / Aufenthalt / Max. Anfahrt / Sortierung), 3 Primär-Aktionen + „Weitere Apps“ (MVV, NVV, myVRN, Navi, DB/MVV Web, Google Maps), Mapy-Direktlinks (`mapy.com/fnc/v1/route|showmap`), interaktives Höhenprofil, History/Zurück-Geste, Touch-Targets ≥ 40 px.
- **Android**: `android_app/` (siehe `android_app/README.md`); `build_pwa.py` schreibt die App-Assets nach `android_app/app/src/main/assets/www/`.
