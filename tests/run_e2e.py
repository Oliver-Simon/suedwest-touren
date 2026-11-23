#!/usr/bin/env python3
"""Unified progressive E2E test runner for Südwest-Touren (Tiers 1-4, 289 tests)."""

from __future__ import annotations

import argparse
import ast
import base64
import io
import json
import os
import py_compile
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from PIL import Image

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TIER_MODULES = {
    1: "tests.test_tier1_coverage",
    2: "tests.test_tier2_boundaries",
    3: "tests.test_tier3_pairwise",
    4: "tests.test_tier4_scenarios",
}

EXPECTED_COUNTS = {1: 125, 2: 125, 3: 26, 4: 13}

FORBIDDEN_IMPORT_ROOTS = {"scripts", "planner", "planer", "build_pwa", "make_icons"}
FORBIDDEN_JS_SYMBOLS = (
    "openTour",
    "closeTour",
    "BY_ID",
    "TOURS",
    "gpxFor",
    "focusStep",
    "planOf",
    "applyLang",
    "HOME",
    "DATA",
)


def _iter_suite_tests(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    out: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            out.extend(_iter_suite_tests(item))
        else:
            out.append(item)
    return out


def discover_tiers(tiers: list[int]) -> dict[int, unittest.TestSuite]:
    loader = unittest.TestLoader()
    suites: dict[int, unittest.TestSuite] = {}
    for t in tiers:
        suites[t] = loader.loadTestsFromName(TIER_MODULES[t])
    return suites


def check_opaque_box_hygiene() -> list[str]:
    violations: list[str] = []
    for py_file in sorted(TESTS_DIR.glob("*.py")):
        src = py_file.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN_IMPORT_ROOTS:
                        violations.append(f"{py_file.name}:{node.lineno}: forbidden import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                if mod in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"{py_file.name}:{node.lineno}: forbidden from-import {node.module}")
        if py_file.name != "run_e2e.py":
            for m in re.finditer(r"""evaluate\(([\s\S]{0,400}?)\)""", src):
                snippet = m.group(1)
                for sym in FORBIDDEN_JS_SYMBOLS:
                    if re.search(rf"\b{sym}\b", snippet):
                        violations.append(f"{py_file.name}: page.evaluate references forbidden JS symbol {sym}")
    return violations


class _StructuredResult(unittest.TextTestResult):
    def __init__(self, stream: Any, descriptions: bool, verbosity: int) -> None:
        super().__init__(stream, descriptions, verbosity)
        self.records: list[dict[str, Any]] = []
        self._t0: dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._t0[test.id()] = time.monotonic()

    def _rec(self, test: unittest.TestCase, status: str, detail: str = "") -> None:
        dt = time.monotonic() - self._t0.get(test.id(), time.monotonic())
        self.records.append(
            {"id": test.id(), "status": status, "duration_s": round(dt, 4), "detail": detail}
        )

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._rec(test, "PASS")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        status = "SKIPPED_LOCAL" if str(reason).startswith("SKIPPED_LOCAL") else "SKIPPED"
        self._rec(test, status, str(reason))

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._rec(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._rec(test, "ERROR", self._exc_info_to_string(err, test))


def run_tiers(
    tiers: list[int],
    target: str,
    live_url: str,
    fail_fast: bool,
    json_report: Path | None,
) -> int:
    os.environ["SUEDWEST_TARGET"] = target
    os.environ["SUEDWEST_LIVE_URL"] = live_url

    suites = discover_tiers(tiers)
    summary: dict[str, Any] = {
        "target": target,
        "live_url": live_url,
        "tiers": {},
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped_local": 0,
        "skipped_other": 0,
    }

    overall_ok = True
    for t in tiers:
        suite = suites[t]
        tests = _iter_suite_tests(suite)
        print(f"\n=== Running Tier {t} ({len(tests)} tests, target={target}) ===", flush=True)
        runner = unittest.TextTestRunner(
            stream=sys.stdout,
            verbosity=2,
            failfast=fail_fast,
            resultclass=_StructuredResult,
        )
        res: _StructuredResult = runner.run(suite)  # type: ignore[assignment]
        skipped_local = sum(1 for r in res.records if r["status"] == "SKIPPED_LOCAL")
        skipped_other = sum(1 for r in res.records if r["status"] == "SKIPPED")
        passed = sum(1 for r in res.records if r["status"] == "PASS")
        failed = len(res.failures)
        errors = len(res.errors)

        # In live mode, SKIPPED_LOCAL must never occur
        if target == "live" and skipped_local > 0:
            overall_ok = False
        if failed > 0 or errors > 0:
            overall_ok = False

        summary["tiers"][str(t)] = {
            "count": len(tests),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped_local": skipped_local,
            "skipped_other": skipped_other,
            "records": res.records,
        }
        summary["total"] += len(tests)
        summary["passed"] += passed
        summary["failed"] += failed
        summary["errors"] += errors
        summary["skipped_local"] += skipped_local
        summary["skipped_other"] += skipped_other

        if fail_fast and not overall_ok:
            break

    if json_report is not None:
        json_report.parent.mkdir(parents=True, exist_ok=True)
        json_report.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"\nSUMMARY: total={summary['total']} passed={summary['passed']} "
        f"failed={summary['failed']} errors={summary['errors']} "
        f"skipped_local={summary['skipped_local']}"
    )
    return 0 if overall_ok else 1


def _build_synthetic_fixture(tmp_dir: Path) -> Path:
    """Create a minimal compliant Interface Contract fixture in a temp dir for --self-check."""
    (tmp_dir / "data").mkdir(parents=True)
    (tmp_dir / "img").mkdir(parents=True)
    (tmp_dir / "img_src").mkdir(parents=True)

    # Sample 800x450 JPEG and PNG icons
    jpg_path = tmp_dir / "img" / "smoke-hike.jpg"
    Image.new("RGB", (800, 450), color=(46, 125, 50)).save(jpg_path, format="JPEG", quality=85)
    b64_img = "data:image/jpeg;base64," + base64.b64encode(jpg_path.read_bytes()).decode("ascii")

    for name, sz in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512)):
        Image.new("RGBA", (sz, sz), color=(21, 101, 192, 255)).save(tmp_dir / name, format="PNG")

    (tmp_dir / "img_src" / "credits.json").write_text(
        json.dumps(
            {
                "smoke-hike": {
                    "file": "File:Smoke.jpg",
                    "artist": "Test Author",
                    "license": "CC BY-SA 4.0",
                    "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0",
                    "page": "https://commons.wikimedia.org/wiki/File:Smoke.jpg",
                }
            }
        ),
        encoding="utf-8",
    )

    (tmp_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Südwest-Touren",
                "short_name": "Südwest",
                "id": "./",
                "start_url": "./",
                "scope": "./",
                "display": "standalone",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                ],
            }
        ),
        encoding="utf-8",
    )

    (tmp_dir / "sw.js").write_text(
        "const VERSION='suedwest-smoke';\n"
        "const SHELL=['./','./index.html','./manifest.json','./icon-192.png','./icon-512.png','./icon-maskable-512.png'];\n"
        "const TILE_HOSTS=['tile.openstreetmap.org','tile.opentopomap.org'];\n"
        "const MAX_TILES = 3000;\n"
        "self.addEventListener('install', e => e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())));\n"
        "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\n"
        "self.addEventListener('fetch', e => e.respondWith(caches.match(e.request).then(r => r || fetch(e.request))));\n",
        encoding="utf-8",
    )

    (tmp_dir / "data" / "excluded_shortcuts.json").write_text(
        json.dumps(
            [
                {"id": "7", "label": "Reichenbach zur Hochschule", "reason": "Daily commute to university"},
                {"id": "10", "label": "miltenberg nach dieburg", "reason": "Commute target Hochschule Süd, Dieburg"},
                {"id": "23", "label": "Marienplatz, Stuttgart -> Stuttgart Hbf", "reason": "Daily commute"},
                {"id": "25", "label": "Schwanen, Waiblingen -> Jugenheim", "reason": "Daily commute"},
                {"id": "52", "label": "-> Rathaus, Waiblingen", "reason": "Daily commute"},
                {"id": "53", "label": "Stuttgart Universität -> Pforzheim Hbf", "reason": "Daily commute"},
                {"id": "63", "label": "Buy tickets", "reason": "Utility shortcut without route"},
                {"id": "64", "label": "Graben-Neudorf -> HAK Moltkestraße 30", "reason": "Daily school commute"},
                {"id": "85", "label": "Clay Kaserne", "reason": "Daily workplace commute"},
            ]
        ),
        encoding="utf-8",
    )

    (tmp_dir / "data" / "routing_notes.json").write_text(json.dumps([]), encoding="utf-8")

    # Minimal valid 1e5 polyline in Pfälzerwald (~49.46, 8.17)
    sample_plan = {
        "fwd": [
            {
                "kind": "ride",
                "mode": "re",
                "line": "RE60",
                "lineName": "RE 60",
                "op": "DB Regio",
                "dep": "08:00",
                "arr": "08:30",
                "from": {"name": "Jugenheim Bickenbacher Straße", "lat": 49.753891, "lon": 8.629438, "ibnr": "8000956", "platform": "1"},
                "to": {"name": "Mannheim Hbf", "lat": 49.4796, "lon": 8.4694, "ibnr": "8000244", "platform": "5"},
                "transfer": 10,
                "intermediateStops": [{"name": "Bensheim", "arr": "08:15", "dep": "08:16"}],
            },
            {
                "kind": "ride",
                "mode": "rb",
                "line": "RB45",
                "lineName": "RB 45",
                "op": "DB Regio",
                "dep": "08:40",
                "arr": "09:15",
                "from": {"name": "Mannheim Hbf", "lat": 49.4796, "lon": 8.4694, "ibnr": "8000244", "platform": "7"},
                "to": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707, "ibnr": "8000703", "platform": "2"},
            },
        ],
        "rev": [
            {
                "kind": "ride",
                "mode": "rb",
                "line": "RB45",
                "lineName": "RB 45",
                "op": "DB Regio",
                "dep": "13:00",
                "arr": "13:35",
                "from": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707, "ibnr": "8000703", "platform": "1"},
                "to": {"name": "Jugenheim Bickenbacher Straße", "lat": 49.753891, "lon": 8.629438, "ibnr": "8000956", "platform": "1"},
            }
        ],
        "alts_fwd": [[{"kind": "ride", "mode": "re", "line": "RE60", "dep": "08:00", "arr": "09:15", "from": {"name": "Jugenheim", "lat": 49.7538, "lon": 8.6294}, "to": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707}}],
                     [{"kind": "ride", "mode": "re", "line": "RE60", "dep": "10:00", "arr": "11:15", "from": {"name": "Jugenheim", "lat": 49.7538, "lon": 8.6294}, "to": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707}}],
                     [{"kind": "ride", "mode": "re", "line": "RE60", "dep": "12:00", "arr": "13:15", "from": {"name": "Jugenheim", "lat": 49.7538, "lon": 8.6294}, "to": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707}}]],
        "alts_rev": [],
        "lastSafeReturn": {
            "dep": "18:30",
            "arr": "19:45",
            "segs": [
                {
                    "kind": "ride",
                    "mode": "rb",
                    "line": "RB45",
                    "dep": "18:30",
                    "arr": "19:45",
                    "from": {"name": "Bad Dürkheim", "lat": 49.4606, "lon": 8.1707},
                    "to": {"name": "Jugenheim", "lat": 49.753891, "lon": 8.629438},
                }
            ],
        },
        "fallback": False,
    }
    ka_plan = json.loads(json.dumps(sample_plan))
    ka_plan["fwd"][0]["from"] = {"name": "Karlsruhe Hbf", "lat": 48.99353, "lon": 8.401939, "ibnr": "8000191", "platform": "4"}
    ka_plan["fwd"][0]["dep"] = "08:25"
    ka_plan["rev"][0]["to"] = {"name": "Karlsruhe Hbf", "lat": 48.99353, "lon": 8.401939, "ibnr": "8000191", "platform": "4"}

    tour = {
        "id": "smoke-hike",
        "source": "curated",
        "region": "pfaelzerwald",
        "category": "wanderung",
        "difficulty": "leicht",
        "ring": True,
        "eveningSun": True,
        "title": {"de": "Bad Dürkheim Smoke Tour", "en": "Bad Dürkheim Smoke Tour"},
        "subtitle": {"de": "Testwanderung Pfalz", "en": "Test hike Palatinate"},
        "sun": {"de": "Abendsonne", "en": "Evening sun"},
        "tips": {"de": "Festes Schuhwerk", "en": "Sturdy shoes"},
        "places": ["Bad Dürkheim", "Eschkopf"],
        "hike": {
            "hasGpx": True,
            "gpxFiles": ["smoke.gpx"],
            "distKm": 6.5,
            "elevGainM": 180,
            "elevLossM": 180,
            "walkMin": 110,
            "startCoord": [49.4606, 8.1707],
            "endCoord": [49.4606, 8.1707],
            "poly5": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
            "elevProfile": [[0.0, 150], [3.2, 330], [6.5, 150]],
        },
        "plans": {
            "jugenheim": {"wd": sample_plan, "sa": sample_plan, "so": sample_plan},
            "karlsruhe": {"wd": ka_plan, "sa": ka_plan, "so": ka_plan},
        },
        "unroutable": {"jugenheim": {}, "karlsruhe": {}},
    }
    data_payload = {
        "generated": "2026-11-23T15:00",
        "bases": {
            "jugenheim": {
                "name": "Jugenheim, Bickenbacher Straße 24g",
                "stopName": "Jugenheim Bickenbacher Straße, Seeheim-Jugenheim",
                "lat": 49.753891,
                "lon": 8.629438,
                "dhid": "de:06432:24355",
                "nearestRail": {"name": "Bickenbach (Bergstr)", "ibnr": "8000956"},
            },
            "karlsruhe": {
                "name": "Karlsruhe Hbf",
                "stopName": "Karlsruhe Hbf",
                "lat": 48.993530,
                "lon": 8.401939,
                "dhid": "de:08212:89",
                "ibnr": "8000191",
            },
        },
        "days": {"wd": "2026-11-24", "sa": "2026-11-28", "so": "2026-11-29"},
        "tours": [tour],
        "freq": {},
        "polys": ["_p~iF~ps|U_ulLnnqC_mqNvxq`@"],
    }
    (tmp_dir / "data" / "data.json").write_text(json.dumps(data_payload), encoding="utf-8")

    # Minimal offline-self-contained HTML shell matching UI contract for Playwright smoke test
    html_tpl = f"""<!doctype html>
<html lang="de" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Südwest-Touren Smoke</title>
__MANIFEST__
<style>
  :root {{ --bg:#f7f8fa; --fg:#111827; --card:#ffffff; }}
  :root[data-theme="dark"] {{ --bg:#12151a; --fg:#f3f4f6; --card:#1f2937; }}
  body {{ margin:0; font-family:sans-serif; background:var(--bg); color:var(--fg); max-width:100vw; overflow-x:hidden; }}
  header {{ display:flex; flex-wrap:wrap; gap:6px; padding:8px; align-items:center; }}
  .card {{ background:var(--card); margin:8px; padding:10px; border-radius:8px; cursor:pointer; }}
  .badge {{ background:#0a7d32; color:#fff; padding:2px 6px; border-radius:4px; }}
  #detail, #homeModal {{ display:none; padding:10px; }}
  .leaflet-container {{ width:100%; height:180px; background:#e5e7eb; }}
  .dark-tiles .leaflet-tile-pane {{ filter: invert(1); }}
</style>
</head>
<body>
<header class="top">
  <button id="backBtn" style="display:none" onclick="goList()">← Zurück</button>
  <span id="homeLabel">Jugenheim</span>
  <button id="baseJg" onclick="setBase('jugenheim')">🏠 Jugenheim</button>
  <button id="baseKa" onclick="setBase('karlsruhe')">❤️ Karlsruhe</button>
  <button id="homeEditBtn" onclick="openModal()">📍</button>
  <button id="langBtn" onclick="toggleLang()">🇩🇪 DE</button>
  <button id="themeBtn" onclick="toggleTheme()">🌙 Dark</button>
</header>
<div id="controls">
  <input id="q" type="search" placeholder="Suche..." oninput="renderList()">
  <button onclick="setDay('wd')">Mo–Fr</button><button onclick="setDay('sa')">Sa</button><button onclick="setDay('so')">So</button>
  <button onclick="renderList()">Alle</button><button onclick="renderList()">Neue Entdeckungen</button><button onclick="renderList()">Pfälzerwald</button>
</div>
<div id="mapWrap" class="leaflet-container">
  <div class="leaflet-tile-pane"></div>
  <svg class="leaflet-overlay-pane" width="380" height="160">
    <path id="p1" d="M10,20 L150,90" stroke="#0a7d32" stroke-width="3" fill="none"></path>
    <path id="p2" d="M150,90 L300,40" stroke="#d32f2f" stroke-width="3" stroke-dasharray="4 4" fill="none"></path>
  </svg>
</div>
<main id="list"></main>
<section id="detail"></section>
<div id="homeModal" role="dialog">
  <button onclick="pickHome('Bickenbacher Straße 24g')">Bickenbacher Straße 24g</button>
  <button onclick="pickHome('Karlsruhe Hbf')">Karlsruhe Hbf</button>
  <button onclick="pickHome('Karlsruhe Durlacher Tor / KIT')">Karlsruhe Durlacher Tor / KIT</button>
  <button onclick="pickHome('Bickenbach (Bergstr) Bahnhof')">Bickenbach (Bergstr) Bahnhof</button>
  <button onclick="pickHome('Darmstadt Hbf')">Darmstadt Hbf</button>
  <button onclick="pickHome('Heidelberg Hbf')">Heidelberg Hbf</button>
  <button onclick="pickHome('Mannheim Hbf')">Mannheim Hbf</button>
  <input id="customHomeName" placeholder="Name"><input id="customHomeLat" placeholder="Lat"><input id="customHomeLon" placeholder="Lon">
  <button id="saveHomeBtn" onclick="saveCustom()">Speichern</button>
  <button id="homeGpsBtn" onclick="pickHome('GPS')">GPS</button>
  <button id="resetHomeBtn" onclick="pickHome('Jugenheim')">Zurücksetzen</button>
  <button onclick="closeModal()">Schließen</button>
</div>
<script>
const SMOKE_GEN = "2026-11-23T15:00";
const SMOKE_IMG = "{b64_img}";
let state = {{
  base: localStorage.getItem('suedwest:base') || 'jugenheim',
  lang: localStorage.getItem('suedwest:lang') || 'de',
  theme: localStorage.getItem('suedwest:theme') || 'light',
  day: 'wd',
  home: 'Jugenheim'
}};
document.documentElement.setAttribute('data-theme', state.theme);
function renderList() {{
  const q = (document.getElementById('q').value || '').toLowerCase();
  const list = document.getElementById('list');
  if (q && !'bad dürkheim smoke tour'.includes(q)) {{ list.innerHTML = '<p>0</p>'; return; }}
  const dep = state.base === 'karlsruhe' ? '08:25' : '08:00';
  list.innerHTML = `<div class="card" onclick="showDetail()"><b>Bad Dürkheim Smoke Tour</b><div>${{state.base}} • ${{dep}}</div></div>`;
}}
function showDetail() {{
  document.getElementById('list').style.display = 'none';
  document.getElementById('backBtn').style.display = 'inline-block';
  const d = document.getElementById('detail');
  d.style.display = 'block';
  const origin = state.base === 'karlsruhe' ? 'Karlsruhe Hbf' : 'Jugenheim';
  const dep = state.base === 'karlsruhe' ? '08:25' : '08:00';
  const db = `https://www.bahn.de/buchung/fahrplan/suche#sts=true&so=${{encodeURIComponent(origin)}}&zo=Bad%20D%C3%BCrkheim&hd=2026-11-24T${{dep}}&soid=A%3D1&zoid=A%3D1&vm=03`;
  const mvv = `https://efa.mvv-muenchen.de/index.html?origin=${{encodeURIComponent(origin)}}`;
  const gm = `https://www.google.com/maps/dir/?api=1&origin=49.75,8.62&destination=49.46,8.17&travelmode=transit`;
  d.innerHTML = `<h2>Bad Dürkheim Smoke Tour</h2>
    <img src="${{SMOKE_IMG}}" alt="hero" width="320">
    <div><a href="https://commons.wikimedia.org/wiki/File:Smoke.jpg">CC BY-SA 4.0</a></div>
    <div><span class="badge">RE60</span> ${{dep}} ${{origin}} Gleis 1 -> 09:15 Bad Dürkheim (10 Min. Umstieg)</div>
    <details><summary>Zwischenhalte</summary><div>08:15 Bensheim</div></details>
    <div>Letzte sichere Rückfahrt: 18:30</div>
    <div>6.5 km • 180 hm • 110 min</div>
    <button onclick="dlGpx()">🥾 GPX für Mapy</button>
    <a href="${{db}}">DB</a> <a href="${{mvv}}">MVV</a> <a href="${{gm}}">Maps</a>`;
}}
function goList() {{ document.getElementById('detail').style.display='none'; document.getElementById('backBtn').style.display='none'; document.getElementById('list').style.display='block'; }}
function setBase(b) {{ state.base = b; localStorage.setItem('suedwest:base', b); document.getElementById('p1').setAttribute('d', b==='karlsruhe'?'M20,140 L180,90':'M10,20 L150,90'); renderList(); if (document.getElementById('detail').style.display==='block') showDetail(); }}
function setDay(d) {{ state.day = d; renderList(); }}
function toggleLang() {{ state.lang = state.lang==='de'?'en':'de'; localStorage.setItem('suedwest:lang', state.lang); document.getElementById('langBtn').textContent = state.lang==='en'?'🇬🇧 EN':'🇩🇪 DE'; }}
function toggleTheme() {{ state.theme = state.theme==='dark'?'light':'dark'; localStorage.setItem('suedwest:theme', state.theme); document.documentElement.setAttribute('data-theme', state.theme); }}
function openModal() {{ document.getElementById('homeModal').style.display='block'; }}
function closeModal() {{ document.getElementById('homeModal').style.display='none'; }}
function pickHome(name) {{ state.home = name; document.getElementById('homeLabel').textContent = name; closeModal(); }}
function saveCustom() {{
  const lat = parseFloat(document.getElementById('customHomeLat').value), lon = parseFloat(document.getElementById('customHomeLon').value);
  if (!isFinite(lat) || !isFinite(lon) || Math.abs(lat)>90 || Math.abs(lon)>180) return;
  pickHome(document.getElementById('customHomeName').value || 'Custom');
}}
function dlGpx() {{
  const xml = '<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="49.7538" lon="8.6294"><name>Start</name></wpt><trk><name>Smoke</name><trkseg><trkpt lat="49.46" lon="8.17"><ele>150</ele></trkpt><trkpt lat="49.47" lon="8.18"><ele>180</ele></trkpt></trkseg></trk></gpx>';
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([xml], {{type:'application/gpx+xml'}})); a.download = 'smoke.gpx'; a.click();
}}
renderList();
</script>
__SW__
</body>
</html>"""

    index_html = html_tpl.replace(
        "__MANIFEST__", '<link rel="manifest" href="manifest.json">'
    ).replace(
        "__SW__", "<script>if('serviceWorker' in navigator && /^https?:$/.test(location.protocol)) navigator.serviceWorker.register('sw.js');</script>"
    )
    standalone_html = html_tpl.replace("__MANIFEST__", "").replace("__SW__", "")
    (tmp_dir / "index.html").write_text(index_html, encoding="utf-8")
    standalone_file = tmp_dir / "suedwest_touren.html"
    standalone_file.write_text(standalone_html, encoding="utf-8")
    return standalone_file


def run_self_check() -> int:
    print("1. Compiling tests/*.py ...")
    for py_file in sorted(TESTS_DIR.glob("*.py")):
        py_compile.compile(str(py_file), doraise=True)
    print("   OK (py_compile)")

    print("2. Checking opaque-box import & JS evaluation hygiene ...")
    violations = check_opaque_box_hygiene()
    if violations:
        for v in violations:
            print(f"   VIOLATION: {v}")
        return 1
    print("   OK (0 opaque-box violations)")

    print("3. Verifying test discovery counts (expect 125 + 125 + 26 + 13 = 289) ...")
    suites = discover_tiers([1, 2, 3, 4])
    total = 0
    for t in (1, 2, 3, 4):
        cnt = len(_iter_suite_tests(suites[t]))
        print(f"   Tier {t}: {cnt} tests (expected {EXPECTED_COUNTS[t]})")
        if cnt != EXPECTED_COUNTS[t]:
            print(f"   ERROR: Tier {t} count mismatch!")
            return 1
        total += cnt
    if total != 289:
        print(f"   ERROR: Total count {total} != 289")
        return 1
    print("   OK (289 tests verified)")

    print("4. Running isolated synthetic temp-dir smoke verification ...")
    old_root = os.environ.get("SUEDWEST_ROOT")
    old_sa = os.environ.get("SUEDWEST_STANDALONE")
    old_target = os.environ.get("SUEDWEST_TARGET")
    try:
        with tempfile.TemporaryDirectory(prefix="sw_e2e_selfcheck_") as tmp:
            tmp_path = Path(tmp)
            sa_path = _build_synthetic_fixture(tmp_path)
            os.environ["SUEDWEST_ROOT"] = str(tmp_path)
            os.environ["SUEDWEST_STANDALONE"] = str(sa_path)
            os.environ["SUEDWEST_TARGET"] = "local"

            smoke_tests = [
                "tests.test_tier1_coverage.TestT1_F01_CommuteExclusion.test_t1_001_excluded_shortcuts_file_exists_and_populated",
                "tests.test_tier1_coverage.TestT1_F05_DeutschlandticketOnly.test_t1_021_zero_forbidden_modes_in_default_fwd_rev",
                "tests.test_tier1_coverage.TestT1_F06_TransferBuffers.test_t1_026_default_fwd_rev_transfers_at_least_4_min",
                "tests.test_tier1_coverage.TestT1_F07_HikeSynchronization.test_t1_031_default_fwd_rev_stay_ge_walk_plus_30",
                "tests.test_tier1_coverage.TestT1_F08_DualBaseTimetables.test_t1_036_jugenheim_base_metadata_and_dhid",
                "tests.test_tier1_coverage.TestT1_F08_DualBaseTimetables.test_t1_037_karlsruhe_base_metadata_and_dhid",
                "tests.test_tier1_coverage.TestT1_F12_MediaAndCredits.test_t1_057_every_jpg_is_valid_800x450_jpeg",
                "tests.test_tier1_coverage.TestT1_F13_OfflinePwaAndServiceWorker.test_t1_061_manifest_json_valid_standalone",
                "tests.test_tier1_coverage.TestT1_F13_OfflinePwaAndServiceWorker.test_t1_062_pwa_png_icons_exist_and_exact_sizes",
                "tests.test_tier1_coverage.TestT1_F13_OfflinePwaAndServiceWorker.test_t1_063_sw_js_valid_tile_limit_and_hosts",
                "tests.test_tier1_coverage.TestT1_F15_MobileViewport.test_t1_071_initial_load_412x915_zero_errors",
                "tests.test_tier1_coverage.TestT1_F15_MobileViewport.test_t1_072_list_view_no_horizontal_overflow",
                "tests.test_tier1_coverage.TestT1_F15_MobileViewport.test_t1_073_card_count_matches_data_json",
                "tests.test_tier1_coverage.TestT1_F16_DualBaseSwitcher.test_t1_078_switching_base_in_detail_updates_timeline",
                "tests.test_tier1_coverage.TestT1_F17_HomeModal.test_t1_082_home_modal_has_all_7_presets",
                "tests.test_tier1_coverage.TestT1_F23_GpxDownload.test_t1_113_downloaded_gpx_parses_as_gpx_11_xml",
                "tests.test_tier1_coverage.TestT1_F24_DeepLinks.test_t1_117_db_deep_link_contains_so_zo_hd_dticket_params",
                "tests.test_tier1_coverage.TestT1_F25_GitHubPagesDeployment.test_t1_121_deployment_readiness_relative_paths",
                "tests.test_tier2_boundaries.TestT2_F20_FilterBoundaries.test_t2_098_search_xss_payload_safely_escaped",
                "tests.test_tier2_boundaries.TestT2_F25_DeploymentBoundaries.test_t2_125_standalone_and_index_report_same_count_and_generated",
            ]
            suite = unittest.TestLoader().loadTestsFromNames(smoke_tests)
            res = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
            if not res.wasSuccessful():
                print("   ERROR: Smoke verification failed!")
                return 1
    finally:
        for k, v in (("SUEDWEST_ROOT", old_root), ("SUEDWEST_STANDALONE", old_sa), ("SUEDWEST_TARGET", old_target)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("   OK (Temp synthetic fixture verified & cleaned up)")
    return 0


def cmd_list(tiers: list[int]) -> int:
    suites = discover_tiers(tiers)
    total = 0
    for t in tiers:
        tests = _iter_suite_tests(suites[t])
        print(f"Tier {t}: {len(tests)} tests")
        for tc in tests:
            print(f"  - {tc.id()}")
        total += len(tests)
    print(f"TOTAL: {total} tests")
    return 0 if (tiers != [1, 2, 3, 4] or total == 289) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Südwest-Touren Opaque-Box E2E Test Runner")
    parser.add_argument("--tier", default="all", choices=["1", "2", "3", "4", "all"], help="Tier to execute")
    parser.add_argument("--target", default="local", choices=["local", "live"], help="Execution target")
    parser.add_argument(
        "--live-url",
        default="https://oliver-simon.github.io/suedwest-touren/",
        help="Live GitHub Pages URL for --target live",
    )
    parser.add_argument("--json-report", type=Path, default=None, help="Output JSON report path")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure/error")
    parser.add_argument("--list", action="store_true", help="List discovered tests and verify counts")
    parser.add_argument("--self-check", action="store_true", help="Run syntax, hygiene, discovery & temp smoke checks")
    args = parser.parse_args()

    tiers = [1, 2, 3, 4] if args.tier == "all" else [int(args.tier)]

    if args.self_check:
        return run_self_check()
    if args.list:
        return cmd_list(tiers)
    return run_tiers(tiers, args.target, args.live_url, args.fail_fast, args.json_report)


if __name__ == "__main__":
    raise SystemExit(main())
