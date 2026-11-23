#!/usr/bin/env python3
"""M1/M2 Catalog & GPX Geometry Validator for Südwest-Touren."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MAPY_DIR = DATA_DIR / "mapy"
IMG_DIR = BASE_DIR / "img"
CREDITS_PATH = BASE_DIR / "img_src" / "credits.json"

ALLOWED_SOURCES = {"shortcut", "mapy", "curated"}
ALLOWED_REGIONS = {"pfaelzerwald", "schwarzwald", "odenwald", "weitere"}
ALLOWED_CATEGORIES = {"wanderung", "berg", "see", "burg", "wein", "stadt", "panoramatour"}
ALLOWED_DIFFICULTIES = {"leicht", "mittel", "schwer"}
ALLOWED_KINDS = {"shortcut_360", "shortcut_pair", "shortcut_standalone", "gpx_linked", "curated"}
EXPECTED_EXCLUDED = {7, 9, 10, 17, 20, 23, 25, 52, 53, 54, 63, 64, 85}
FREE_LICENSES_RE = re.compile(
    r"^(CC[\s-]?BY(?:[\s-]?SA)?(?:\s+\d\.\d)?|CC0|PD|Public\s+domain|GFDL|CC\s+Public\s+Domain)",
    re.IGNORECASE,
)


def haversine_m(a: list[float], b: list[float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(x)))


def decode_poly5(encoded: str) -> list[list[float]]:
    pts: list[list[float]] = []
    idx = 0
    lat = 0
    lon = 0
    n = len(encoded)
    while idx < n:
        for is_lon in (False, True):
            shift = 0
            res = 0
            while True:
                b = ord(encoded[idx]) - 63
                idx += 1
                res |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if (res & 1) else (res >> 1)
            if is_lon:
                lon += d
            else:
                lat += d
        pts.append([lat / 1e5, lon / 1e5])
    return pts


def validate_media(tours: list[dict]) -> None:
    from PIL import Image

    assert CREDITS_PATH.exists(), f"Missing {CREDITS_PATH}"
    credits = json.loads(CREDITS_PATH.read_text(encoding="utf-8"))
    assert isinstance(credits, dict), f"credits.json must be dict, got {type(credits)}"

    for t in tours:
        tid = t["id"]
        img_path = IMG_DIR / f"{tid}.jpg"
        assert img_path.exists(), f"Missing image {img_path}"
        sz = img_path.stat().st_size
        assert sz > 5000, f"Image {img_path} too small ({sz} bytes <= 5000)"
        head = img_path.read_bytes()[:64].lstrip().lower()
        assert not head.startswith((b"<!doctype", b"<html", b"<svg")), f"{img_path} is HTML/SVG masquerading as JPEG"
        with Image.open(img_path) as im:
            assert im.format == "JPEG", f"{img_path} format {im.format} != JPEG"
            assert im.size == (800, 450), f"{img_path} size {im.size} != (800, 450)"

        c = credits.get(tid)
        assert isinstance(c, dict), f"Missing credits.json entry for {tid}"
        for k in ("file", "artist", "license", "licenseUrl", "page"):
            assert str(c.get(k, "")).strip(), f"Credit for {tid} missing {k}"
        lic = str(c["license"]).strip()
        assert FREE_LICENSES_RE.match(lic), f"Non-free or invalid license for {tid}: {lic!r}"
        assert str(c["licenseUrl"]).startswith("https://"), f"{tid} non-https licenseUrl: {c['licenseUrl']!r}"
        assert str(c["page"]).startswith("https://"), f"{tid} non-https page: {c['page']!r}"
        assert "<script" not in str(c["artist"]).lower(), f"{tid} unsafe artist string"

    print(f"MEDIA PASS: {len(tours)} images (800x450 JPEG > 5KB) and {len(tours)} credits verified.")


def validate(catalog_path: Path, check_media: bool = False) -> None:
    excl_path = DATA_DIR / "excluded_shortcuts.json"
    assert excl_path.exists(), f"Missing {excl_path}"
    assert catalog_path.exists(), f"Missing {catalog_path}"

    excluded = json.loads(excl_path.read_text(encoding="utf-8"))
    assert isinstance(excluded, list) and len(excluded) == 13, f"Expected 13 excluded, got {len(excluded)}"
    excl_indices = {x["rawIndex"] for x in excluded}
    assert excl_indices == EXPECTED_EXCLUDED, f"Excluded mismatch: {excl_indices ^ EXPECTED_EXCLUDED}"
    for x in excluded:
        for k in ("rawIndex", "app", "id", "label", "category", "reason"):
            assert x.get(k) not in (None, ""), f"Missing {k} in excluded item {x}"
        assert isinstance(x.get("borderlineReviewed"), bool), f"Missing borderlineReviewed in {x}"

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog.get("generated"), "Missing catalog.generated"

    # Waypoints invariant
    wpts = catalog.get("waypoints") or []
    assert len(wpts) == 19, f"Expected 19 waypoints, got {len(wpts)}"
    wpt_root = ET.parse(MAPY_DIR / "points_26_11_23_143109.gpx").getroot()
    raw_wpts = wpt_root.findall("{http://www.topografix.com/GPX/1/1}wpt")
    assert len(raw_wpts) == 19
    for w in wpts:
        assert w.get("name") and w.get("lat") is not None and w.get("lon") is not None and w.get("ele") is not None
        assert w.get("region") in ALLOWED_REGIONS, f"Invalid waypoint region: {w}"

    tours = catalog.get("tours") or []
    assert len(tours) > 0, "No tours in catalog"

    seen_ids: set[str] = set()
    seen_sc_indices: set[int] = set()
    all_gpx_files: set[str] = set()
    ring360_count = 0
    pair_count = 0
    gpx_payload_bytes = 0
    unlinked_list: list[tuple[str, str]] = []
    found_41_min_change = False
    found_sand_gertelbach = False

    curated_tours: list[dict] = []
    for t in tours:
        tid = t["id"]
        assert re.match(r"^[a-z0-9_-]+$", tid), f"Invalid slug id: {tid}"
        assert tid not in seen_ids, f"Duplicate id: {tid}"
        seen_ids.add(tid)

        assert t["source"] in ALLOWED_SOURCES, f"Invalid source in {tid}: {t['source']}"
        assert t["region"] in ALLOWED_REGIONS, f"Invalid region in {tid}: {t['region']}"
        assert t["category"] in ALLOWED_CATEGORIES, f"Invalid category in {tid}: {t['category']}"
        assert t["difficulty"] in ALLOWED_DIFFICULTIES, f"Invalid difficulty in {tid}: {t['difficulty']}"
        assert isinstance(t["ring"], bool), f"ring not bool in {tid}"
        assert isinstance(t["eveningSun"], bool), f"eveningSun not bool in {tid}"
        assert isinstance(t["places"], list) and len(t["places"]) >= 1, f"places empty in {tid}"

        for field in ("title", "subtitle", "sun", "tips"):
            for lang in ("de", "en"):
                assert isinstance(t[field].get(lang), str) and t[field][lang].strip(), f"Empty {field}.{lang} in {tid}"

        for ridx in t.get("rawShortcutIndices") or []:
            seen_sc_indices.add(ridx)
            if ridx == 41:
                assert t.get("minChangeTime") == 10, f"#41 missing minChangeTime=10 in {tid}"
                found_41_min_change = True

        tr = t["transit"]
        assert tr["kind"] in ALLOWED_KINDS, f"Invalid transit.kind in {tid}: {tr['kind']}"
        if tr["kind"] == "shortcut_360":
            ring360_count += 1
        elif tr["kind"] == "shortcut_pair":
            pair_count += 1

        for stop in [tr.get("startStop"), tr.get("endStop")] + list(tr.get("vias") or []):
            if stop is None:
                continue
            assert stop.get("name") and stop.get("lat") is not None and stop.get("lon") is not None, f"Invalid stop in {tid}: {stop}"
            assert not (stop.get("transitousId") or "").startswith("de-amarillo-"), f"Carpool leak in {tid}: {stop}"
            assert any(stop.get(k) for k in ("dhid", "stationId", "transitousId", "ibnr", "remoteId")), f"Stop missing ID in {tid}: {stop}"
            if stop.get("type") == "STATION":
                assert stop.get("stationId") and stop.get("remoteId"), f"STATION missing stationId/remoteId in {tid}: {stop}"

        hk = t["hike"]
        if tr["unlinkedReason"] is None:
            assert tr["startStop"] and tr["endStop"], f"{tid} missing stop with null unlinkedReason"
            if hk["hasGpx"]:
                assert tr["walkToStartM"] is not None and 0 <= tr["walkToStartM"] <= 800, f"walkToStartM > 800 in {tid}: {tr['walkToStartM']}"
                assert tr["walkFromEndM"] is not None and 0 <= tr["walkFromEndM"] <= 800, f"walkFromEndM > 800 in {tid}: {tr['walkFromEndM']}"
                d_s = haversine_m([tr["startStop"]["lat"], tr["startStop"]["lon"]], hk["startCoord"])
                d_e = haversine_m([tr["endStop"]["lat"], tr["endStop"]["lon"]], hk["endCoord"])
                assert d_s <= 800.5, f"startStop {d_s:.1f}m > 800m in {tid}"
                assert d_e <= 800.5, f"endStop {d_e:.1f}m > 800m in {tid}"
                if not t["ring"]:
                    id_s = tr["startStop"].get("dhid") or tr["startStop"].get("stationId") or tr["startStop"]["name"]
                    id_e = tr["endStop"].get("dhid") or tr["endStop"].get("stationId") or tr["endStop"]["name"]
                    assert id_s != id_e, f"Collapsed non-ring stops on {tid}: {id_s}"
        else:
            unlinked_list.append((tid, tr["unlinkedReason"]))
            assert tr["startStop"] is None or tr["endStop"] is None, f"{tid} has unlinkedReason but both stops populated"

        if tid == "buchen-odenwald-bus-rueckfahrt":
            assert tr["startStop"]["dhid"] == "de:06432:24355" and tr["endStop"]["dhid"] == "de:06432:24355", (
                f"#68 endpoints not normalized to Jugenheim in {tid}"
            )
            assert [v["name"] for v in tr["vias"]] == [
                "Buchen(Odenw)",
                "Eberbach",
                "Reichenberghalle, Reichelsheim (Odenwald)",
            ], f"#68 vias mismatch in {tid}: {[v['name'] for v in tr['vias']]}"

        if t["source"] == "curated":
            curated_tours.append(t)
            assert tr["kind"] == "curated", f"Curated tour {tid} must have transit.kind='curated', got {tr['kind']}"
            assert tr["unlinkedReason"] is None, f"Curated tour {tid} has unlinkedReason: {tr['unlinkedReason']}"
            assert tr["startStop"] and tr["endStop"], f"Curated tour {tid} missing startStop/endStop"
            for wk in ("walkToStartM", "walkFromEndM"):
                wv = tr.get(wk)
                assert wv is None or (isinstance(wv, (int, float)) and 0 <= wv <= 800), (
                    f"Curated tour {tid} invalid {wk}: {wv}"
                )
            if not t["ring"]:
                ss, es = tr["startStop"], tr["endStop"]
                id_s = ss.get("dhid") or ss.get("stationId") or ss["name"]
                id_e = es.get("dhid") or es.get("stationId") or es["name"]
                assert id_s != id_e, f"Collapsed non-ring stops on curated tour {tid}: {id_s}"
            assert hk.get("hasGpx") is False, f"Curated tour {tid} must have hasGpx=False (no fake GPX)"
            assert hk.get("gpxFiles") == [], f"Curated tour {tid} must have gpxFiles=[]"
            assert hk.get("startCoord") is None and hk.get("endCoord") is None, (
                f"Curated tour {tid} must have startCoord=None and endCoord=None"
            )
            assert hk.get("poly5") == "", f"Curated tour {tid} must have poly5=''"
            assert hk.get("elevProfile") == [], f"Curated tour {tid} must have elevProfile=[]"
            assert hk.get("distKm", -1) >= 0 and hk.get("elevGainM", -1) >= 0, f"Negative hike metric in {tid}"
            assert hk.get("elevLossM", -1) >= 0 and hk.get("walkMin", -1) >= 0, f"Negative hike metric in {tid}"
            if hk["walkMin"] > 0:
                assert hk["distKm"] > 0, f"walkMin > 0 with distKm <= 0 in {tid}"
                eff_km = hk["distKm"] + hk["elevGainM"] / 100.0
                pace = eff_km / (hk["walkMin"] / 60.0)
                assert 2.0 <= pace <= 6.5, f"Swiss pace {pace:.2f} km/h out of [2.0, 6.5] in {tid}"

        if hk["hasGpx"]:
            assert not tr.get("vias"), f"GPX tour {tid} must not have transit.vias: {tr.get('vias')}"
            if tid == "hoeningen-sonnenwende-bad-duerkheim":
                assert "Sonnenwende, Bad Dürkheim" in t["places"], f"Missing Sonnenwende POI in {tid}.places"
            assert hk["distKm"] > 0, f"distKm <= 0 in {tid}"
            assert hk["walkMin"] >= math.ceil((hk["distKm"] / 4.0) * 60), f"walkMin too low in {tid}"
            assert len(hk["elevProfile"]) <= 30, f"elevProfile > 30 pts in {tid}"
            pts = decode_poly5(hk["poly5"])
            assert len(pts) >= 2, f"poly5 < 2 pts in {tid}"
            for p_lat, p_lon in pts:
                assert 48.0 <= p_lat <= 50.6 and 6.5 <= p_lon <= 10.2, f"Out-of-bounds coord in {tid}: {p_lat},{p_lon}"
            assert haversine_m(pts[0], hk["startCoord"]) <= 2.5, f"poly5 start mismatch in {tid}"
            assert haversine_m(pts[-1], hk["endCoord"]) <= 2.5, f"poly5 end mismatch in {tid}"
            all_gpx_files.update(hk["gpxFiles"])
            if "Route von Sand Kapelle nach Bühlertal Gertelbachstraße.gpx" in hk["gpxFiles"] and len(hk["gpxFiles"]) == 1:
                assert hk["walkMin"] >= 100, f"Sand->Gertelbach walkMin {hk['walkMin']} < 100"
                found_sand_gertelbach = True
            gpx_payload_bytes += len(hk["poly5"]) + len(json.dumps(hk["elevProfile"]))

    assert ring360_count == 19, f"Expected 19 shortcut_360, got {ring360_count}"
    assert pair_count == 9, f"Expected 9 shortcut_pair, got {pair_count}"
    assert found_41_min_change, "#41 minChangeTime=10 not verified"
    assert found_sand_gertelbach, "Sand->Gertelbach track not found"

    expected_retained = set(range(1, 86)) - EXPECTED_EXCLUDED
    assert seen_sc_indices == expected_retained, f"Shortcut coverage mismatch: {expected_retained ^ seen_sc_indices}"

    # Deduplication invariants
    assert "Route from Löhrbach_ Ost to Unter-Abtsteinach_ Schule (1).gpx" not in all_gpx_files
    assert "Route from Scenic viewpoint to Rhodt_ Mitte (1).gpx" not in all_gpx_files
    assert "Route from Ebersteinburg Kapelle to Ebersteinburg Kapelle.gpx" in all_gpx_files
    assert "Trip from Ebersteinburg Kapelle.gpx" in all_gpx_files
    assert "Route from Parking lot to Parking lot.gpx" in all_gpx_files
    assert "Route from Parking lot to Parking lot (1).gpx" in all_gpx_files
    assert len(all_gpx_files) == 64, f"Expected 64 unique GPX filenames across catalog, got {len(all_gpx_files)}"
    assert gpx_payload_bytes < 60_000, f"GPX payload too large: {gpx_payload_bytes} B"

    excluded_gpx = catalog.get("excludedGpx")
    assert isinstance(excluded_gpx, list) and len(excluded_gpx) >= 1, (
        "Missing or empty catalog['excludedGpx'] documenting Stage 2 duplicates"
    )
    excl_gpx_files: set[str] = set()
    for doc in excluded_gpx:
        assert isinstance(doc, dict), f"Invalid excludedGpx entry: {doc}"
        fn = doc.get("file")
        assert isinstance(fn, str) and fn.strip(), f"excludedGpx entry missing file: {doc}"
        assert isinstance(doc.get("duplicateOf"), str) and doc["duplicateOf"].strip(), (
            f"excludedGpx entry missing duplicateOf: {doc}"
        )
        assert isinstance(doc.get("reason"), str) and doc["reason"].strip(), (
            f"excludedGpx entry missing reason: {doc}"
        )
        excl_gpx_files.add(Path(fn).name)

    assert "Route from Scenic viewpoint to Rhodt_ Mitte (1).gpx" in excl_gpx_files, (
        "Stage 2 duplicate 'Route from Scenic viewpoint to Rhodt_ Mitte (1).gpx' missing from excludedGpx"
    )
    assert not (excl_gpx_files & all_gpx_files), (
        f"excludedGpx overlaps active tour gpxFiles: {excl_gpx_files & all_gpx_files}"
    )

    sha_groups: dict[str, list[str]] = {}
    for p in sorted(MAPY_DIR.glob("*.gpx")):
        if p.name in ("My Tours.gpx", "points_26_11_23_143109.gpx"):
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        sha_groups.setdefault(h, []).append(p.name)
    assert len(sha_groups) == 65, f"Expected 65 SHA-unique GPX files in {MAPY_DIR}, got {len(sha_groups)}"
    covered_gpx = all_gpx_files | excl_gpx_files
    missing_sha_groups = [
        names for names in sha_groups.values() if not any(n in covered_gpx for n in names)
    ]
    assert not missing_sha_groups, f"Unaccounted SHA-unique GPX files: {missing_sha_groups}"
    assert len(covered_gpx) == 65, f"Expected 65 accounted GPX filenames, got {len(covered_gpx)}"

    if curated_tours or catalog_path.name == "tours_catalog_full.json":
        assert 20 <= len(curated_tours) <= 30, f"Expected 20..30 curated tours, got {len(curated_tours)}"
        for reg in ("pfaelzerwald", "schwarzwald", "odenwald"):
            cnt = sum(1 for t in curated_tours if t["region"] == reg)
            assert cnt >= 5, f"Region {reg} has only {cnt} curated tours (<5)"
        cur_blob = json.dumps(curated_tours, ensure_ascii=False).lower()
        for req_kw in ("krähberg", "ohrsberg"):
            assert req_kw in cur_blob, f"Curated tours missing required waypoint keyword {req_kw!r}"
        wp_hits = sum(
            1 for kw in ("eschkopf", "krähberg", "ohrsberg", "friedrichsturm", "geroldsau") if kw in cur_blob
        )
        assert wp_hits >= 3, f"Expected >= 3 key Mapy waypoints in curated tours, found {wp_hits}"

    print(
        f"VALIDATION PASS: {len(tours)} tours ({len(curated_tours)} curated, "
        f"{ring360_count} shortcut_360, {pair_count} shortcut_pair), "
        f"{len(wpts)} waypoints, 64 unique GPX files, {len(excluded)} excluded shortcuts, "
        f"{len(unlinked_list)} unlinked (>800m), GPX payload {gpx_payload_bytes} B."
    )
    for uid, rsn in unlinked_list:
        print(f"  [unlinked] {uid}: {rsn}")

    if check_media:
        validate_media(tours)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate Südwest-Touren M1/M2 catalog & media.")
    parser.add_argument(
        "catalog",
        nargs="?",
        default=str(DATA_DIR / "tours_catalog.json"),
        help="Path to tours_catalog.json or tours_catalog_full.json",
    )
    parser.add_argument(
        "--check-media",
        action="store_true",
        help="Also verify 100%% img/<id>.jpg (JPEG 800x450 >5KB) and img_src/credits.json coverage",
    )
    args = parser.parse_args()
    validate(Path(args.catalog), check_media=args.check_media)
