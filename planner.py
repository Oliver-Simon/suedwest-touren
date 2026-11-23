#!/usr/bin/env python3
"""M3 Dual-Base Deutschlandticket Timetable Engine for Südwest-Touren.

Computes offline timetables for all 144 tours in `data/tours_catalog_full.json`
from both home bases (`jugenheim` and `karlsruhe`) across `wd` (`2026-11-24`),
`sa` (`2026-11-28`), and `so` (`2026-11-29`).

Primary engine: Transitous / MOTIS 2 (`https://api.transitous.org/api/v1/plan`)
Fallback / cross-check engine: VRN-EFA rapidJSON (`https://www.vrn.de/mngvrn/XML_TRIP_REQUEST2`)
All responses are cached persistently in `cache/routing/`.

Outputs:
- `data/data.json`
- `data/routing_notes.json`
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3.util.connection
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force IPv4 so api.transitous.org never stalls on IPv6 SYN timeouts.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from planer.slim import dedupe_and_sort_alts, slim_data_dict
from planer.validate import (
    ALLOWED_RIDE_MODES,
    FORBIDDEN_LINE_RE,
    FORBIDDEN_MODES,
    FORBIDDEN_OP_RE,
    compute_hike_stay,
    compute_transfers,
    decode_polyline,
    haversine_m,
    in_sw_bounds,
    is_forbidden_fernverkehr,
    is_major_hub,
    minutes_diff,
    parse_hhmm,
    seg_is_ruftaxi_text,
    seg_is_seasonal_text,
    validate_dataset,
)

DATA_DIR = ROOT_DIR / "data"
CATALOG_PATH = DATA_DIR / "tours_catalog_full.json"
OUT_DATA_PATH = DATA_DIR / "data.json"
OUT_NOTES_PATH = DATA_DIR / "routing_notes.json"
CACHE_DIR = ROOT_DIR / "cache" / "routing"
STOPS_LOOKUP_CACHE_DIR = ROOT_DIR / "cache" / "stops_lookup"

USER_AGENT = "suedwest-touren-planner/1.0 (oliver.simon@stud.h-da.de)"
BERLIN_TZ = ZoneInfo("Europe/Berlin")

DAYS = {
    "wd": "2026-11-24",
    "sa": "2026-11-28",
    "so": "2026-11-29",
}

BASES = {
    "jugenheim": {
        "name": "Jugenheim, Bickenbacher Straße 24g",
        "stopName": "Jugenheim Bickenbacher Straße, Seeheim-Jugenheim",
        "lat": 49.753891,
        "lon": 8.629438,
        "dhid": "de:06432:24355",
        "vrnId": "124355",
        "rmvId": "3024355",
        "remoteId": "A=1@O=Jugenheim Bickenbacher Straße, Seeheim-Jugenheim@X=8629438@Y=49753891@U=80@L=124355@",
        "transitousId": "de-DELFI_de:06432:24355:2:2",
        "nearestRail": {
            "name": "Bickenbach (Bergstr)",
            "ibnr": "8000956",
            "lat": 49.757577,
            "lon": 8.601538,
        },
    },
    "karlsruhe": {
        "name": "Karlsruhe Hbf",
        "stopName": "Karlsruhe Hbf",
        "lat": 48.993530,
        "lon": 8.401939,
        "dhid": "de:08212:89",
        "ibnr": "8000191",
        "remoteId": "A=1@O=Karlsruhe Hbf@X=8402181@Y=48993512@U=80@L=8000191@",
        "transitousId": "de-DELFI_de:08212:90_G",
    },
}

TRANSITOUS_MODES = "REGIONAL_RAIL,REGIONAL_FAST_RAIL,SUBURBAN,METRO,TRAM,BUS,FERRY,ODM"
DHID_RE = re.compile(r"(de:\d+:\d+)")

WARNING_RUFTAXI = {
    "type": "ruftaxi",
    "text": "Ruftaxi / Bedarfsverkehr – Voranmeldung mindestens 60 Min. vor Abfahrt erforderlich.",
}
WARNING_SEASONAL = {
    "type": "seasonal",
    "text": "Saison-/Ausflugslinie – verkehrt i. d. R. Mai bis Oktober bzw. an Wochenenden.",
}
WARNING_HUB_TIGHT = {
    "type": "hub_tight",
    "text": "Knapper Umstieg am Hauptbahnhof (4 Min.) – bitte zügig zum Anschlussgleis wechseln.",
}


# ---------------------------------------------------------------------------
# Polyline Precision 7 -> 5 & DP Compaction
# ---------------------------------------------------------------------------


def encode_poly5(coords: list[tuple[float, float]]) -> str:
    if not coords:
        return ""
    out: list[str] = []
    prev_lat = 0
    prev_lon = 0
    for lat, lon in coords:
        ilat = round(lat * 1e5)
        ilon = round(lon * 1e5)
        for v in (ilat - prev_lat, ilon - prev_lon):
            sv = ~(v << 1) if v < 0 else (v << 1)
            while sv >= 0x20:
                out.append(chr((0x20 | (sv & 0x1F)) + 63))
                sv >>= 5
            out.append(chr(sv + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def _perp_dist_2d(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(pt[0] - (a[0] + t * dx), pt[1] - (a[1] + t * dy))


def simplify_coords(coords: list[tuple[float, float]], eps_m: float = 15.0) -> list[tuple[float, float]]:
    if len(coords) <= 2:
        return coords
    lat0 = coords[0][0]
    cos_lat = math.cos(math.radians(lat0))
    mx = 111320.0 * cos_lat
    my = 111320.0
    pts_m = [((c[1] - coords[0][1]) * mx, (c[0] - lat0) * my) for c in coords]
    stack = [(0, len(pts_m) - 1)]
    keep = {0, len(pts_m) - 1}
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 <= 1:
            continue
        max_d = -1.0
        max_idx = -1
        a, b = pts_m[i0], pts_m[i1]
        for idx in range(i0 + 1, i1):
            d = _perp_dist_2d(pts_m[idx], a, b)
            if d > max_d:
                max_d = d
                max_idx = idx
        if max_d > eps_m and max_idx != -1:
            keep.add(max_idx)
            stack.append((i0, max_idx))
            stack.append((max_idx, i1))
    return [coords[i] for i in sorted(keep)]


_GEOM_CACHE: dict[str, str] = {}
_GEOM_LOCK = threading.Lock()


def convert_geom_to_poly5(points_str: str, precision: int, fallback_coords: list[tuple[float, float]]) -> str:
    if points_str:
        with _GEOM_LOCK:
            cached = _GEOM_CACHE.get(points_str)
        if cached is not None:
            return cached

    coords: list[tuple[float, float]] = []
    if points_str:
        try:
            coords = decode_polyline(points_str, precision=precision)
        except Exception:
            coords = []
    coords = [c for c in coords if in_sw_bounds(c[0], c[1])]
    if len(coords) < 2:
        coords = [c for c in fallback_coords if in_sw_bounds(c[0], c[1])]
    if len(coords) > 250:
        step = max(1, len(coords) // 200)
        coords = coords[::step] + ([coords[-1]] if coords[-1] != coords[::step][-1] else [])
    if len(coords) > 60:
        coords = simplify_coords(coords, eps_m=18.0)
    res = encode_poly5(coords)
    if points_str:
        with _GEOM_LOCK:
            _GEOM_CACHE[points_str] = res
    return res


# ---------------------------------------------------------------------------
# Time & Stop Helpers
# ---------------------------------------------------------------------------


def iso_to_berlin(iso_str: str) -> tuple[str, str, dt.datetime]:
    """Convert UTC/ISO timestamp to ('HH:MM', 'YYYY-MM-DDTHH:MM:SS+01:00', dt_berlin)."""
    d = dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(BERLIN_TZ)
    return d.strftime("%H:%M"), d.isoformat(timespec="seconds"), d


def dhid_base(st: dict[str, Any] | None) -> str:
    if not st:
        return ""
    d = str(st.get("dhid") or "")
    m = DHID_RE.search(d)
    if m:
        return m.group(1)
    tid = str(st.get("transitousId") or "")
    m2 = DHID_RE.search(tid)
    if m2:
        return m2.group(1)
    return d or str(st.get("name") or "")


def is_jugenheim_stop(st: dict[str, Any] | None) -> bool:
    if not st:
        return False
    b = dhid_base(st)
    if b in ("de:06432:24355", "de:06432:1058"):
        return True
    nm = str(st.get("name") or "")
    if "Bickenbach" in nm or "Jugenheim" in nm:
        return True
    if st.get("lat") is not None and st.get("lon") is not None:
        return haversine_m(float(st["lat"]), float(st["lon"]), 49.753891, 8.629438) <= 800.0
    return False


def is_karlsruhe_stop(st: dict[str, Any] | None) -> bool:
    if not st:
        return False
    b = dhid_base(st)
    if b in ("de:08212:89", "de:08212:90"):
        return True
    nm = str(st.get("name") or "")
    if "Karlsruhe Hbf" in nm or "Karlsruhe Hauptbahnhof" in nm:
        return True
    if st.get("lat") is not None and st.get("lon") is not None:
        return haversine_m(float(st["lat"]), float(st["lon"]), 48.993530, 8.401939) <= 600.0
    return False


def extract_platform(raw_obj: dict[str, Any]) -> str | None:
    """Extract platform/track/quay from Transitous or VRN stop dict."""
    for k in ("track", "scheduledTrack", "platform"):
        v = raw_obj.get(k)
        if v and str(v).strip():
            return str(v).strip()
    props = raw_obj.get("properties") or {}
    if props.get("platform"):
        return str(props["platform"]).strip()
    sid = str(raw_obj.get("stopId") or raw_obj.get("id") or "")
    # e.g. de-DELFI_de:06432:24355:2:2 -> last token '2'
    parts = sid.split(":")
    if len(parts) >= 5:
        last = parts[-1].strip()
        if last and len(last) <= 6 and not last.endswith("_G"):
            return last
    return None


def classify_ride_mode(raw_mode: str, line_str: str, op_str: str, vrn_class: int | None = None) -> str:
    rm = (raw_mode or "").upper()
    ln = (line_str or "").strip().upper()
    op = (op_str or "").lower()
    if rm == "FERRY" or vrn_class == 9:
        return "ferry"
    if rm in ("BUS", "ODM") or vrn_class in (5, 6, 10):
        return "bus"
    if rm == "TRAM" or vrn_class == 4:
        if re.match(r"^S\d+\b", ln) and ("avg" in op or "kvv" in op or "albtal" in op):
            return "stadtbahn"
        return "tram"
    if rm in ("SUBURBAN", "METRO") or vrn_class == 3 or re.match(r"^S\d+\b", ln):
        if "avg" in op or "kvv" in op or "albtal" in op:
            return "stadtbahn"
        return "sbahn"
    if ln.startswith("RE") or ln.startswith("MEX") or ln.startswith("FEX") or ln.startswith("IRE"):
        return "re"
    if ln.startswith("RB") or vrn_class == 13:
        return "rb"
    if rm in ("REGIONAL_RAIL", "REGIONAL_FAST_RAIL", "RAIL"):
        return "re" if "RE" in ln else "rb"
    return "bus"


def clean_line_label(mode: str, short_name: str, display_name: str) -> str:
    raw = (short_name or display_name or "").strip()
    if not raw:
        return mode.upper()
    if mode == "bus":
        if not re.match(r"^(?:bus|ruftaxi|rufbus|alt|ast|flex|sev|air|x)\b", raw, re.IGNORECASE):
            return f"Bus {raw}"
        return raw
    if mode == "tram":
        if re.match(r"^\d+$", raw):
            return f"Tram {raw}"
        return raw
    return raw


def decorate_warnings(seg: dict[str, Any], is_ruftaxi_stop: bool = False, vrn_class: int | None = None) -> None:
    if seg.get("kind") != "ride":
        return
    warns: list[dict[str, str]] = list(seg.get("warnings") or [])
    existing_types = {w.get("type") for w in warns}

    if vrn_class == 10 or seg_is_ruftaxi_text(seg):
        if "ruftaxi" not in existing_types:
            warns.append(dict(WARNING_RUFTAXI))
            existing_types.add("ruftaxi")
    if seg_is_seasonal_text(seg):
        if "seasonal" not in existing_types:
            warns.append(dict(WARNING_SEASONAL))
            existing_types.add("seasonal")

    if warns:
        seg["warnings"] = warns


# ---------------------------------------------------------------------------
# Dual-Engine Cached Router (Transitous + VRN-EFA)
# ---------------------------------------------------------------------------


class DualEngineRouter:
    def __init__(self, cache_dir: Path, stop_meta_by_dhid: dict[str, dict[str, Any]]) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stop_meta_by_dhid = stop_meta_by_dhid

        self.transitous_sess = self._make_session()
        self.vrn_sess = self._make_session()
        self._t_lock = threading.Lock()
        self._v_lock = threading.Lock()
        self._last_t_req = 0.0
        self._last_v_req = 0.0
        self.t_calls = 0
        self.v_calls = 0

        self.polys: list[str] = [""]
        self.poly_idx: dict[str, int] = {"": 0}
        self._poly_lock = threading.Lock()

    @staticmethod
    def _make_session() -> requests.Session:
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT})
        retry = Retry(
            total=3,
            backoff_factor=1.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=retry)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    def register_poly(self, p5: str) -> int:
        with self._poly_lock:
            if p5 not in self.poly_idx:
                self.poly_idx[p5] = len(self.polys)
                self.polys.append(p5)
            return self.poly_idx[p5]

    def _rate_limit_transitous(self) -> None:
        with self._t_lock:
            now = time.monotonic()
            wait = self._last_t_req + 1.02 - now
            self._last_t_req = max(now, self._last_t_req + 1.02)
            self.t_calls += 1
        if wait > 0:
            time.sleep(wait)

    def _rate_limit_vrn(self) -> None:
        with self._v_lock:
            now = time.monotonic()
            wait = self._last_v_req + 1.02 - now
            self._last_v_req = max(now, self._last_v_req + 1.02)
            self.v_calls += 1
        if wait > 0:
            time.sleep(wait)

    def _enrich_stop(
        self,
        name: str,
        lat: float,
        lon: float,
        raw_id: str | None,
        platform: str | None,
        ref_stop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        m = DHID_RE.search(str(raw_id or ""))
        dhid = m.group(1) if m else (ref_stop.get("dhid") if ref_stop else None)
        cat_st = self.stop_meta_by_dhid.get(dhid or "") or ref_stop or {}
        ibnr = cat_st.get("ibnr")
        remote_id = cat_st.get("remoteId")
        if not remote_id and dhid:
            x = round(lon * 1e6)
            y = round(lat * 1e6)
            lid = ibnr or cat_st.get("stationId") or dhid
            remote_id = f"A=1@O={name}@X={x}@Y={y}@U=80@L={lid}@"
        return {
            "name": name,
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6),
            "dhid": dhid,
            "ibnr": ibnr,
            "remoteId": remote_id,
            "platform": platform or "1",
        }

    def find_nearest_stop(
        self,
        lat: float,
        lon: float,
        max_radius_m: float = 2500.0,
    ) -> tuple[dict[str, Any], int] | None:
        """Find nearest real ÖPNV stop within max_radius_m via Transitous /api/v1/map/stops (cached)."""
        STOPS_LOOKUP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = f"transitous_r2500_{round(lat, 4):.4f}_{round(lon, 4):.4f}.json"
        cpath = STOPS_LOOKUP_CACHE_DIR / key
        stops_data: list[dict[str, Any]] = []
        if cpath.exists():
            try:
                stops_data = json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                stops_data = []
        else:
            self._rate_limit_transitous()
            try:
                r = self.transitous_sess.get(
                    "https://api.transitous.org/api/v1/map/stops",
                    params={
                        "min": f"{lat - 0.025:.6f},{lon - 0.036:.6f}",
                        "max": f"{lat + 0.025:.6f},{lon + 0.036:.6f}",
                    },
                    timeout=25,
                )
                r.raise_for_status()
                raw = r.json()
                stops_data = raw if isinstance(raw, list) else []
            except Exception:
                stops_data = []
            try:
                fd, tmp = tempfile.mkstemp(dir=str(STOPS_LOOKUP_CACHE_DIR), suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(stops_data, fh, ensure_ascii=False)
                os.replace(tmp, cpath)
            except Exception:
                pass

        best: tuple[float, dict[str, Any]] | None = None
        for s in stops_data:
            sid = str(s.get("stopId") or s.get("id") or "")
            if not sid or sid.startswith("de-amarillo-"):
                continue
            slat, slon = s.get("lat"), s.get("lon")
            if slat is None or slon is None:
                continue
            m_dhid = DHID_RE.search(sid)
            if not m_dhid:
                continue
            if m_dhid.group(1) in {"de:07337:5512", "de:07337:4692"}:
                continue
            dist = haversine_m(lat, lon, float(slat), float(slon))
            if dist > max_radius_m:
                continue
            if best is None or dist < best[0]:
                best = (
                    dist,
                    {
                        "name": str(s.get("name") or "").strip(),
                        "lat": round(float(slat), 6),
                        "lon": round(float(slon), 6),
                        "dhid": m_dhid.group(1),
                        "transitousId": sid,
                        "ibnr": None,
                        "remoteId": None,
                    },
                )
        if best is None:
            return None
        return best[1], int(round(best[0]))

    def query_transitous_raw(
        self,
        from_place: str,
        to_place: str,
        time_utc: str,
        search_window: int = 57600,
        use_coords_allowance: bool = False,
    ) -> list[dict[str, Any]]:
        key_raw = f"transitous_v2|{from_place}|{to_place}|{time_utc}|{search_window}|{int(use_coords_allowance)}"
        h = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:24]
        cpath = self.cache_dir / f"t_{h}.json"
        if cpath.exists():
            try:
                return json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Also accept cached v2 entry with standard search_window (43200 / 57600) if already downloaded
        for alt_sw in (57600, 43200, 36000):
            if alt_sw == search_window:
                continue
            alt_key = f"transitous_v2|{from_place}|{to_place}|{time_utc}|{alt_sw}|{int(use_coords_allowance)}"
            alt_h = hashlib.sha256(alt_key.encode("utf-8")).hexdigest()[:24]
            alt_path = self.cache_dir / f"t_{alt_h}.json"
            if alt_path.exists():
                try:
                    return json.loads(alt_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

        params: dict[str, Any] = {
            "fromPlace": from_place,
            "toPlace": to_place,
            "time": time_utc,
            "transitModes": TRANSITOUS_MODES,
            "minTransferTime": 4,
            "searchWindow": search_window,
            "numItineraries": 8,
            "detailedTransfers": "false",
        }
        if use_coords_allowance:
            params["maxPreTransitTime"] = 1500
            params["maxPostTransitTime"] = 1500

        self._rate_limit_transitous()
        try:
            r = self.transitous_sess.get("https://api.transitous.org/api/v1/plan", params=params, timeout=25)
            r.raise_for_status()
            itins = r.json().get("itineraries") or []
            for it in itins:
                for leg in it.get("legs") or []:
                    leg.pop("steps", None)
                    leg.pop("alternatives", None)
        except Exception:
            itins = []

        fd, tmp = tempfile.mkstemp(dir=str(self.cache_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(itins, fh, ensure_ascii=False)
        os.replace(tmp, cpath)
        return itins

    def query_vrn_raw(
        self,
        orig_dhid: str,
        dest_dhid: str,
        date_ymd: str,
        hhmm: str,
    ) -> list[dict[str, Any]]:
        key_raw = f"vrn_v1|{orig_dhid}|{dest_dhid}|{date_ymd}|{hhmm}"
        h = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:24]
        cpath = self.cache_dir / f"v_{h}.json"
        if cpath.exists():
            try:
                return json.loads(cpath.read_text(encoding="utf-8"))
            except Exception:
                pass

        params = {
            "outputFormat": "rapidJSON",
            "coordOutputFormat": "WGS84[dd.ddddd]",
            "type_origin": "any",
            "name_origin": orig_dhid,
            "type_destination": "any",
            "name_destination": dest_dhid,
            "itdDate": date_ymd.replace("-", ""),
            "itdTime": hhmm.replace(":", ""),
            "itdTripDateTimeDepArr": "dep",
            "exclMOT_0": "on",
            "calcNumberOfTrips": 12,
        }
        self._rate_limit_vrn()
        try:
            r = self.vrn_sess.get("https://www.vrn.de/mngvrn/XML_TRIP_REQUEST2", params=params, timeout=20)
            r.raise_for_status()
            journeys = r.json().get("journeys") or []
        except Exception:
            journeys = []

        fd, tmp = tempfile.mkstemp(dir=str(self.cache_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(journeys, fh, ensure_ascii=False)
        os.replace(tmp, cpath)
        return journeys

    def parse_transitous_itinerary(
        self,
        itin: dict[str, Any],
        orig_stop: dict[str, Any],
        dest_stop: dict[str, Any],
        day_date: str,
    ) -> list[dict[str, Any]] | None:
        raw_legs = itin.get("legs") or []
        if not raw_legs:
            return None

        segs: list[dict[str, Any]] = []
        for idx, leg in enumerate(raw_legs):
            raw_mode = str(leg.get("mode") or "").upper()
            fr_raw = leg.get("from") or {}
            to_raw = leg.get("to") or {}
            st_iso = leg.get("startTime") or leg.get("scheduledStartTime")
            et_iso = leg.get("endTime") or leg.get("scheduledEndTime")
            if not st_iso or not et_iso:
                return None

            dep_hm, dep_berlin, dep_dt = iso_to_berlin(st_iso)
            arr_hm, arr_berlin, arr_dt = iso_to_berlin(et_iso)
            # Filter out itineraries that start on a completely different date (unless post-midnight return < 01:30)
            if idx == 0:
                if dep_berlin[:10] != day_date:
                    return None

            fr_name = fr_raw.get("name") or ""
            to_name = to_raw.get("name") or ""
            if fr_name in ("START", "Origin", ""):
                fr_name = orig_stop["name"] if idx == 0 else (segs[-1]["to"]["name"] if segs else orig_stop["name"])
            if to_name in ("END", "Destination", ""):
                to_name = dest_stop["name"] if idx == len(raw_legs) - 1 else dest_stop["name"]

            fr_lat = float(fr_raw.get("lat") if fr_raw.get("lat") is not None else orig_stop["lat"])
            fr_lon = float(fr_raw.get("lon") if fr_raw.get("lon") is not None else orig_stop["lon"])
            to_lat = float(to_raw.get("lat") if to_raw.get("lat") is not None else dest_stop["lat"])
            to_lon = float(to_raw.get("lon") if to_raw.get("lon") is not None else dest_stop["lon"])

            ref_from = orig_stop if idx == 0 else None
            ref_to = dest_stop if idx == len(raw_legs) - 1 else None
            frm = self._enrich_stop(fr_name, fr_lat, fr_lon, fr_raw.get("stopId"), extract_platform(fr_raw), ref_from)
            to = self._enrich_stop(to_name, to_lat, to_lon, to_raw.get("stopId"), extract_platform(to_raw), ref_to)

            geom = leg.get("legGeometry") or {}
            p5 = convert_geom_to_poly5(
                geom.get("points") or "",
                int(geom.get("precision") or 7),
                [(fr_lat, fr_lon), (to_lat, to_lon)],
            )
            p_idx = self.register_poly(p5)
            dist_m = int(round(float(leg.get("distance") or haversine_m(fr_lat, fr_lon, to_lat, to_lon))))

            if raw_mode == "WALK":
                # Skip trivial indoor transfers (< 40m or 0 min between two rides)
                if 0 < idx < len(raw_legs) - 1 and dist_m < 80:
                    continue
                if dist_m < 30 and len(raw_legs) > 1:
                    continue
                seg = {
                    "kind": "walk",
                    "mode": "foot",
                    "line": "Fußweg",
                    "lineName": "Fußweg",
                    "op": "",
                    "dep": dep_hm,
                    "arr": arr_hm,
                    "depIso": dep_berlin,
                    "arrIso": arr_berlin,
                    "from": frm,
                    "to": to,
                    "dest": to["name"],
                    "dist": dist_m,
                    "intermediateStops": [],
                    "warnings": [],
                    "legs": [{"m": "foot", "l": "", "d": dep_hm, "a": arr_hm, "dist": dist_m, "p": p_idx}],
                }
                segs.append(seg)
                continue

            short_n = str(leg.get("routeShortName") or "").strip()
            disp_n = str(leg.get("displayName") or leg.get("routeLongName") or short_n).strip()
            op_n = str(leg.get("agencyName") or "").strip()
            mode = classify_ride_mode(raw_mode, short_n or disp_n, op_n)
            line_lbl = clean_line_label(mode, short_n, disp_n)

            inter_stops: list[dict[str, Any]] = []
            for ist in leg.get("intermediateStops") or []:
                ist_arr = ist.get("arrival") or ist.get("scheduledArrival") or ist.get("departure")
                ist_dep = ist.get("departure") or ist.get("scheduledDeparture") or ist_arr
                a_hm = iso_to_berlin(ist_arr)[0] if ist_arr else dep_hm
                d_hm = iso_to_berlin(ist_dep)[0] if ist_dep else a_hm
                inter_stops.append(
                    {
                        "name": ist.get("name") or "",
                        "arr": a_hm,
                        "dep": d_hm,
                        "platform": extract_platform(ist) or "1",
                        "lat": round(float(ist.get("lat") or fr_lat), 6),
                        "lon": round(float(ist.get("lon") or fr_lon), 6),
                    }
                )

            seg = {
                "kind": "ride",
                "mode": mode,
                "line": line_lbl,
                "lineName": disp_n or line_lbl,
                "op": op_n,
                "dep": dep_hm,
                "arr": arr_hm,
                "depIso": dep_berlin,
                "arrIso": arr_berlin,
                "from": frm,
                "to": to,
                "dest": str(leg.get("headsign") or to["name"]).strip(),
                "dist": dist_m,
                "stops": len(inter_stops),
                "intermediateStops": inter_stops,
                "warnings": [],
                "legs": [{"m": mode, "l": line_lbl, "d": dep_hm, "a": arr_hm, "dist": dist_m, "p": p_idx}],
            }
            decorate_warnings(seg)
            if is_forbidden_fernverkehr(seg) is not None:
                return None
            segs.append(seg)

        return self._finalize_and_validate_itinerary(segs, orig_stop)

    def parse_vrn_journey(
        self,
        journey: dict[str, Any],
        orig_stop: dict[str, Any],
        dest_stop: dict[str, Any],
        day_date: str,
    ) -> list[dict[str, Any]] | None:
        raw_legs = journey.get("legs") or []
        if not raw_legs:
            return None

        segs: list[dict[str, Any]] = []
        for idx, leg in enumerate(raw_legs):
            trans = leg.get("transportation") or {}
            prod = trans.get("product") or {}
            p_class = prod.get("class")
            op_n = str((trans.get("operator") or {}).get("name") or "").strip()
            orig_raw = leg.get("origin") or {}
            dest_raw = leg.get("destination") or {}

            st_iso = orig_raw.get("departureTimePlanned") or orig_raw.get("departureTimeEstimated")
            et_iso = dest_raw.get("arrivalTimePlanned") or dest_raw.get("arrivalTimeEstimated")
            if not st_iso or not et_iso:
                return None

            dep_hm, dep_berlin, _ = iso_to_berlin(st_iso)
            arr_hm, arr_berlin, _ = iso_to_berlin(et_iso)
            if idx == 0 and dep_berlin[:10] != day_date:
                return None

            o_coord = orig_raw.get("coord") or [orig_stop["lat"], orig_stop["lon"]]
            d_coord = dest_raw.get("coord") or [dest_stop["lat"], dest_stop["lon"]]
            fr_lat, fr_lon = float(o_coord[0]), float(o_coord[1])
            to_lat, to_lon = float(d_coord[0]), float(d_coord[1])

            fr_name = orig_raw.get("name") or orig_stop["name"]
            to_name = dest_raw.get("name") or dest_stop["name"]

            ref_from = orig_stop if idx == 0 else None
            ref_to = dest_stop if idx == len(raw_legs) - 1 else None
            frm = self._enrich_stop(fr_name, fr_lat, fr_lon, orig_raw.get("id"), extract_platform(orig_raw), ref_from)
            to = self._enrich_stop(to_name, to_lat, to_lon, dest_raw.get("id"), extract_platform(dest_raw), ref_to)

            raw_coords = [
                (float(c[0]), float(c[1]))
                for c in (leg.get("coords") or [])
                if isinstance(c, list) and len(c) >= 2 and in_sw_bounds(float(c[0]), float(c[1]))
            ]
            if len(raw_coords) < 2:
                raw_coords = [(fr_lat, fr_lon), (to_lat, to_lon)]
            if len(raw_coords) > 60:
                raw_coords = simplify_coords(raw_coords, eps_m=18.0)
            p5 = encode_poly5(raw_coords)
            p_idx = self.register_poly(p5)
            dist_m = int(round(haversine_m(fr_lat, fr_lon, to_lat, to_lon)))

            # Footpath in VRN (class == 99 or 100 or no product class)
            if p_class in (None, 99, 100) and not trans.get("name"):
                if 0 < idx < len(raw_legs) - 1 and dist_m < 80:
                    continue
                if dist_m < 30 and len(raw_legs) > 1:
                    continue
                seg = {
                    "kind": "walk",
                    "mode": "foot",
                    "line": "Fußweg",
                    "lineName": "Fußweg",
                    "op": "",
                    "dep": dep_hm,
                    "arr": arr_hm,
                    "depIso": dep_berlin,
                    "arrIso": arr_berlin,
                    "from": frm,
                    "to": to,
                    "dest": to["name"],
                    "dist": dist_m,
                    "intermediateStops": [],
                    "warnings": [],
                    "legs": [{"m": "foot", "l": "", "d": dep_hm, "a": arr_hm, "dist": dist_m, "p": p_idx}],
                }
                segs.append(seg)
                continue

            if p_class == 0:
                return None

            t_name = str(trans.get("name") or "").strip()
            t_short = str(trans.get("disassembledName") or trans.get("number") or t_name).strip()
            mode = classify_ride_mode(str(prod.get("name") or ""), t_name or t_short, op_n, vrn_class=p_class)
            line_lbl = clean_line_label(mode, t_name if p_class == 10 else t_short, t_name)

            inter_stops: list[dict[str, Any]] = []
            stop_seq = leg.get("stopSequence") or []
            for ist in stop_seq[1:-1]:
                ist_arr = ist.get("arrivalTimePlanned") or ist.get("departureTimePlanned")
                ist_dep = ist.get("departureTimePlanned") or ist_arr
                a_hm = iso_to_berlin(ist_arr)[0] if ist_arr else dep_hm
                d_hm = iso_to_berlin(ist_dep)[0] if ist_dep else a_hm
                ic = ist.get("coord") or [fr_lat, fr_lon]
                inter_stops.append(
                    {
                        "name": ist.get("name") or "",
                        "arr": a_hm,
                        "dep": d_hm,
                        "platform": extract_platform(ist) or "1",
                        "lat": round(float(ic[0]), 6),
                        "lon": round(float(ic[1]), 6),
                    }
                )

            dest_disp = str((trans.get("destination") or {}).get("name") or to["name"]).strip()
            seg = {
                "kind": "ride",
                "mode": mode,
                "line": line_lbl,
                "lineName": t_name or line_lbl,
                "op": op_n,
                "dep": dep_hm,
                "arr": arr_hm,
                "depIso": dep_berlin,
                "arrIso": arr_berlin,
                "from": frm,
                "to": to,
                "dest": dest_disp,
                "dist": dist_m,
                "stops": len(inter_stops),
                "intermediateStops": inter_stops,
                "warnings": [],
                "legs": [{"m": mode, "l": line_lbl, "d": dep_hm, "a": arr_hm, "dist": dist_m, "p": p_idx}],
            }
            decorate_warnings(seg, vrn_class=p_class)
            if is_forbidden_fernverkehr(seg) is not None:
                return None
            segs.append(seg)

        return self._finalize_and_validate_itinerary(segs, orig_stop)

    def _finalize_and_validate_itinerary(
        self,
        segs: list[dict[str, Any]],
        orig_stop: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if not segs:
            return None
        rides = [s for s in segs if s.get("kind") == "ride"]
        if not rides:
            return None

        # Check origin proximity if orig_stop is one of our bases
        fr0 = segs[0].get("from") or {}
        if fr0.get("lat") is not None and orig_stop.get("lat") is not None:
            d0 = haversine_m(float(orig_stop["lat"]), float(orig_stop["lon"]), float(fr0["lat"]), float(fr0["lon"]))
            if d0 > 2400.0:
                return None

        # Validate each segment duration and coordinates
        for s in segs:
            if is_forbidden_fernverkehr(s) is not None:
                return None
            dur = minutes_diff(s["dep"], s["arr"], s.get("depIso"), s.get("arrIso"))
            if dur < 0 or dur > 720:
                return None
            for endp in (s.get("from"), s.get("to")):
                if not endp or not in_sw_bounds(float(endp["lat"]), float(endp["lon"])):
                    return None

        # Enforce transfer buffers >= 4 min on every ride->ride transition
        for prev_r, next_r, gap, hub in compute_transfers(segs):
            if gap < 4 or gap > 360:
                return None
            prev_r["transfer"] = gap
            if is_major_hub(hub) and gap < 5:
                warns = list(prev_r.get("warnings") or [])
                if not any(w.get("type") == "hub_tight" for w in warns):
                    warns.append(dict(WARNING_HUB_TIGHT))
                prev_r["warnings"] = warns

        return segs

    def get_od_itineraries(
        self,
        orig_stop: dict[str, Any],
        dest_stop: dict[str, Any],
        day_key: str,
        is_outbound: bool,
    ) -> list[list[dict[str, Any]]]:
        """Fetch, parse, filter, and deduplicate all valid D-Ticket itineraries for (orig -> dest, day_key)."""
        day_date = DAYS[day_key]
        p_key = hashlib.sha256(
            f"parsed_v1|{dhid_base(orig_stop)}|{dhid_base(dest_stop)}|{day_date}|{int(is_outbound)}".encode("utf-8")
        ).hexdigest()[:24]
        p_path = self.cache_dir / f"p_{p_key}.json"
        if p_path.exists():
            try:
                cached_segs = json.loads(p_path.read_text(encoding="utf-8"))
                for itin in cached_segs:
                    for seg in itin:
                        for sub in seg.get("legs") or []:
                            p5_str = sub.get("p5", "")
                            sub["p"] = self.register_poly(p5_str)
                return cached_segs
            except Exception:
                pass

        # Outbound searches from 06:00 Berlin (05:00 UTC); return searches from 09:00 Berlin (08:00 UTC)
        start_utc = f"{day_date}T05:00:00Z" if is_outbound else f"{day_date}T08:00:00Z"
        sw = 36000 if is_outbound else 46800

        fp = orig_stop.get("transitousId") or orig_stop.get("dhid")
        tp = dest_stop.get("transitousId") or dest_stop.get("dhid")
        use_coords = False
        if not orig_stop.get("transitousId"):
            fp = f"{orig_stop['lat']},{orig_stop['lon']}"
            use_coords = True
        if not dest_stop.get("transitousId"):
            tp = f"{dest_stop['lat']},{dest_stop['lon']}"
            use_coords = True

        # Only query VRN-EFA first if the stop is marked odmOnly (e.g. Ruftaxi-only stops)
        is_odm = bool(orig_stop.get("odmOnly") or dest_stop.get("odmOnly"))
        valid: list[list[dict[str, Any]]] = []

        if not is_odm:
            raw_t = self.query_transitous_raw(str(fp), str(tp), start_utc, search_window=sw, use_coords_allowance=use_coords)
            for it in raw_t:
                parsed = self.parse_transitous_itinerary(it, orig_stop, dest_stop, day_date)
                if parsed:
                    valid.append(parsed)

        # If Transitous yielded < 3 distinct departure times, also query coordinate fallback or VRN-EFA
        distinct_deps = {s[0]["dep"] for s in valid}
        if len(distinct_deps) < 3 and not use_coords and not is_odm:
            fp_c = f"{orig_stop['lat']},{orig_stop['lon']}"
            tp_c = f"{dest_stop['lat']},{dest_stop['lon']}"
            raw_tc = self.query_transitous_raw(fp_c, tp_c, start_utc, search_window=sw, use_coords_allowance=True)
            for it in raw_tc:
                parsed = self.parse_transitous_itinerary(it, orig_stop, dest_stop, day_date)
                if parsed:
                    valid.append(parsed)
            distinct_deps = {s[0]["dep"] for s in valid}

        if len(distinct_deps) == 0:
            o_dhid = dhid_base(orig_stop)
            d_dhid = dhid_base(dest_stop)
            vrn_times = ("07:30", "11:00") if is_outbound else ("14:00", "18:00")
            for vt in vrn_times:
                raw_v = self.query_vrn_raw(o_dhid, d_dhid, day_date, vt)
                for j in raw_v:
                    parsed = self.parse_vrn_journey(j, orig_stop, dest_stop, day_date)
                    if parsed:
                        valid.append(parsed)
                if len({s[0]["dep"] for s in valid}) >= 3:
                    break

        res = dedupe_and_sort_alts(valid, max_alts=16)
        # Store p5 string in each leg so parsed cache can reconstruct poly table instantaneously
        for itin in res:
            for seg in itin:
                for sub in seg.get("legs") or []:
                    idx_p = sub.get("p", 0)
                    sub["p5"] = self.polys[idx_p] if 0 <= idx_p < len(self.polys) else ""
        try:
            fd, tmp = tempfile.mkstemp(dir=str(self.cache_dir), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False)
            os.replace(tmp, p_path)
        except Exception:
            pass
        return res


# ---------------------------------------------------------------------------
# Tour Endpoint Resolution & Plan Pairing
# ---------------------------------------------------------------------------


def resolve_tour_endpoints(tour: dict[str, Any], base_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (stopA, stopB) where outbound is base -> stopA and return is stopB -> base."""
    tr = tour["transit"]
    ss = tr["startStop"]
    es = tr["endStop"]
    vias = tr.get("vias") or []
    kind = tr["kind"]

    if kind in ("gpx_linked", "curated") or (kind == "shortcut_pair" and (tour.get("hike") or {}).get("hasGpx")):
        return ss, es

    if is_jugenheim_stop(ss) and is_jugenheim_stop(es) and vias:
        valid_vias = [
            v for v in vias if (not is_karlsruhe_stop(v) if base_key == "karlsruhe" else not is_jugenheim_stop(v))
        ]
        if not valid_vias:
            valid_vias = vias
        return valid_vias[0], valid_vias[-1]

    if is_jugenheim_stop(ss) and not is_jugenheim_stop(es):
        if base_key == "karlsruhe" and is_karlsruhe_stop(es) and vias:
            return vias[0], vias[-1]
        return es, es

    if base_key == "karlsruhe" and is_karlsruhe_stop(ss):
        return es, es
    return ss, es


def arrival_before_0230(segs: list[dict[str, Any]]) -> bool:
    dep_m = parse_hhmm(segs[0].get("dep"))
    arr_m = parse_hhmm(segs[-1].get("arr"))
    if dep_m is None or arr_m is None:
        return False
    if arr_m < dep_m and arr_m > 150:
        return False
    return True


def build_plan_for_slot(
    tour: dict[str, Any],
    out_itins: list[list[dict[str, Any]]],
    ret_itins: list[list[dict[str, Any]]],
    approach_info: dict[str, int] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Match outbound and return itineraries satisfying hike synchronization & >= 3 windows."""
    wmin = int((tour.get("hike") or {}).get("walkMin") or 0)
    approach_min = int((approach_info or {}).get("approachMin") or 0)
    min_stay = max(wmin + approach_min + 30, 270 if wmin >= 240 else 0) if wmin > 0 else 45

    safe_returns = [r for r in ret_itins if arrival_before_0230(r)]
    if not out_itins or not safe_returns:
        return {"fallback": True, "fwd": None, "rev": None, "alts_fwd": [], "alts_rev": [], "lastSafeReturn": None}, (
            "Keine durchgehende Deutschlandticket-Verbindung (Hin- und Rückfahrt am selben Tag) im Fahrplan gefunden."
        )

    # Filter out_itins to those that have at least one valid return with stay >= min_stay (and <= 720 min)
    valid_outs: list[tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]] = []
    for f in out_itins:
        dep_f = parse_hhmm(f[0].get("dep")) or 0
        if dep_f < 300 or dep_f > 990:  # 05:00 .. 16:30
            continue
        compat: list[tuple[int, list[dict[str, Any]]]] = []
        for r in safe_returns:
            st = compute_hike_stay(f, r)
            if st is not None and min_stay <= st <= 780:
                # Ensure return actually departs after outbound arrives on the same day (not wrap-around yesterday)
                f_arr_iso = f[-1].get("arrIso")
                r_dep_iso = r[0].get("depIso")
                if f_arr_iso and r_dep_iso:
                    d_arr = dt.datetime.fromisoformat(f_arr_iso)
                    d_dep = dt.datetime.fromisoformat(r_dep_iso)
                    if d_dep < d_arr:
                        continue
                compat.append((st, r))
        if compat:
            valid_outs.append((f, compat))

    if not valid_outs:
        return {"fallback": True, "fwd": None, "rev": None, "alts_fwd": [], "alts_rev": [], "lastSafeReturn": None}, (
            f"Aufenthaltszeit vor Ort reicht am Stichtag nicht für Wanderdauer + Puffer ({min_stay} Min.)."
        )

    # Pick recommended default outbound `fwd` (~07:45..09:45 preferred, otherwise earliest valid)
    def score_out(item: tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]) -> tuple[int, int]:
        f = item[0]
        dep_m = parse_hhmm(f[0]["dep"]) or 0
        in_morning = 0 if 450 <= dep_m <= 585 else (1 if 390 <= dep_m <= 660 else 2)
        dur_f = minutes_diff(f[0]["dep"], f[-1]["arr"], f[0].get("depIso"), f[-1].get("arrIso"))
        return (in_morning, dur_f)

    best_out_item = min(valid_outs, key=score_out)
    main_fwd = copy.deepcopy(best_out_item[0])
    compat_for_main = best_out_item[1]

    # Pick recommended return `rev`: comfortable stay closest to min_stay + 15..90 min, shortest duration
    def score_ret(st_r: tuple[int, list[dict[str, Any]]]) -> tuple[int, int]:
        st, r = st_r
        ideal_extra = abs((st - min_stay) - 35)
        dur_r = minutes_diff(r[0]["dep"], r[-1]["arr"], r[0].get("depIso"), r[-1].get("arrIso"))
        return (ideal_extra // 15, dur_r)

    best_ret_st, best_ret = min(compat_for_main, key=score_ret)
    main_rev = copy.deepcopy(best_ret)

    # Last safe return for `main_fwd`: latest departure in compat_for_main that is >= main_rev[0].dep
    main_rev_dep = parse_hhmm(main_rev[0]["dep"]) or 0

    def adj_dep(r: list[dict[str, Any]]) -> int:
        dm = parse_hhmm(r[0]["dep"]) or 0
        return dm + (1440 if dm < 240 and main_rev_dep > 600 else 0)

    later_or_eq = [item for item in compat_for_main if adj_dep(item[1]) >= main_rev_dep]
    lsr_st, lsr_segs_raw = max(later_or_eq, key=lambda item: adj_dep(item[1])) if later_or_eq else (best_ret_st, main_rev)
    lsr_segs = copy.deepcopy(lsr_segs_raw)

    if wmin > 0:
        main_fwd[-1]["minStay"] = min_stay
        if approach_min > 0:
            main_fwd[-1]["stayNote"] = (
                f"Wanderung ca. {wmin} Min. + {approach_min} Min. Zustieg + 30 Min. Puffer "
                f"(Aufenthalt {best_ret_st} Min.)"
            )
        else:
            main_fwd[-1]["stayNote"] = f"Wanderung ca. {wmin} Min. + 30 Min. Puffer (Aufenthalt {best_ret_st} Min.)"

    # Attach approach-walk warnings if tour required extended >800m stop linking
    if approach_info:
        w_start = int(approach_info.get("walkToStartM") or 0)
        w_end = int(approach_info.get("walkFromEndM") or 0)
        w_start_min = max(1, int(round(w_start / 66.67))) if w_start > 0 else 0
        w_end_min = max(1, int(round(w_end / 66.67))) if w_end > 0 else 0

        def _add_warn(seg: dict[str, Any], wobj: dict[str, str]) -> None:
            ws = list(seg.get("warnings") or [])
            if not any(x.get("type") == wobj["type"] and x.get("text") == wobj["text"] for x in ws):
                ws.append(dict(wobj))
            seg["warnings"] = ws

        if w_start > 800:
            warn_s = {
                "type": "approach",
                "text": f"Zustieg ab Haltestelle ca. {w_start} m ({w_start_min} Min. Fußweg) zum Wanderstart.",
            }
            _add_warn(main_fwd[-1], warn_s)
        if w_end > 800:
            warn_e = {
                "type": "approach",
                "text": f"Zustieg ab Haltestelle ca. {w_end} m ({w_end_min} Min. Fußweg) vom Wanderende zur Haltestelle.",
            }
            _add_warn(main_rev[0], warn_e)
            _add_warn(lsr_segs[0], warn_e)
            if w_start <= 800:
                _add_warn(main_fwd[-1], warn_e)

    # Build alts_fwd (up to 8 diverse windows, sorted and deduplicated)
    all_valid_f = [copy.deepcopy(item[0]) for item in valid_outs]
    alts_fwd = dedupe_and_sort_alts(all_valid_f, main=main_fwd, max_alts=8)

    # Ensure every f in alts_fwd has at least one compatible return in alts_rev!
    needed_rets: list[list[dict[str, Any]]] = [copy.deepcopy(main_rev), copy.deepcopy(lsr_segs)]
    for af in alts_fwd:
        # Find best return for af
        best_r_for_af = None
        for r in safe_returns:
            st = compute_hike_stay(af, r)
            if st is not None and min_stay <= st <= 780:
                best_r_for_af = r
                break
        if best_r_for_af:
            needed_rets.append(copy.deepcopy(best_r_for_af))
    for _, r in compat_for_main:
        needed_rets.append(copy.deepcopy(r))

    alts_rev = dedupe_and_sort_alts(needed_rets, main=main_rev, max_alts=8)

    lsr_obj = {
        "dep": lsr_segs[0]["dep"],
        "arr": lsr_segs[-1]["arr"],
        "minStayAchieved": lsr_st,
        "isLastSafe": True,
        "segs": lsr_segs,
    }

    plan = {
        "fallback": False,
        "fwd": main_fwd,
        "rev": main_rev,
        "alts_fwd": alts_fwd,
        "alts_rev": alts_rev,
        "lastSafeReturn": lsr_obj,
    }
    note_reason = None
    distinct_deps = {a[0]["dep"] for a in alts_fwd if a}
    if len(distinct_deps) < 3:
        note_reason = (
            f"Eingeschränktes Fahrplanangebot: Nur {len(distinct_deps)} gültige D-Ticket-Abfahrtsfenster "
            f"mit >= {min_stay} Min. Aufenthalt verfügbar."
        )
    return plan, note_reason


# ---------------------------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------------------------


def main() -> int:
    t_start = time.monotonic()
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    tours: list[dict[str, Any]] = catalog["tours"]

    # Collect stop metadata lookup table by base DHID
    stop_meta_by_dhid: dict[str, dict[str, Any]] = {
        "de:06432:24355": dict(BASES["jugenheim"]),
        "de:08212:89": dict(BASES["karlsruhe"]),
        "de:08212:90": dict(BASES["karlsruhe"]),
    }
    for t in tours:
        tr = t.get("transit") or {}
        for st in [tr.get("startStop"), tr.get("endStop")] + list(tr.get("vias") or []):
            if isinstance(st, dict):
                d = dhid_base(st)
                if d and d not in stop_meta_by_dhid:
                    stop_meta_by_dhid[d] = st

    router = DualEngineRouter(CACHE_DIR, stop_meta_by_dhid)

    # 0. Auto-link previously unlinked Mapy GPX tours up to 2,500 m radius (without touching M1 catalog)
    tour_approach_map: dict[str, dict[str, int]] = {}
    auto_linked_cnt = 0
    for t in tours:
        tr = t["transit"]
        if not tr.get("unlinkedReason"):
            continue
        hike = t.get("hike") or {}
        sc = hike.get("startCoord")
        ec = hike.get("endCoord")
        if not sc or not ec:
            continue

        ss = tr.get("startStop")
        es = tr.get("endStop")
        w_start = tr.get("walkToStartM")
        w_end = tr.get("walkFromEndM")

        SEASONAL_ONLY_DHIDS = {"de:07337:5512", "de:07337:4692"}  # Modenbacher Hof, Edenkoben Waldhaus (summer-only)
        if ss is None or dhid_base(ss) in SEASONAL_ONLY_DHIDS:
            found_s = router.find_nearest_stop(float(sc[0]), float(sc[1]), max_radius_m=2500.0)
            if found_s:
                ss, w_start = found_s
        if es is None or dhid_base(es) in SEASONAL_ONLY_DHIDS:
            found_e = router.find_nearest_stop(float(ec[0]), float(ec[1]), max_radius_m=2500.0)
            if found_e:
                es, w_end = found_e

        if ss is not None and es is not None:
            tr["startStop"] = ss
            tr["endStop"] = es
            tr["walkToStartM"] = int(w_start or 0)
            tr["walkFromEndM"] = int(w_end or 0)
            tr["originalUnlinkedReason"] = tr["unlinkedReason"]
            tr["unlinkedReason"] = None
            tr["approachLinked"] = True
            approach_min = int(round((tr["walkToStartM"] + tr["walkFromEndM"]) / 66.67))
            tr["approachMin"] = approach_min
            tour_approach_map[t["id"]] = {
                "walkToStartM": tr["walkToStartM"],
                "walkFromEndM": tr["walkFromEndM"],
                "approachMin": approach_min,
            }
            for st in (ss, es):
                d = dhid_base(st)
                if d and d not in stop_meta_by_dhid:
                    stop_meta_by_dhid[d] = st
            auto_linked_cnt += 1

    if auto_linked_cnt:
        print(f"planner.py: Auto-linked {auto_linked_cnt} previously unlinked Mapy tours within 2,500 m", flush=True)

    # 1. Gather all unique (base_key, stop_dhid, day_key, is_outbound) tasks
    od_tasks: dict[tuple[str, str, str, bool], tuple[dict[str, Any], dict[str, Any], str, bool]] = {}
    for t in tours:
        tr = t["transit"]
        if tr.get("unlinkedReason"):
            continue
        for base_key, base_info in BASES.items():
            sA, sB = resolve_tour_endpoints(t, base_key)
            dA, dB = dhid_base(sA), dhid_base(sB)
            for day_key in ("wd", "sa", "so"):
                k_out = (base_key, dA, day_key, True)
                if k_out not in od_tasks:
                    od_tasks[k_out] = (base_info, sA, day_key, True)
                k_ret = (base_key, dB, day_key, False)
                if k_ret not in od_tasks:
                    od_tasks[k_ret] = (sB, base_info, day_key, False)

    print(f"planner.py: {len(tours)} tours | {len(od_tasks)} unique (base, stop, day, dir) OD queries", flush=True)

    od_results: dict[tuple[str, str, str, bool], list[list[dict[str, Any]]]] = {}
    task_items = list(od_tasks.items())
    done_cnt = 0

    def _run_od(item: tuple[tuple[str, str, str, bool], tuple[dict[str, Any], dict[str, Any], str, bool]]):
        key, (orig_s, dest_s, dk, is_out) = item
        res = router.get_od_itineraries(orig_s, dest_s, dk, is_out)
        return key, res

    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, res in ex.map(_run_od, task_items):
            od_results[key] = res
            done_cnt += 1
            if done_cnt % 50 == 0 or done_cnt == len(task_items):
                el = time.monotonic() - t_start
                print(
                    f"  [{done_cnt:4d}/{len(task_items)}] elapsed={el:.1f}s "
                    f"(net: transitous={router.t_calls}, vrn={router.v_calls})",
                    flush=True,
                )

    # 2. Assemble tour plans & routing notes
    out_tours: list[dict[str, Any]] = []
    routing_notes: list[dict[str, Any]] = []

    for t in tours:
        t_out = copy.deepcopy(t)
        tr = t_out["transit"]
        unlinked_reason = tr.get("unlinkedReason")
        wmin = int((t_out.get("hike") or {}).get("walkMin") or 0)

        plans: dict[str, dict[str, Any]] = {"jugenheim": {}, "karlsruhe": {}}
        unroutable: dict[str, dict[str, str]] = {"jugenheim": {}, "karlsruhe": {}}

        if unlinked_reason:
            reason_str = f"Kein ÖPNV-Halt im 800-m-Radius: {unlinked_reason}"
            for bk in ("jugenheim", "karlsruhe"):
                for dk in ("wd", "sa", "so"):
                    plans[bk][dk] = {
                        "fallback": True,
                        "reason": reason_str,
                        "fwd": None,
                        "rev": None,
                        "alts_fwd": [],
                        "alts_rev": [],
                        "lastSafeReturn": None,
                    }
                    unroutable[bk][dk] = reason_str
            routing_notes.append(
                {
                    "tour_id": t_out["id"],
                    "title": (t_out.get("title") or {}).get("de") or t_out["id"],
                    "status": "unlinked_gpx_over_800m",
                    "bases": ["jugenheim", "karlsruhe"],
                    "days": ["wd", "sa", "so"],
                    "reason": reason_str,
                }
            )
            t_out["durationMin"] = max(180, wmin + 120)
            t_out["plans"] = plans
            t_out["unroutable"] = unroutable
            out_tours.append(t_out)
            continue

        tour_durations: list[int] = []
        tour_slot_notes: list[tuple[str, str, str]] = []

        approach_info = tour_approach_map.get(t_out["id"])
        for bk in ("jugenheim", "karlsruhe"):
            sA, sB = resolve_tour_endpoints(t_out, bk)
            dA, dB = dhid_base(sA), dhid_base(sB)
            for dk in ("wd", "sa", "so"):
                out_itins = od_results.get((bk, dA, dk, True)) or []
                ret_itins = od_results.get((bk, dB, dk, False)) or []
                plan, note_rsn = build_plan_for_slot(t_out, out_itins, ret_itins, approach_info=approach_info)
                if plan.get("fallback"):
                    rsn = note_rsn or "Keine gültige Deutschlandticket-Verbindung an diesem Wochentag verfügbar."
                    plan["reason"] = rsn
                    unroutable[bk][dk] = rsn
                    tour_slot_notes.append((bk, dk, rsn))
                else:
                    if note_rsn:
                        unroutable[bk][dk] = note_rsn
                        tour_slot_notes.append((bk, dk, note_rsn))
                    f0 = plan["fwd"]
                    r0 = plan["rev"]
                    total_dur = minutes_diff(f0[0]["dep"], r0[-1]["arr"], f0[0].get("depIso"), r0[-1].get("arrIso"))
                    tour_durations.append(total_dur)
                plans[bk][dk] = plan

        if tour_slot_notes:
            routing_notes.append(
                {
                    "tour_id": t_out["id"],
                    "title": (t_out.get("title") or {}).get("de") or t_out["id"],
                    "status": "partial_or_unroutable",
                    "bases": sorted({x[0] for x in tour_slot_notes}),
                    "days": sorted({x[1] for x in tour_slot_notes}),
                    "reason": tour_slot_notes[0][2],
                }
            )

        t_out["durationMin"] = int(round(sum(tour_durations) / len(tour_durations))) if tour_durations else max(180, wmin + 120)
        t_out["plans"] = plans
        t_out["unroutable"] = unroutable
        out_tours.append(t_out)

    gen_ts = dt.datetime.now(BERLIN_TZ).isoformat(timespec="minutes")
    data_payload: dict[str, Any] = {
        "generated": gen_ts,
        "bases": {
            "jugenheim": {k: v for k, v in BASES["jugenheim"].items() if k != "transitousId"},
            "karlsruhe": {k: v for k, v in BASES["karlsruhe"].items() if k != "transitousId"},
        },
        "days": DAYS,
        "waypoints": catalog.get("waypoints") or [],
        "excludedGpx": catalog.get("excludedGpx") or [],
        "tours": out_tours,
        "freq": {},
        "polys": router.polys,
    }

    # 3. Compact with slim_data_dict
    slim_data_dict(data_payload, max_alts=8)

    notes_payload = {
        "generated": gen_ts,
        "days": DAYS,
        "totalTours": len(out_tours),
        "notedToursCount": len(routing_notes),
        "notes": routing_notes,
    }

    OUT_DATA_PATH.write_text(
        json.dumps(data_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    OUT_NOTES_PATH.write_text(
        json.dumps(notes_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 4. Validate immediately
    errors = validate_dataset(data_payload, notes_payload)
    print(
        f"planner.py finished in {time.monotonic() - t_start:.1f}s: "
        f"data.json={OUT_DATA_PATH.stat().st_size / 1024:.1f} KB, "
        f"polys={len(data_payload['polys'])}, "
        f"routing_notes={len(routing_notes)}, "
        f"validation_errors={len(errors)}"
    )
    if errors:
        for e in errors[:30]:
            print(f"  [ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
