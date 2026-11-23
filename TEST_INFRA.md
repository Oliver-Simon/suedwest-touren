# Test Infrastructure — Südwest-Touren (Jugenheim & Karlsruhe)

## 1. Overview & Opaque-Box Testing Philosophy

This document defines the requirement-driven, **opaque-box** End-to-End (E2E) test architecture for **Südwest-Touren (Jugenheim & Karlsruhe)**. All tests are derived exclusively from the authoritative specifications (`ORIGINAL_REQUEST.md`, `PROJECT.md`) and published Interface Contracts, completely independent of the implementation team's internal code.

### Strict Opaque-Box Boundaries
1. **Allowed Read-Only Inspection Targets**:
   - **Published Interface Contracts (`PROJECT.md`)**: `data/excluded_shortcuts.json`, `data/tours_catalog.json`, `data/tours_catalog_full.json`, `data/routing_notes.json`, `data/data.json`, `img/*.jpg`, `img_src/credits.json`, `index.html`, `sw.js`, `manifest.json`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `/home/oliver/suedwest_touren.html`.
   - **Authoritative Input Oracles (`ORIGINAL_REQUEST.md`)**: `data/phone_shortcuts.json`, `data/mapy/*.gpx`.
   - **Rendered Application**: Local static HTTP server (`127.0.0.1:<ephemeral_port>`) for `--target local`, standalone `file:///home/oliver/suedwest_touren.html`, and live GitHub Pages (`https://oliver-simon.github.io/suedwest-touren/`) for `--target live`.
2. **Forbidden Couplings**:
   - **NEVER** import `scripts.*`, `planner`, `planer.*`, `build_pwa`, or `make_icons`.
   - **NEVER** call internal JavaScript functions (`openTour`, `closeTour`, `BY_ID`, `TOURS`, `gpxFor`, `focusStep`, `planOf`, `applyLang`) or read internal JS globals (`HOME`, `DATA`) via `page.evaluate()`.
   - All UI interactions occur strictly via real DOM events (`click`, `fill`, `select_option`), standard Web APIs (`localStorage`, `navigator.serviceWorker`, `caches`, `getComputedStyle`, `document.documentElement.scrollWidth`), standard Leaflet `.leaflet-container` / `.leaflet-overlay-pane path`, `<a href>`, and Playwright `expect_download()`.

---

## 2. Environment & Tooling

- **Runtime**: Python 3.12.3
- **Test Runner**: Python stdlib `unittest` orchestrated via `tests/run_e2e.py` (chosen because `pytest`, `gpxpy`, and `httpx` are not installed in the system environment).
- **Libraries Used**:
  - `playwright.sync_api` (headless Chromium)
  - `PIL.Image` (JPEG/PNG dimension & format verification)
  - `requests` (HTTP/HTTPS live endpoint probing)
  - `bs4.BeautifulSoup` / `lxml` / `xml.etree.ElementTree` (HTML & GPX 1.1 XML validation)
  - Python stdlib (`json`, `math`, `hashlib`, `http.server`, `re`, `urllib.parse`, `datetime`, `tempfile`)

### Mobile Browser Harness Standard (`412×915`)
- **Viewport**: `412 × 915` (`device_scale_factor=2`, `is_mobile=True`, `has_touch=True`)
- **Locale & Timezone**: `locale="de-DE"`, `timezone_id="Europe/Berlin"`
- **Default Geolocation**: `49.753891, 8.629438` (Jugenheim, Bickenbacher Straße 24g), `permissions=["geolocation"]`
- **Zero-Error Gate**: Every Playwright session attaches `pageerror` and `console` (`error`) listeners. During intentional `set_offline(True)` tests, only expected external tile/weather/geocoding host failures (`tile.openstreetmap.org`, `opentopomap.org`, `waymarkedtrails.org`, `nominatim.openstreetmap.org`, `api.met.no`, `open-meteo`) are ignored; all JS exceptions and same-origin asset errors are hard failures.

---

## 3. Directory Layout

```
/home/oliver/suedwest_touren/
├── TEST_INFRA.md                   # This document
├── TEST_READY.md                   # Readiness signal after syntax + discovery + self-check pass
└── tests/
    ├── __init__.py
    ├── common.py                   # ContractLoader, PolylineDecoder (1e5), Haversine, GPX validator, HTTP server, MobileAppHarness, AppUI
    ├── test_tier1_coverage.py      # 125 Feature Coverage tests (T1-001 .. T1-125)
    ├── test_tier2_boundaries.py    # 125 Boundary & Corner-Case tests (T2-001 .. T2-125)
    ├── test_tier3_pairwise.py      #  26 Cross-Feature Pairwise tests (T3-001 .. T3-026)
    ├── test_tier4_scenarios.py     #  13 Real-World E2E Scenario tests (T4-001 .. T4-013)
    └── run_e2e.py                  # Unified progressive CLI (--tier, --target, --list, --self-check, --json-report, --fail-fast)
```

---

## 4. Feature Coverage (`N = 25`) & Tier Thresholds

| Tier | Name | Required Minimum | Implemented | Scope |
|---|---|---|---|---|
| **Tier 1** | Feature Coverage | `>= 5 * N = 125` | **125** (`T1-001..T1-125`) | 5 positive/contract tests per feature `F01..F25` |
| **Tier 2** | Boundary & Corner Cases | `>= 5 * N = 125` | **125** (`T2-001..T2-125`) | 5 edge/boundary/negative/encoding tests per feature `F01..F25` |
| **Tier 3** | Cross-Feature Pairwise | `>= N = 25` | **26** (`T3-001..T3-026`) | Pairwise interactions across switcher, modal, timeline, map, filters, i18n, dark mode, offline, exports |
| **Tier 4** | Real-World Scenarios | `>= max(5, N // 2) = 12` | **13** (`T4-001..T4-013`) | Full multi-step user journeys from startup through GPX/deep-links/offline/live GitHub Pages |
| **Total** | | `>= 288` | **289** | **100% of R1–R5 features `F01..F25` covered** |

### Feature Matrix (`F01..F25`)
- **R1 Data**: `F01` Commute & Utility Exclusion; `F02` Shortcut Pairing, 360° Loops & Station ID Recovery; `F03` GPX Deduplication, Validity & Metrics; `F04` GPX ↔ Shortcut & ≤800 m Stop Linking + 19 Waypoints.
- **R2 Routing**: `F05` 100% D-Ticket Enforcement (Zero Fernverkehr); `F06` ≥4-Minute Transfer Buffer (Hub-Aware); `F07` Hike Synchronization (`stay >= walkMin + 30`); `F08` Dual-Base Timetables (`jugenheim` `de:06432:24355`, `karlsruhe` `de:08212:89`), ≥3 Windows & `lastSafeReturn`; `F09` Ruftaxi/ALT/Flexline & Seasonal Bus Warnings; `F10` Unroutable Tour & Missing Day-Type Documentation.
- **R3 Curation & Media**: `F11` 20–30 Curated Highlight Tours across 3 Regions; `F12` Wikimedia Commons 800×450 JPEGs & License Credits.
- **R4 PWA & UI**: `F13` Offline PWA Shell, Manifest & 3000-Tile SW (`index.html`, `sw.js`); `F14` Standalone Single-File HTML (`/home/oliver/suedwest_touren.html`); `F15` Mobile Viewport `412×915` & Zero JS/Console Errors; `F16` Dual Home-Base Header Switcher (`🏠 Jugenheim` ↔ `❤️ Karlsruhe`); `F17` Home Location Modal (7 Presets, Nominatim, GPS, Custom, Reset); `F18` MVV-Style Product Badges & Beaded Timeline; `F19` Leaflet Map (Rail/Bus/Hike Layers, Elevation Badge & Controls); `F20` R4 Filter Bar, Search & Weekday Selector; `F21` Bilingual Toggle (`🇩🇪 DE` / `🇬🇧 EN`); `F22` Dark Mode (`auto`/`light`/`dark`); `F23` GPX 1.1 Download Validity; `F24` Transit Deep-Links (DB, MVV/SBM/HAFAS, Google Maps).
- **R5 Deployment**: `F25` GitHub Pages Live Deployment (`Oliver-Simon/suedwest-touren`).

---

## 5. Resilient Opaque-Box Locator Strategy

To avoid brittle coupling to internal CSS classes or variable names while remaining strictly opaque-box:
- **Base Switcher**: Header toggle/buttons matching `/Jugenheim|Karlsruhe/i`.
- **Language Toggle**: Header button matching `/^(🇩🇪\s*DE|🇬🇧\s*EN|DE|EN)$/i`.
- **Theme Toggle**: Button matching `aria-label` or visible text `/Dark|Hell|Dunkel|Theme|🌙|☀️/i`.
- **Weekday Tabs**: Buttons matching `/Mo[–-]Fr|Werktag|Weekday/i`, `/^Sa(mstag)?$|^Sat(urday)?$/i`, `/^So(nntag)?$|^Sun(day)?$/i`.
- **Filter Pills**: Buttons/chips matching R4 labels in DE or EN (*Alle*/*All*, *Meine Mapy*/*My Mapy*, *Neue Entdeckungen*/*New Discoveries*, *Pfälzerwald*, *Schwarzwald*/*Black Forest*, *Odenwald*, *Heute noch machbar*/*Feasible today*, *Abendsonne*/*Evening sun*).
- **Tour Cards & Detail**: Cards containing tour titles inside the tour list; detail view detected by visible back button (`/Zurück|Back|←|Alle Touren/i`), hero image, timeline steps, and export links.
- **Deep Links & GPX**: `a[href*="bahn.de"]`, `a[href*="mvv-muenchen.de"], a[href*="hafas.de"], a[href*="vrn.de"]`, `a[href*="google.com/maps"]`, and GPX download button matching `/GPX/i`.

---

## 6. Execution Commands

```bash
# List all 289 tests and verify tier counts
python3 tests/run_e2e.py --list

# Self-check (syntax, opaque-box hygiene, 289 count, synthetic temp-dir smoke run)
python3 tests/run_e2e.py --self-check

# Progressive tier execution against local build
python3 tests/run_e2e.py --tier 1 --target local --fail-fast
python3 tests/run_e2e.py --tier 2 --target local
python3 tests/run_e2e.py --tier 3 --target local
python3 tests/run_e2e.py --tier 4 --target local

# Full local acceptance run with JSON report
python3 tests/run_e2e.py --tier all --target local --json-report /tmp/e2e_local.json

# Full live GitHub Pages acceptance run (M5 post-deploy)
python3 tests/run_e2e.py --tier all --target live --live-url https://oliver-simon.github.io/suedwest-touren/ --json-report /tmp/e2e_live.json
```

### Target Modes (`--target local` vs `--target live`)
- **`--target local`** (default): Executes all data, media, PWA/SW, standalone HTML, and local Playwright `412×915` tests. Remote live-HTTPS checks in `F25` (`T1-122..T1-125`, `T2-122..T2-124`, `T3-026`, `T4-013`) report `SKIPPED_LOCAL` so M5 Phase 1 can validate 100% locally before `git push`.
- **`--target live`**: Enforces all `F25` live HTTPS checks against `--live-url` (`HTTP 200`, valid SSL, zero mixed-content, active HTTPS Service Worker, offline reload, and full live user journey). Any `SKIPPED_LOCAL` is disallowed in `--target live`.
