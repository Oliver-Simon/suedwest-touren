#!/usr/bin/env python3
"""Strict M3 Validator for `data/data.json` and `data/routing_notes.json`.

Enforces 100% of PROJECT.md M3 contracts and E2E data invariants (F01..F12):
- Top-level schema (`generated`, `bases.jugenheim`, `bases.karlsruhe`, `days`, `waypoints`, `tours`, `freq`, `polys`)
- 100% Deutschlandticket compliance (`0` Fernverkehr/FlixTrain/Sonderzug/IC/ICE/EC/TGV/RJX/NJ violations)
- Transfer buffer (`>= 4 min` on every ride->ride transition, major hub `< 5 min` flagged with `hub_tight`)
- Hike synchronization (`stay >= max(walkMin + 30, 270 if walkMin >= 240 else 0)`) on `fwd/rev`, `lastSafeReturn`, and `alts_fwd[0]`
- Dual-base origin proximity (`<= 2500 m` for Jugenheim, `<= 3000 m` for Karlsruhe)
- `>= 3` deduplicated, sorted departure windows in `alts_fwd` (or documented in `routing_notes.json` / `unroutable`)
- Valid `lastSafeReturn` (`dep >= rev[0].dep`, arrival before `02:30`)
- Ruftaxi & seasonal bus warnings (`warnings[]`) without false positives on regular buses
- Valid precision-5 (`1e5`) polylines in `polys` within Southwest bounds
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_PATH = ROOT_DIR / "data" / "data.json"
DEFAULT_NOTES_PATH = ROOT_DIR / "data" / "routing_notes.json"

JUGENHEIM_BASE = {"lat": 49.753891, "lon": 8.629438, "dhid": "de:06432:24355", "nearestRailIbnr": "8000956"}
KARLSRUHE_BASE = {"lat": 48.993530, "lon": 8.401939, "dhid": "de:08212:89", "ibnr": "8000191"}

FORBIDDEN_MODES = {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "AIRPLANE", "COACH"}
ALLOWED_RIDE_MODES = {"sbahn", "re", "rb", "stadtbahn", "tram", "bus", "ferry", "rail"}

FORBIDDEN_LINE_RE = re.compile(
    r"(?:^|\b)(?:ICE|IC|EC|ECE|FLX|FlixTrain|TGV|RJX|RJ|NJ|EN|EST|THA|Sonderzug)(?:\b|\d)",
    re.IGNORECASE,
)
FORBIDDEN_OP_RE = re.compile(
    r"(?:db\s+fernverkehr|flixtrain|sncf|öbb\s*nightjet|\bnightjet\b|thalys|eurostar|sonderzug)",
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


def parse_hhmm(val: Any) -> int | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if "T" in s:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.hour * 60 + d.minute
        except ValueError:
            pass
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def minutes_diff(dep: str, arr: str, dep_iso: str | None = None, arr_iso: str | None = None) -> int:
    if dep_iso and arr_iso:
        try:
            d0 = dt.datetime.fromisoformat(dep_iso.replace("Z", "+00:00"))
            d1 = dt.datetime.fromisoformat(arr_iso.replace("Z", "+00:00"))
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


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


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


def in_sw_bounds(lat: float, lon: float) -> bool:
    return 47.0 <= lat <= 52.0 and 5.5 <= lon <= 11.5


def is_forbidden_fernverkehr(seg: dict[str, Any]) -> str | None:
    mode = str(seg.get("mode", "")).strip().upper()
    if mode in FORBIDDEN_MODES:
        return f"Forbidden mode={mode}"
    for key in ("line", "lineName", "displayName", "tripShortName", "trainType", "l"):
        val = str(seg.get(key, "") or "")
        if val and FORBIDDEN_LINE_RE.search(val):
            return f"Forbidden line {key}={val!r}"
    for key in ("op", "agencyName", "operator", "note"):
        val = str(seg.get(key, "") or "")
        if val and FORBIDDEN_OP_RE.search(val):
            return f"Forbidden operator/marker {key}={val!r}"
    if seg.get("class") == 0:
        return "Forbidden VRN product class=0"
    for sub in seg.get("legs") or []:
        if isinstance(sub, dict):
            hit = is_forbidden_fernverkehr(sub)
            if hit:
                return hit
    return None


def compute_transfers(segs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], int, str]]:
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
        hub = (next_r.get("from") or {}).get("name") or (prev_r.get("to") or {}).get("name") or ""
        out.append((prev_r, next_r, gap, hub))
    return out


def is_major_hub(stop_name: str) -> bool:
    low = (stop_name or "").lower()
    return any(h in low for h in MAJOR_HUBS)


def compute_hike_stay(fwd_segs: list[dict[str, Any]], rev_segs: list[dict[str, Any]]) -> int | None:
    if not fwd_segs or not rev_segs:
        return None
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
    blob = " ".join(str(seg.get(k, "") or "") for k in ("line", "lineName", "op", "mode", "dest", "note", "stayNote"))
    return bool(RUFTAXI_RE.search(blob))


def seg_is_seasonal_text(seg: dict[str, Any]) -> bool:
    blob = " ".join(str(seg.get(k, "") or "") for k in ("line", "lineName", "op", "dest", "note", "stayNote"))
    return bool(SEASONAL_RE.search(blob))


def iter_itineraries(plan: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    out = []
    if plan.get("fwd"):
        out.append(("fwd", plan["fwd"]))
    if plan.get("rev"):
        out.append(("rev", plan["rev"]))
    for i, a in enumerate(plan.get("alts_fwd") or []):
        segs = a.get("segs") if isinstance(a, dict) else a
        if segs:
            out.append((f"alts_fwd[{i}]", segs))
    for i, a in enumerate(plan.get("alts_rev") or []):
        segs = a.get("segs") if isinstance(a, dict) else a
        if segs:
            out.append((f"alts_rev[{i}]", segs))
    lsr = plan.get("lastSafeReturn")
    if isinstance(lsr, dict) and lsr.get("segs"):
        out.append(("lastSafeReturn", lsr["segs"]))
    return out


def validate_dataset(data: dict[str, Any], notes_raw: Any) -> list[str]:
    errors: list[str] = []

    if not str(data.get("generated", "")).strip():
        errors.append("Top-level 'generated' missing")
    bases = data.get("bases") or {}
    jg = bases.get("jugenheim") or {}
    ka = bases.get("karlsruhe") or {}
    if jg.get("dhid") != JUGENHEIM_BASE["dhid"]:
        errors.append(f"bases.jugenheim.dhid mismatch: {jg.get('dhid')}")
    if str((jg.get("nearestRail") or {}).get("ibnr", "")) != JUGENHEIM_BASE["nearestRailIbnr"]:
        errors.append("bases.jugenheim.nearestRail.ibnr mismatch")
    if ka.get("dhid") != KARLSRUHE_BASE["dhid"] or str(ka.get("ibnr", "")) != KARLSRUHE_BASE["ibnr"]:
        errors.append("bases.karlsruhe dhid/ibnr mismatch")

    days = data.get("days") or {}
    for k in ("wd", "sa", "so"):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(days.get(k, ""))):
            errors.append(f"days.{k} invalid format: {days.get(k)}")

    if len(data.get("waypoints") or []) != 19:
        errors.append(f"Expected 19 waypoints, got {len(data.get('waypoints') or [])}")

    tours = data.get("tours") or []
    if len(tours) != 144:
        errors.append(f"Expected 144 tours, got {len(tours)}")

    polys = data.get("polys") or []
    for idx, p_str in enumerate(polys):
        if p_str:
            pts = decode_polyline(p_str, precision=5)
            if len(pts) < 2:
                errors.append(f"polys[{idx}] decoded < 2 points")
            elif not in_sw_bounds(pts[0][0], pts[0][1]):
                errors.append(f"polys[{idx}] point[0] out of SW bounds: {pts[0]}")

    # Extract routing notes list
    note_entries: list[dict[str, Any]] = []
    if isinstance(notes_raw, list):
        note_entries = [x for x in notes_raw if isinstance(x, dict)]
    elif isinstance(notes_raw, dict):
        for k in ("notes", "unroutable", "items", "entries"):
            if isinstance(notes_raw.get(k), list):
                note_entries = [x for x in notes_raw[k] if isinstance(x, dict)]
                break

    note_by_id: dict[str, dict[str, Any]] = {}
    for n in note_entries:
        tid = str(n.get("tour_id") or n.get("id") or "").strip()
        rsn = str(n.get("reason") or "").strip()
        if not tid or len(rsn) < 10:
            errors.append(f"Invalid routing_notes entry: {n}")
        else:
            note_by_id[tid] = n

    for tour in tours:
        tid = tour.get("id", "?")
        wmin = int((tour.get("hike") or {}).get("walkMin") or 0)
        min_req_stay = max(wmin + 30, 270 if wmin >= 240 else 0) if wmin > 0 else 0
        plans = tour.get("plans") or {}
        unr = tour.get("unroutable") or {}

        for b in ("jugenheim", "karlsruhe"):
            if b not in plans:
                errors.append(f"{tid} missing plans.{b}")
                continue
            for d in ("wd", "sa", "so"):
                if d not in plans[b]:
                    errors.append(f"{tid} missing plans.{b}.{d}")
                    continue
                plan = plans[b][d]
                if not isinstance(plan, dict):
                    errors.append(f"{tid} {b}/{d} plan is not a dict")
                    continue

                is_routable = not plan.get("fallback") and bool(plan.get("fwd")) and bool(plan.get("rev"))
                if not is_routable:
                    r_local = (unr.get(b) or {}).get(d) or plan.get("reason")
                    r_note = (note_by_id.get(tid) or {}).get("reason")
                    if not str(r_local or r_note or "").strip():
                        errors.append(f"{tid} {b}/{d} fallback/unroutable lacks reason")
                    continue

                # Origin proximity check (T2-036, T2-037)
                fr0 = plan["fwd"][0].get("from") or {}
                if fr0.get("lat") is not None:
                    base_c = JUGENHEIM_BASE if b == "jugenheim" else KARLSRUHE_BASE
                    max_d = 2500.0 if b == "jugenheim" else 3000.0
                    dist = haversine_m(base_c["lat"], base_c["lon"], float(fr0["lat"]), float(fr0["lon"]))
                    if dist > max_d:
                        errors.append(f"{tid} {b}/{d} fwd[0].from is {dist:.0f}m from base (> {max_d}m)")

                # Hike stay check (F07)
                if wmin > 0:
                    stay = compute_hike_stay(plan["fwd"], plan["rev"])
                    if stay is None or stay < min_req_stay:
                        errors.append(f"{tid} {b}/{d} hike stay={stay} < required {min_req_stay}")

                # lastSafeReturn check (F08, T2-039)
                lsr = plan.get("lastSafeReturn")
                if not isinstance(lsr, dict) or not lsr.get("dep") or not lsr.get("arr") or not lsr.get("segs"):
                    errors.append(f"{tid} {b}/{d} missing valid lastSafeReturn")
                else:
                    if wmin > 0:
                        lsr_stay = compute_hike_stay(plan["fwd"], lsr["segs"])
                        if lsr_stay is None or lsr_stay < min_req_stay:
                            errors.append(f"{tid} {b}/{d} lastSafeReturn stay={lsr_stay} < {min_req_stay}")
                    rev_dep = parse_hhmm(plan["rev"][0].get("dep"))
                    lsr_dep = parse_hhmm(lsr.get("dep"))
                    lsr_arr = parse_hhmm(lsr.get("arr"))
                    if rev_dep is not None and lsr_dep is not None:
                        adj_lsr = lsr_dep + (1440 if lsr_dep < 240 and rev_dep > 600 else 0)
                        if adj_lsr < rev_dep:
                            errors.append(f"{tid} {b}/{d} lastSafeReturn dep {lsr.get('dep')} < rev dep {plan['rev'][0].get('dep')}")
                    if lsr_arr is not None and lsr_dep is not None and lsr_arr < lsr_dep:
                        if lsr_arr > 150:
                            errors.append(f"{tid} {b}/{d} lastSafeReturn arrives after 02:30 ({lsr.get('arr')})")

                # alts_fwd sorted, deduplicated, >= 3 windows (F08, T2-038)
                alts_f = plan.get("alts_fwd") or []
                alts_r = plan.get("alts_rev") or []
                sigs = []
                prev_dep = -1
                deps_set = set()
                for a in alts_f:
                    segs = a["segs"] if isinstance(a, dict) else a
                    if not segs:
                        continue
                    dep_m = parse_hhmm(segs[0].get("dep"))
                    if dep_m is None or dep_m < prev_dep:
                        errors.append(f"{tid} {b}/{d} alts_fwd not sorted: {segs[0].get('dep')}")
                    else:
                        prev_dep = dep_m
                    deps_set.add(segs[0].get("dep"))
                    sig = (
                        segs[0].get("dep"),
                        segs[-1].get("arr"),
                        tuple(s.get("line") for s in segs if s.get("kind") == "ride"),
                    )
                    if sig in sigs:
                        errors.append(f"{tid} {b}/{d} duplicate alt in alts_fwd: {sig}")
                    sigs.append(sig)
                if len(deps_set) < 3 and tid not in note_by_id and not (unr.get(b) or {}).get(d):
                    errors.append(f"{tid} {b}/{d} has only {len(deps_set)} departure windows without routing note")

                # Compatible alt pair check (T1-035)
                if wmin > 0 and alts_f and alts_r:
                    f0 = alts_f[0]["segs"] if isinstance(alts_f[0], dict) else alts_f[0]
                    ok_pair = False
                    for ar in alts_r:
                        rs = ar["segs"] if isinstance(ar, dict) else ar
                        st = compute_hike_stay(f0, rs)
                        if st is not None and st >= min_req_stay:
                            ok_pair = True
                            break
                    if not ok_pair:
                        errors.append(f"{tid} {b}/{d} alts_fwd[0] has no compatible return >= {min_req_stay}m in alts_rev")

                # Check every itinerary for D-Ticket, transfers, durations, coords, warnings
                for it_name, segs in iter_itineraries(plan):
                    for seg in segs:
                        hit = is_forbidden_fernverkehr(seg)
                        if hit:
                            errors.append(f"{tid} {b}/{d}/{it_name}: {hit}")
                        if seg.get("kind") == "ride":
                            m = str(seg.get("mode", "")).lower()
                            if m not in ALLOWED_RIDE_MODES:
                                errors.append(f"{tid} {b}/{d}/{it_name} invalid ride mode {m!r}")
                            if not str(seg.get("line") or seg.get("lineName") or "").strip():
                                errors.append(f"{tid} {b}/{d}/{it_name} ride missing line")
                            if seg_is_ruftaxi_text(seg) and not seg.get("warnings"):
                                errors.append(f"{tid} {b}/{d}/{it_name} Ruftaxi segment missing warnings[]")
                            if seg_is_seasonal_text(seg) and not seg.get("warnings"):
                                errors.append(f"{tid} {b}/{d}/{it_name} seasonal segment missing warnings[]")
                            if seg.get("line") in ("Bus 670", "Bus 213") and not seg_is_ruftaxi_text(seg):
                                if any(w.get("type") == "ruftaxi" for w in (seg.get("warnings") or [])):
                                    errors.append(f"{tid} {b}/{d}/{it_name} false ruftaxi warning on {seg.get('line')}")
                        if seg.get("minStay") is not None and wmin > 0:
                            if int(seg["minStay"]) < min_req_stay:
                                errors.append(f"{tid} {b}/{d}/{it_name} minStay={seg['minStay']} < {min_req_stay}")
                        if seg.get("dep") and seg.get("arr"):
                            dur = minutes_diff(seg["dep"], seg["arr"], seg.get("depIso"), seg.get("arrIso"))
                            if dur < 0 or dur > 720:
                                errors.append(f"{tid} {b}/{d}/{it_name} implausible segment duration {dur}m")
                        for endp in (seg.get("from"), seg.get("to")):
                            if isinstance(endp, dict) and endp.get("lat") is not None:
                                lat, lon = float(endp["lat"]), float(endp["lon"])
                                if (lat == 0.0 and lon == 0.0) or not in_sw_bounds(lat, lon):
                                    errors.append(f"{tid} {b}/{d}/{it_name} stop out of bounds: {endp}")
                        for w in seg.get("warnings") or []:
                            if not str(w.get("type", "")).strip() or len(str(w.get("text", "")).strip()) < 8:
                                errors.append(f"{tid} {b}/{d}/{it_name} invalid warning object: {w}")
                        for sub in seg.get("legs") or []:
                            p_idx = sub.get("p")
                            if not isinstance(p_idx, int) or p_idx < 0 or p_idx >= len(polys):
                                errors.append(f"{tid} {b}/{d}/{it_name} invalid polyline index p={p_idx}")

                    for prev_r, next_r, gap, hub in compute_transfers(segs):
                        if gap < 4:
                            errors.append(f"{tid} {b}/{d}/{it_name} transfer at {hub} = {gap}m < 4m")
                        t_field = prev_r.get("transfer")
                        if t_field is not None and (int(t_field) < 4 or abs(int(t_field) - gap) > 2):
                            errors.append(f"{tid} {b}/{d}/{it_name} transfer={t_field} mismatch gap={gap}")
                        if is_major_hub(hub) and gap < 5:
                            warns = list(prev_r.get("warnings") or []) + list(next_r.get("warnings") or [])
                            if not warns:
                                errors.append(f"{tid} {b}/{d}/{it_name} major hub {hub} gap={gap}m < 5m unflagged")

        if tour.get("source") == "curated":
            for b in ("jugenheim", "karlsruhe"):
                if not any(
                    not plans[b][d].get("fallback") and plans[b][d].get("fwd") and plans[b][d].get("rev")
                    for d in ("wd", "sa", "so")
                ):
                    errors.append(f"Curated tour {tid} not routable on any day from {b}")

    return errors


def main() -> int:
    data_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DATA_PATH
    notes_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_NOTES_PATH
    if not data_path.exists():
        print(f"ERROR: {data_path} does not exist", file=sys.stderr)
        return 1
    if not notes_path.exists():
        print(f"ERROR: {notes_path} does not exist", file=sys.stderr)
        return 1
    data = json.loads(data_path.read_text(encoding="utf-8"))
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    errors = validate_dataset(data, notes)

    routable_slots = 0
    fallback_slots = 0
    for t in data.get("tours", []):
        for b in ("jugenheim", "karlsruhe"):
            for d in ("wd", "sa", "so"):
                p = ((t.get("plans") or {}).get(b) or {}).get(d) or {}
                if not p.get("fallback") and p.get("fwd") and p.get("rev"):
                    routable_slots += 1
                else:
                    fallback_slots += 1

    print(
        f"validate.py: {len(data.get('tours', []))} tours | "
        f"{routable_slots} routable slots | {fallback_slots} fallback slots | "
        f"{len(data.get('polys', []))} polys | {len(errors)} errors"
    )
    if errors:
        for e in errors[:50]:
            print(f"  [ERROR] {e}", file=sys.stderr)
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more errors", file=sys.stderr)
        return 1
    print("validate.py: ALL CHECKS PASSED (0 errors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
