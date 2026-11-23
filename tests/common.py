"""Shared opaque-box test infrastructure for Südwest-Touren E2E tests.

Strictly adheres to opaque-box rules:
- Inspects ONLY published Interface Contract artifacts and read-only input oracles.
- NEVER imports implementation modules (`scripts.*`, `planner`, `planer.*`, `build_pwa`, `make_icons`).
- NEVER calls internal JS functions or reads internal JS globals via `page.evaluate()`.
"""

from __future__ import annotations

import datetime as _dt
import functools
import glob
import hashlib
import http.server
import json
import math
import os
import re
import socketserver
import threading
import unittest
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from PIL import Image
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path("/home/oliver/suedwest_touren")
DEFAULT_STANDALONE = Path("/home/oliver/suedwest_touren.html")
DEFAULT_LIVE_URL = "https://oliver-simon.github.io/suedwest-touren/"

JUGENHEIM_BASE = {
    "lat": 49.753891,
    "lon": 8.629438,
    "dhid": "de:06432:24355",
    "vrnId": "124355",
    "rmvId": "3024355",
    "nearestRailIbnr": "8000956",
}

KARLSRUHE_BASE = {
    "lat": 48.993530,
    "lon": 8.401939,
    "dhid": "de:08212:89",
    "ibnr": "8000191",
}

REQUIRED_PRESETS = [
    "Bickenbacher Straße 24g",
    "Karlsruhe Hbf",
    "Karlsruhe Durlacher Tor",
    "Bickenbach",
    "Darmstadt Hbf",
    "Heidelberg Hbf",
    "Mannheim Hbf",
]

REQUIRED_COMMUTE_SUBSTRINGS = [
    "Hochschule Süd",
    "Moltkestraße 30",
    "HAK",
    "Clay Kaserne",
    "Stuttgart Universität",
    "Waiblingen",
    "Marienplatz",
    "Buy tickets",
]

FORBIDDEN_MODES = {
    "HIGHSPEED_RAIL",
    "LONG_DISTANCE",
    "NIGHT_RAIL",
    "AIRPLANE",
    "COACH",
}

FORBIDDEN_LINE_RE = re.compile(
    r"(?:^|\b)(?:ICE|IC|EC|ECE|FLX|FlixTrain|TGV|RJX|RJ|NJ|EN|EST|THA|Sonderzug)(?:\b|\d)",
    re.IGNORECASE,
)

FORBIDDEN_OP_RE = re.compile(
    r"(?:db\s+fernverkehr|flixtrain|sncf|öbb\s*nightjet|\bnightjet\b|thalys|eurostar)",
    re.IGNORECASE,
)

MAJOR_HUBS = (
    "mannheim hbf",
    "heidelberg hbf",
    "karlsruhe hbf",
    "frankfurt hbf",
    "darmstadt hbf",
    "stuttgart hbf",
)

RUFTAXI_RE = re.compile(
    r"(ruftaxi|rufbus|\balt\b|\bast\b|\balf\b|flexline|anruf|bedarfs|\bodm\b|\bflex\b|on[- ]?demand)",
    re.IGNORECASE,
)

SEASONAL_RE = re.compile(
    r"(kalmit[- ]?express|felsenland[- ]?express|kuckucksb[äa]hnel|nationalparkbus|mai\s*[–-]\s*oktober|saison|ausflugslinie|nur\s+sa/so)",
    re.IGNORECASE,
)

FREE_LICENSES_RE = re.compile(
    r"^(CC[\s-]?BY(?:[\s-]?SA)?(?:\s+\d\.\d)?|CC0|PD|Public\s+domain|GFDL|CC\s+Public\s+Domain)",
    re.IGNORECASE,
)

OFFLINE_EXTERNAL_HOST_RE = re.compile(
    r"(tile\.openstreetmap\.org|opentopomap\.org|waymarkedtrails\.org|nominatim\.openstreetmap\.org|api\.met\.no|open-meteo|wikimedia\.org)",
    re.IGNORECASE,
)


@dataclass
class TestConfig:
    root: Path = field(default_factory=lambda: Path(os.environ.get("SUEDWEST_ROOT", str(DEFAULT_ROOT))))
    standalone_path: Path = field(
        default_factory=lambda: Path(os.environ.get("SUEDWEST_STANDALONE", str(DEFAULT_STANDALONE)))
    )
    target: str = field(default_factory=lambda: os.environ.get("SUEDWEST_TARGET", "local").lower())
    live_url: str = field(default_factory=lambda: os.environ.get("SUEDWEST_LIVE_URL", DEFAULT_LIVE_URL))

    @classmethod
    def from_env(cls) -> "TestConfig":
        return cls()

    @property
    def is_live(self) -> bool:
        return self.target == "live"


def skip_unless_live(test_case: unittest.TestCase, cfg: TestConfig, reason: str = "") -> None:
    if not cfg.is_live:
        msg = f"SKIPPED_LOCAL: {reason or 'Requires --target live'}"
        test_case.skipTest(msg)


# ---------------------------------------------------------------------------
# Time, Geometry, Polyline, GPX & Domain Validation Helpers
# ---------------------------------------------------------------------------


def parse_hhmm(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if "T" in s:
        try:
            dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.hour * 60 + dt.minute
        except ValueError:
            pass
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def minutes_diff(dep: str, arr: str, dep_iso: Optional[str] = None, arr_iso: Optional[str] = None) -> int:
    if dep_iso and arr_iso:
        try:
            d0 = _dt.datetime.fromisoformat(dep_iso.replace("Z", "+00:00"))
            d1 = _dt.datetime.fromisoformat(arr_iso.replace("Z", "+00:00"))
            return int(round((d1 - d0).total_seconds() / 60.0))
        except ValueError:
            pass
    m0 = parse_hhmm(dep)
    m1 = parse_hhmm(arr)
    if m0 is None or m1 is None:
        raise ValueError(f"Invalid time pair: dep={dep!r}, arr={arr!r}")
    diff = m1 - m0
    if diff < 0:
        diff += 1440
    return diff


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    if not encoded:
        return []
    scale = 10 ** precision
    coords: list[tuple[float, float]] = []
    idx = 0
    lat = 0
    lon = 0
    n = len(encoded)
    while idx < n:
        for is_lon in (False, True):
            shift = 0
            result = 0
            while idx < n:
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / scale, lon / scale))
    return coords


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def polyline_length_km(pts: list[tuple[float, float]]) -> float:
    if len(pts) < 2:
        return 0.0
    total_m = 0.0
    for i in range(1, len(pts)):
        total_m += haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
    return total_m / 1000.0


def in_sw_bounds(lat: float, lon: float, strict: bool = False) -> bool:
    if strict:
        return 47.0 <= lat <= 51.5 and 6.0 <= lon <= 11.0
    return 47.0 <= lat <= 52.0 and 5.5 <= lon <= 11.5


def validate_gpx_xml(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    tag = root.tag
    if "}" in tag:
        ns_uri, local = tag[1:].split("}", 1)
    else:
        ns_uri, local = "", tag
    if local != "gpx":
        raise AssertionError(f"Expected <gpx> root tag, got <{local}>")
    version = root.attrib.get("version", "")
    ns = {"g": ns_uri} if ns_uri else {}

    def _find_all(parent: ET.Element, name: str) -> list[ET.Element]:
        return parent.findall(f"g:{name}", ns) if ns else parent.findall(name)

    def _find_text(parent: ET.Element, name: str) -> str:
        el = parent.find(f"g:{name}", ns) if ns else parent.find(name)
        return (el.text or "").strip() if el is not None and el.text else ""

    wpts = []
    for w in _find_all(root, "wpt"):
        wpts.append(
            {
                "lat": float(w.attrib["lat"]),
                "lon": float(w.attrib["lon"]),
                "name": _find_text(w, "name"),
            }
        )

    trks = []
    for t in _find_all(root, "trk"):
        pts = []
        for seg in _find_all(t, "trkseg"):
            for p in _find_all(seg, "trkpt"):
                ele_str = _find_text(p, "ele")
                pts.append(
                    {
                        "lat": float(p.attrib["lat"]),
                        "lon": float(p.attrib["lon"]),
                        "ele": float(ele_str) if ele_str else None,
                    }
                )
        trks.append({"name": _find_text(t, "name"), "points": pts})

    return {"version": version, "namespace": ns_uri, "wpts": wpts, "trks": trks}


def is_forbidden_fernverkehr(seg: dict[str, Any]) -> Optional[str]:
    mode = str(seg.get("mode", "")).strip().upper()
    if mode in FORBIDDEN_MODES:
        return f"Forbidden mode={mode}"
    for key in ("line", "lineName", "displayName", "tripShortName", "trainType", "l"):
        val = str(seg.get(key, "") or "")
        if val and FORBIDDEN_LINE_RE.search(val):
            return f"Forbidden line {key}={val!r}"
    for key in ("op", "agencyName", "operator"):
        val = str(seg.get(key, "") or "")
        if val and FORBIDDEN_OP_RE.search(val):
            return f"Forbidden operator {key}={val!r}"
    if seg.get("class") == 0:
        return "Forbidden VRN product class=0"
    for sub in seg.get("legs") or []:
        if isinstance(sub, dict):
            hit = is_forbidden_fernverkehr(sub)
            if hit:
                return hit
    return None


def compute_transfers(segs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], int, str]]:
    """Return [(prev_ride, next_ride, gap_min, station_name)] for every ride->ride transition."""
    rides = [(i, s) for i, s in enumerate(segs) if s.get("kind") == "ride"]
    out = []
    for k in range(1, len(rides)):
        _, prev_r = rides[k - 1]
        _, next_r = rides[k]
        arr_t = prev_r.get("arr", "")
        dep_t = next_r.get("dep", "")
        if not arr_t or not dep_t:
            continue
        gap = minutes_diff(arr_t, dep_t, prev_r.get("arrIso"), next_r.get("depIso"))
        # If gap > 720, likely an unintended non-wrap or overnight; keep raw positive diff
        hub = (
            (next_r.get("from") or {}).get("name")
            or (prev_r.get("to") or {}).get("name")
            or ""
        )
        out.append((prev_r, next_r, gap, hub))
    return out


def is_major_hub(stop_name: str) -> bool:
    low = (stop_name or "").lower()
    return any(h in low for h in MAJOR_HUBS)


def compute_hike_stay(fwd_segs: list[dict[str, Any]], rev_segs: list[dict[str, Any]]) -> Optional[int]:
    """Compute stay duration (minutes) between arrival at hike start A and departure from hike end B."""
    if not fwd_segs or not rev_segs:
        return None
    # If hike segment is embedded at the tail of fwd_segs, arrival at A is just before hike
    arr_seg = fwd_segs[-1]
    if arr_seg.get("kind") == "hike" and len(fwd_segs) >= 2:
        arr_seg = fwd_segs[-2]
    dep_seg = rev_segs[0]
    if dep_seg.get("kind") == "hike" and len(rev_segs) >= 2:
        dep_seg = rev_segs[1]
    arr_t = arr_seg.get("arr")
    dep_t = dep_seg.get("dep")
    if not arr_t or not dep_t:
        return None
    return minutes_diff(arr_t, dep_t, arr_seg.get("arrIso"), dep_seg.get("depIso"))


def seg_is_ruftaxi_text(seg: dict[str, Any]) -> bool:
    blob = " ".join(
        str(seg.get(k, "") or "")
        for k in ("line", "lineName", "op", "mode", "dest", "note", "stayNote")
    )
    return bool(RUFTAXI_RE.search(blob))


def seg_is_seasonal_text(seg: dict[str, Any]) -> bool:
    blob = " ".join(
        str(seg.get(k, "") or "")
        for k in ("line", "lineName", "op", "dest", "note", "stayNote")
    )
    return bool(SEASONAL_RE.search(blob))


# ---------------------------------------------------------------------------
# Published Interface Contract Loader
# ---------------------------------------------------------------------------


class ContractLoader:
    """Read-only loader for PROJECT.md Interface Contracts and ORIGINAL_REQUEST.md input oracles."""

    def __init__(self, cfg: Optional[TestConfig] = None):
        self.cfg = cfg or TestConfig.from_env()
        self.root = self.cfg.root

    def path(self, rel: str) -> Path:
        return self.root / rel

    def load_json(self, rel: str) -> Any:
        p = self.path(rel)
        if not p.exists():
            raise FileNotFoundError(f"Required contract artifact missing: {p}")
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def excluded_shortcuts(self) -> list[dict[str, Any]]:
        raw = self.load_json("data/excluded_shortcuts.json")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for k in ("excluded", "items", "shortcuts", "entries"):
                if isinstance(raw.get(k), list):
                    return raw[k]
        raise AssertionError(f"Unexpected excluded_shortcuts.json structure: {type(raw)}")

    def tours_catalog(self) -> dict[str, Any]:
        return self.load_json("data/tours_catalog.json")

    def tours_catalog_full(self) -> dict[str, Any]:
        return self.load_json("data/tours_catalog_full.json")

    def routing_notes(self) -> Any:
        return self.load_json("data/routing_notes.json")

    def data_json(self) -> dict[str, Any]:
        return self.load_json("data/data.json")

    def credits_json(self) -> dict[str, Any]:
        return self.load_json("img_src/credits.json")

    def manifest_json(self) -> dict[str, Any]:
        return self.load_json("manifest.json")

    def phone_shortcuts(self) -> list[dict[str, Any]]:
        return self.load_json("data/phone_shortcuts.json")

    def raw_gpx_files(self) -> list[Path]:
        return sorted(self.path("data/mapy").glob("*.gpx"))

    def unique_individual_gpx_hashes(self) -> dict[str, list[Path]]:
        by_hash: dict[str, list[Path]] = {}
        for p in self.raw_gpx_files():
            if p.name in ("My Tours.gpx", "points_26_11_23_143109.gpx"):
                continue
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            by_hash.setdefault(h, []).append(p)
        return by_hash

    def raw_waypoints(self) -> list[dict[str, Any]]:
        p = self.path("data/mapy/points_26_11_23_143109.gpx")
        parsed = validate_gpx_xml(p.read_text(encoding="utf-8"))
        return parsed["wpts"]

    @staticmethod
    def tour_title(tour: dict[str, Any], lang: str = "de") -> str:
        t = tour.get("title")
        if isinstance(t, dict):
            return str(t.get(lang) or t.get("de") or t.get("en") or "")
        return str(t or "")

    @staticmethod
    def hike_info(tour: dict[str, Any]) -> dict[str, Any]:
        return tour.get("hike") or {}

    @staticmethod
    def walk_min(tour: dict[str, Any]) -> int:
        h = tour.get("hike") or {}
        val = h.get("walkMin") or h.get("hikeMin") or tour.get("walkMin") or tour.get("hikeMin") or 0
        return int(val or 0)

    @staticmethod
    def plans_for(tour: dict[str, Any]) -> dict[str, Any]:
        return tour.get("plans") or tour.get("plans_by_base") or {}

    @staticmethod
    def last_safe_return(plan: dict[str, Any]) -> Optional[dict[str, Any]]:
        lsr = plan.get("lastSafeReturn")
        if isinstance(lsr, dict):
            return lsr
        for alt in plan.get("alts_rev") or []:
            if isinstance(alt, dict) and alt.get("isLastSafe"):
                return alt
        return None

    @staticmethod
    def routing_notes_entries(notes_raw: Any) -> list[dict[str, Any]]:
        if isinstance(notes_raw, list):
            return [x for x in notes_raw if isinstance(x, dict)]
        if isinstance(notes_raw, dict):
            for k in ("notes", "unroutable", "items", "entries"):
                if isinstance(notes_raw.get(k), list):
                    return [x for x in notes_raw[k] if isinstance(x, dict)]
            out = []
            for k, v in notes_raw.items():
                if isinstance(v, dict):
                    item = dict(v)
                    item.setdefault("tour_id", k)
                    out.append(item)
                elif isinstance(v, str):
                    out.append({"tour_id": k, "reason": v})
            return out
        return []

    @staticmethod
    def is_plan_routable(plan: Optional[dict[str, Any]]) -> bool:
        if not isinstance(plan, dict):
            return False
        if plan.get("fallback") is True:
            return False
        return bool(plan.get("fwd")) and bool(plan.get("rev"))


# ---------------------------------------------------------------------------
# Ephemeral Local Static HTTP Server
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript; charset=utf-8",
        ".mjs": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".gpx": "application/gpx+xml",
    }

    def log_message(self, format: str, *args: Any) -> None:
        return


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class StaticServer:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._server: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> "StaticServer":
        handler = functools.partial(_QuietHandler, directory=str(self.directory))
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ---------------------------------------------------------------------------
# Playwright Mobile App Harness & Opaque-Box UI Locators
# ---------------------------------------------------------------------------


class MobileAppHarness:
    """Manages headless Chromium at 412x915 mobile viewport with zero-error gate."""

    def __init__(
        self,
        viewport: tuple[int, int] = (412, 915),
        color_scheme: str = "light",
        geolocation: Optional[dict[str, float]] = None,
        grant_geolocation: bool = True,
        offline_allowed: bool = False,
    ):
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self.color_scheme = color_scheme
        self.geolocation = geolocation or {"latitude": 49.753891, "longitude": 8.629438}
        self.grant_geolocation = grant_geolocation
        self.offline_allowed = offline_allowed
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.external_requests: list[str] = []

    def __enter__(self) -> "MobileAppHarness":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        is_mobile = self.viewport["width"] < 900
        ctx_kwargs: dict[str, Any] = {
            "viewport": self.viewport,
            "device_scale_factor": 2 if is_mobile else 1,
            "is_mobile": is_mobile,
            "has_touch": is_mobile,
            "locale": "de-DE",
            "timezone_id": "Europe/Berlin",
            "color_scheme": self.color_scheme,
        }
        if self.grant_geolocation:
            ctx_kwargs["permissions"] = ["geolocation"]
            ctx_kwargs["geolocation"] = self.geolocation
        self.context = self._browser.new_context(**ctx_kwargs)
        self.page = self.context.new_page()
        self._attach_listeners(self.page)
        return self

    def _attach_listeners(self, page: Page) -> None:
        def on_pageerror(err: Any) -> None:
            self.page_errors.append(str(err))

        def on_console(msg: Any) -> None:
            if msg.type != "error":
                return
            text = msg.text or ""
            loc = getattr(msg, "location", {}) or {}
            url = str(loc.get("url", "") or "")
            if self.offline_allowed and (
                OFFLINE_EXTERNAL_HOST_RE.search(url)
                or OFFLINE_EXTERNAL_HOST_RE.search(text)
                or ("ERR_INTERNET_DISCONNECTED" in text and "127.0.0.1" not in url and "localhost" not in url)
                or ("Failed to load resource" in text and (not url or OFFLINE_EXTERNAL_HOST_RE.search(url)))
            ):
                return
            self.console_errors.append(f"{text} ({url})" if url else text)

        def on_request(req: Any) -> None:
            u = req.url or ""
            if u.startswith("http://") or u.startswith("https://"):
                if "127.0.0.1" not in u and "localhost" not in u:
                    self.external_requests.append(u)

        page.on("pageerror", on_pageerror)
        page.on("console", on_console)
        page.on("request", on_request)

    def clear_errors(self) -> None:
        self.console_errors.clear()
        self.page_errors.clear()

    def assert_zero_errors(self, ctx_label: str = "") -> None:
        prefix = f"[{ctx_label}] " if ctx_label else ""
        if self.page_errors or self.console_errors:
            raise AssertionError(
                f"{prefix}Browser errors detected: page_errors={self.page_errors}, console_errors={self.console_errors}"
            )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                if self._pw is not None:
                    self._pw.stop()


class AppUI:
    """Resilient, strictly opaque-box DOM interaction layer for Südwest-Touren."""

    def __init__(self, page: Page):
        self.page = page

    def open_url(self, url: str, wait_cards: bool = True) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if wait_cards:
            self.wait_for_cards()

    def wait_for_cards(self, timeout_ms: int = 12000) -> None:
        self.page.wait_for_function(
            """() => {
                const cards = document.querySelectorAll('#list .card, .tour-card, [data-tour-id], main article');
                return cards.length > 0;
            }""",
            timeout=timeout_ms,
        )

    def card_locator(self):
        for sel in ("#list .card", ".tour-card", "[data-tour-id]", "main article"):
            loc = self.page.locator(sel)
            if loc.count() > 0:
                return loc
        return self.page.locator("#list .card")

    def card_count(self) -> int:
        return self.card_locator().count()

    def visible_card_texts(self) -> list[str]:
        loc = self.card_locator()
        out = []
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                out.append(el.inner_text())
        return out

    def open_card_index(self, idx: int = 0) -> None:
        cards = self.card_locator()
        count = cards.count()
        if count <= idx:
            raise AssertionError(f"Requested card index {idx}, only {count} cards available")
        cards.nth(idx).scroll_into_view_if_needed()
        cards.nth(idx).click()
        self.wait_for_detail()

    def open_card_matching(self, pattern: str) -> bool:
        cards = self.card_locator()
        rx = re.compile(pattern, re.IGNORECASE)
        for i in range(cards.count()):
            el = cards.nth(i)
            if el.is_visible() and rx.search(el.inner_text()):
                el.scroll_into_view_if_needed()
                el.click()
                self.wait_for_detail()
                return True
        return False

    def wait_for_detail(self, timeout_ms: int = 8000) -> None:
        self.page.wait_for_function(
            """() => {
                const d = document.querySelector('#detail, [data-view="detail"], .detail-view');
                if (d && getComputedStyle(d).display !== 'none' && d.innerText.trim().length > 20) return true;
                const back = Array.from(document.querySelectorAll('button, a')).find(
                    b => /zurück|back|alle touren/i.test(b.innerText || b.getAttribute('aria-label') || '') && b.offsetParent !== null
                );
                return Boolean(back);
            }""",
            timeout=timeout_ms,
        )

    def go_back(self) -> None:
        btn = self.page.locator("#backBtn").first
        if btn.count() > 0 and btn.is_visible():
            btn.click()
        else:
            alt = self.page.locator("header button, header a, button").filter(
                has_text=re.compile(r"Zurück|Back|←|Alle Touren", re.IGNORECASE)
            ).first
            alt.click()
        self.page.wait_for_timeout(200)

    def detail_text(self) -> str:
        d = self.page.locator("#detail, [data-view='detail'], .detail-view").first
        if d.count() > 0 and d.is_visible():
            return d.inner_text()
        return self.page.locator("body").inner_text()

    def header_text(self) -> str:
        h = self.page.locator("header, .top").first
        return h.inner_text() if h.count() > 0 else ""

    def switch_base(self, base: str) -> None:
        target_rx = re.compile(r"Karlsruhe" if base == "karlsruhe" else r"Jugenheim", re.IGNORECASE)
        # Try explicit base-switcher buttons in header first
        candidates = self.page.locator("header button, .top button, [data-base], .base-switch button")
        for i in range(candidates.count()):
            b = candidates.nth(i)
            if not b.is_visible():
                continue
            txt = (b.inner_text() or "") + " " + (b.get_attribute("aria-label") or "")
            if target_rx.search(txt) and "modal" not in txt.lower():
                # Avoid clicking homeEditBtn unless it is the actual base toggle
                bid = b.get_attribute("id") or ""
                if bid == "homeEditBtn":
                    continue
                b.click()
                self.page.wait_for_timeout(250)
                return
        # Fallback: single toggle button showing current or next base
        toggle = self.page.locator("header button, .top button").filter(
            has_text=re.compile(r"Jugenheim|Karlsruhe", re.IGNORECASE)
        ).first
        if toggle.count() > 0 and toggle.is_visible():
            toggle.click()
            self.page.wait_for_timeout(250)
            return
        raise AssertionError(f"Base switcher control for {base!r} not found in header")

    def toggle_lang(self) -> None:
        for sel in ("#langBtn", "header button, .top button"):
            loc = self.page.locator(sel)
            if sel == "#langBtn" and loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                self.page.wait_for_timeout(200)
                return
        btn = self.page.locator("button").filter(
            has_text=re.compile(r"^(?:🇩🇪\s*)?DE$|^(?:🇬🇧\s*)?EN$", re.IGNORECASE)
        ).first
        btn.click()
        self.page.wait_for_timeout(200)

    def lang_button_text(self) -> str:
        btn = self.page.locator("#langBtn").first
        if btn.count() > 0 and btn.is_visible():
            return btn.inner_text().strip()
        btn = self.page.locator("button").filter(
            has_text=re.compile(r"^(?:🇩🇪\s*)?DE$|^(?:🇬🇧\s*)?EN$", re.IGNORECASE)
        ).first
        return btn.inner_text().strip()

    def toggle_theme(self) -> None:
        btn = self.page.locator("#themeBtn").first
        if btn.count() > 0 and btn.is_visible():
            btn.click()
            self.page.wait_for_timeout(200)
            return
        btn = self.page.locator("button").filter(
            has_text=re.compile(r"Dark|Hell|Dunkel|Theme|🌙|☀️", re.IGNORECASE)
        ).first
        if btn.count() > 0:
            btn.click()
            self.page.wait_for_timeout(200)
            return
        btn = self.page.locator("button[aria-label*='theme' i], button[aria-label*='dark' i], button[aria-label*='modus' i]").first
        btn.click()
        self.page.wait_for_timeout(200)

    def select_day(self, day: str) -> None:
        patterns = {
            "wd": r"Mo[–-]Fr|Werktag|Weekday",
            "sa": r"^Sa(?:mstag)?$|^Sat(?:urday)?$",
            "so": r"^So(?:nntag)?$|^Sun(?:day)?$",
        }
        rx = re.compile(patterns[day], re.IGNORECASE)
        btns = self.page.locator("button, [role='tab']")
        for i in range(btns.count()):
            b = btns.nth(i)
            if b.is_visible() and rx.search(b.inner_text().strip()):
                b.click()
                self.page.wait_for_timeout(200)
                return
        raise AssertionError(f"Weekday selector button for {day!r} not found")

    def click_filter(self, pattern: str) -> None:
        rx = re.compile(pattern, re.IGNORECASE)
        btns = self.page.locator("button, .pill, [role='button']")
        for i in range(btns.count()):
            b = btns.nth(i)
            if b.is_visible() and rx.search(b.inner_text().strip()):
                b.click()
                self.page.wait_for_timeout(200)
                return
        raise AssertionError(f"Filter button matching {pattern!r} not found")

    def fill_search(self, query: str) -> None:
        inp = self.page.locator("#q, input[type='search'], input[placeholder*='Suche' i], input[placeholder*='Search' i]").first
        inp.fill(query)
        self.page.wait_for_timeout(200)

    def set_feasible_time(self, hhmm: str) -> None:
        # Click the 'Heute noch machbar' pill if not already active / input hidden
        time_inp = self.page.locator("input[type='time'], #abfTime, #feasibleTime, input[aria-label*='Uhrzeit' i]").first
        if time_inp.count() == 0 or not time_inp.is_visible():
            self.click_filter(r"Heute noch machbar|Feasible today|Machbar ab")
            self.page.wait_for_timeout(150)
        time_inp = self.page.locator("input[type='time'], #abfTime, #feasibleTime, input[aria-label*='Uhrzeit' i]").first
        if time_inp.count() > 0 and time_inp.is_visible():
            time_inp.fill(hhmm)
            time_inp.dispatch_event("change")
            self.page.wait_for_timeout(200)

    def open_home_modal(self) -> None:
        btn = self.page.locator("#homeEditBtn").first
        if btn.count() > 0 and btn.is_visible():
            btn.click()
        else:
            btn = self.page.locator("header button, .top button").filter(
                has_text=re.compile(r"Heimat|Home|Standort|Location|Startort|✏️|📍", re.IGNORECASE)
            ).first
            btn.click()
        self.page.wait_for_timeout(200)

    def home_modal_locator(self):
        return self.page.locator("#homeModal, [role='dialog'], .modal").first

    def close_home_modal(self) -> None:
        modal = self.home_modal_locator()
        close_btn = modal.locator("button").filter(
            has_text=re.compile(r"Schließen|Close|×|✕|Abbrechen|Cancel", re.IGNORECASE)
        ).first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click()
        else:
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(200)

    def detail_links(self) -> dict[str, list[str]]:
        links = self.page.locator("a[href]")
        out: dict[str, list[str]] = {"db": [], "mvv": [], "gmaps": [], "all": []}
        for i in range(links.count()):
            el = links.nth(i)
            href = el.get_attribute("href") or ""
            if not href:
                continue
            out["all"].append(href)
            low = href.lower()
            if "bahn.de" in low:
                out["db"].append(href)
            elif any(k in low for k in ("mvv-muenchen.de", "hafas.de", "vrn.de", "muenchennavigator://")):
                out["mvv"].append(href)
            elif "google.com/maps" in low:
                out["gmaps"].append(href)
        return out

    def download_gpx(self) -> tuple[str, str]:
        btn = self.page.locator("button, a").filter(has_text=re.compile(r"GPX", re.IGNORECASE)).first
        if btn.count() == 0:
            raise AssertionError("GPX export button not found")
        with self.page.expect_download(timeout=10000) as dl_info:
            btn.click()
        dl = dl_info.value
        path = dl.path()
        content = Path(path).read_text(encoding="utf-8")
        return dl.suggested_filename, content

    def scroll_overflow_ok(self) -> tuple[int, int]:
        return self.page.evaluate(
            "() => [document.documentElement.scrollWidth, window.innerWidth]"
        )

    def computed_body_colors(self) -> dict[str, str]:
        return self.page.evaluate(
            """() => {
                const s = getComputedStyle(document.body);
                return {
                    bg: s.backgroundColor,
                    color: s.color,
                    theme: document.documentElement.getAttribute('data-theme') || ''
                };
            }"""
        )

    def map_path_count(self) -> int:
        return self.page.locator(".leaflet-overlay-pane path").count()

    def map_path_styles(self) -> list[dict[str, str]]:
        return self.page.evaluate(
            """() => Array.from(document.querySelectorAll('.leaflet-overlay-pane path')).map(p => ({
                stroke: p.getAttribute('stroke') || getComputedStyle(p).stroke || '',
                dash: p.getAttribute('stroke-dasharray') || getComputedStyle(p).strokeDasharray || '',
                width: p.getAttribute('stroke-width') || getComputedStyle(p).strokeWidth || ''
            }))"""
        )
