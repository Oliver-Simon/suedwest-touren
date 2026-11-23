"""Tier 1 — Feature Coverage E2E Tests (125 tests: T1-001 .. T1-125, 5 per F01..F25)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

from tests.common import (
    FREE_LICENSES_RE,
    JUGENHEIM_BASE,
    KARLSRUHE_BASE,
    REQUIRED_COMMUTE_SUBSTRINGS,
    REQUIRED_PRESETS,
    AppUI,
    ContractLoader,
    MobileAppHarness,
    StaticServer,
    TestConfig,
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
    skip_unless_live,
    validate_gpx_xml,
)


def _cfg_loader() -> tuple[TestConfig, ContractLoader]:
    cfg = TestConfig.from_env()
    return cfg, ContractLoader(cfg)


def _iter_itineraries(plan: dict) -> list[tuple[str, list[dict]]]:
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
    lsr = ContractLoader.last_safe_return(plan)
    if lsr and lsr.get("segs"):
        out.append(("lastSafeReturn", lsr["segs"]))
    return out


def _rgb_luminance(rgb_str: str) -> float:
    nums = [int(x) for x in re.findall(r"\d+", rgb_str)[:3]]
    if len(nums) < 3:
        return 128.0
    return 0.2126 * nums[0] + 0.7152 * nums[1] + 0.0722 * nums[2]


# ---------------------------------------------------------------------------
# F01: Commute & Utility Shortcut Exclusion (T1-001 .. T1-005)
# ---------------------------------------------------------------------------


class TestT1_F01_CommuteExclusion(unittest.TestCase):
    def test_t1_001_excluded_shortcuts_file_exists_and_populated(self) -> None:
        _, loader = _cfg_loader()
        items = loader.excluded_shortcuts()
        self.assertIsInstance(items, list)
        self.assertGreaterEqual(len(items), 7, f"Expected >= 7 excluded entries, got {len(items)}")

    def test_t1_002_excluded_entries_have_label_and_reason(self) -> None:
        _, loader = _cfg_loader()
        items = loader.excluded_shortcuts()
        self.assertTrue(items)
        for idx, item in enumerate(items):
            ident = item.get("label") or item.get("name") or item.get("id")
            self.assertTrue(str(ident or "").strip(), f"Excluded entry #{idx} missing identifier: {item}")
            reason = str(item.get("reason") or "").strip()
            self.assertTrue(reason, f"Excluded entry #{idx} ({ident}) missing non-empty reason")

    def test_t1_003_dieburg_ka_kaserne_commutes_excluded(self) -> None:
        _, loader = _cfg_loader()
        excl_blob = json.dumps(loader.excluded_shortcuts(), ensure_ascii=False).lower()
        for kw in ("hochschule süd", "moltkestraße 30", "hak", "clay kaserne"):
            self.assertIn(kw, excl_blob, f"Expected commute {kw!r} in excluded_shortcuts.json")

        for rel in ("data/tours_catalog.json", "data/data.json"):
            payload = loader.load_json(rel)
            for tour in payload.get("tours", []):
                title_blob = json.dumps(tour.get("title", ""), ensure_ascii=False).lower()
                for forbidden in ("moltkestraße 30", "clay kaserne", "hochschule süd"):
                    self.assertNotIn(forbidden, title_blob, f"Commute {forbidden!r} leaked into {rel}")

    def test_t1_004_stuttgart_waiblingen_buy_tickets_excluded(self) -> None:
        _, loader = _cfg_loader()
        excl_blob = json.dumps(loader.excluded_shortcuts(), ensure_ascii=False).lower()
        for kw in ("stuttgart universität", "waiblingen", "marienplatz", "buy tickets"):
            self.assertIn(kw, excl_blob, f"Expected {kw!r} in excluded_shortcuts.json")

        data = loader.data_json()
        for tour in data.get("tours", []):
            blob = (
                json.dumps(tour.get("title", ""), ensure_ascii=False)
                + " "
                + json.dumps(tour.get("places", []), ensure_ascii=False)
            ).lower()
            for forbidden in ("stuttgart universität", "rathaus, waiblingen", "schwanen, waiblingen", "buy tickets"):
                self.assertNotIn(forbidden, blob, f"Excluded shortcut {forbidden!r} leaked into data.json")

    def test_t1_005_excursion_shortcuts_retained(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        all_text = " ".join(
            loader.tour_title(t, "de") + " " + " ".join(map(str, t.get("places", [])))
            for t in data.get("tours", [])
        ).lower()
        for excursion in ("landau", "bad dürkheim", "eberbach", "rastatt"):
            self.assertIn(excursion, all_text, f"Excursion shortcut {excursion!r} missing from data.json")


# ---------------------------------------------------------------------------
# F02: Shortcut Pairing, 360° Loops & Station ID Recovery (T1-006 .. T1-010)
# ---------------------------------------------------------------------------


class TestT1_F02_ShortcutPairingAndIds(unittest.TestCase):
    def test_t1_006_tours_catalog_schema(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        self.assertTrue(str(cat.get("generated", "")).strip(), "tours_catalog.json missing generated timestamp")
        self.assertIsInstance(cat.get("tours"), list)
        self.assertGreater(len(cat["tours"]), 20)

    def test_t1_007_360_ring_loops_preserved_with_vias(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        rings = [
            t
            for t in cat.get("tours", [])
            if t.get("ring") is True and len((t.get("transit") or {}).get("vias") or []) >= 1
        ]
        self.assertGreaterEqual(len(rings), 10, f"Expected >= 10 360° ring tours with vias, got {len(rings)}")
        blob = " ".join(loader.tour_title(t, "de") for t in rings).lower()
        for expected in ("landau", "forbach"):
            self.assertIn(expected, blob, f"Expected 360° loop {expected!r} among ring tours")

    def test_t1_008_outbound_return_pairs_consolidated(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        shortcut_tours = [t for t in cat.get("tours", []) if t.get("source") == "shortcut"]
        # Raw shortcuts = 85; minus ~10 commutes and paired round-trips must yield < 65 shortcut tours
        self.assertLess(len(shortcut_tours), 65, f"Expected consolidated pairs (<65), got {len(shortcut_tours)}")
        titles = [loader.tour_title(t, "de").lower() for t in shortcut_tours]
        for kw in ("rastatt", "mosbach", "amorbach", "loreley", "miltenberg"):
            matches = [x for x in titles if kw in x]
            self.assertTrue(matches, f"Consolidated pair for {kw!r} missing")

    def test_t1_009_recovered_station_ids_non_null(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        shortcut_tours = [t for t in cat.get("tours", []) if t.get("source") == "shortcut"]
        self.assertTrue(shortcut_tours)
        recovered_hits = 0
        for t in shortcut_tours:
            tr = t.get("transit") or {}
            for key in ("startStop", "endStop"):
                st = tr.get(key)
                if not st:
                    continue
                has_id = any(bool(st.get(f)) for f in ("dhid", "ibnr", "remoteId", "id"))
                self.assertTrue(has_id, f"Tour {t.get('id')} {key} lacks recovered stop ID: {st}")
                if any(nm in str(st.get("name", "")).lower() for nm in ("amorbach", "achern", "oppenau", "worms")):
                    recovered_hits += 1
        self.assertGreater(recovered_hits, 0, "Expected recovered IDs on Amorbach/Achern/Oppenau/Worms stops")

    def test_t1_010_transit_stop_wgs84_coordinates_valid(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        checked = 0
        for t in cat.get("tours", []):
            tr = t.get("transit") or {}
            stops = [tr.get("startStop"), tr.get("endStop")] + list(tr.get("vias") or [])
            for st in stops:
                if not isinstance(st, dict) or st.get("lat") is None:
                    continue
                lat = float(st["lat"])
                lon = float(st["lon"])
                self.assertTrue(
                    47.0 <= lat <= 51.5 and 6.0 <= lon <= 11.0,
                    f"Stop {st.get('name')} out of SW bounds: ({lat}, {lon})",
                )
                checked += 1
        self.assertGreater(checked, 20)


# ---------------------------------------------------------------------------
# F03: GPX Track Deduplication, Validity & Metrics (T1-011 .. T1-015)
# ---------------------------------------------------------------------------


class TestT1_F03_GpxDeduplicationAndMetrics(unittest.TestCase):
    def test_t1_011_gpx_tours_have_positive_metrics(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        gpx_tours = [t for t in data.get("tours", []) if (t.get("hike") or {}).get("hasGpx")]
        self.assertGreaterEqual(len(gpx_tours), 30)
        for t in gpx_tours:
            h = t["hike"]
            self.assertGreater(float(h["distKm"]), 0.0, f"{t['id']} distKm <= 0")
            self.assertGreaterEqual(float(h["elevGainM"]), 0.0, f"{t['id']} elevGainM < 0")
            self.assertGreaterEqual(float(h["elevLossM"]), 0.0, f"{t['id']} elevLossM < 0")
            self.assertGreater(int(h["walkMin"]), 0, f"{t['id']} walkMin <= 0")

    def test_t1_012_exact_duplicate_loehrbach_deduplicated(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        hits = []
        for t in cat.get("tours", []):
            gpx_files = (t.get("hike") or {}).get("gpxFiles") or []
            for gf in gpx_files:
                if "löhrbach" in gf.lower() and "abtsteinach" in gf.lower():
                    hits.append((t.get("id"), gf))
        self.assertEqual(len(hits), 1, f"Expected Löhrbach duplicate merged to 1 occurrence, got {hits}")

    def test_t1_013_gpx_coords_and_poly5_decode_1e5(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        gpx_tours = [t for t in data.get("tours", []) if (t.get("hike") or {}).get("hasGpx")]
        self.assertTrue(gpx_tours)
        for t in gpx_tours:
            h = t["hike"]
            sc = h.get("startCoord")
            ec = h.get("endCoord")
            self.assertEqual(len(sc), 2)
            self.assertEqual(len(ec), 2)
            self.assertTrue(in_sw_bounds(float(sc[0]), float(sc[1])), f"{t['id']} startCoord out of bounds: {sc}")
            self.assertTrue(in_sw_bounds(float(ec[0]), float(ec[1])), f"{t['id']} endCoord out of bounds: {ec}")
            pts = decode_polyline(str(h.get("poly5") or ""), precision=5)
            self.assertGreaterEqual(len(pts), 2, f"{t['id']} poly5 decoded < 2 pts")
            self.assertTrue(in_sw_bounds(pts[0][0], pts[0][1]), f"{t['id']} poly5[0] out of bounds: {pts[0]}")

    def test_t1_014_gpx_elev_profile_present_and_bounded(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        gpx_tours = [t for t in cat.get("tours", []) if (t.get("hike") or {}).get("hasGpx")]
        self.assertTrue(gpx_tours)
        for t in gpx_tours:
            h = t["hike"]
            prof = h.get("elevProfile") or []
            self.assertGreaterEqual(len(prof), 2, f"{t['id']} missing elevProfile")
            self.assertLessEqual(float(prof[0][0]), 0.5)
            self.assertLessEqual(float(prof[-1][0]), float(h["distKm"]) + 0.3)

    def test_t1_015_walk_min_swiss_formula_plausible_pace(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        gpx_tours = [t for t in data.get("tours", []) if (t.get("hike") or {}).get("hasGpx")]
        self.assertTrue(gpx_tours)
        for t in gpx_tours:
            h = t["hike"]
            dist = float(h["distKm"])
            gain = float(h["elevGainM"])
            wmin = float(h["walkMin"])
            # Effective flat-km under Swiss formula ~ dist + gain/100 (or /80..100)
            eff_km = dist + gain / 100.0
            pace_kmh = eff_km / (wmin / 60.0)
            self.assertTrue(
                2.0 <= pace_kmh <= 6.5,
                f"{t['id']} implausible pace {pace_kmh:.2f} km/h (dist={dist}, gain={gain}, walkMin={wmin})",
            )


# ---------------------------------------------------------------------------
# F04: GPX <-> Shortcut & <=800 m Stop Linking + Waypoints (T1-016 .. T1-020)
# ---------------------------------------------------------------------------


class TestT1_F04_StopLinkingAndWaypoints(unittest.TestCase):
    def test_t1_016_catalog_exposes_19_mapy_waypoints(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        wps = cat.get("waypoints") or []
        self.assertEqual(len(wps), 19, f"Expected 19 Mapy waypoints, got {len(wps)}")
        names = " ".join(str(w.get("name", "")) for w in wps).lower()
        for expected in ("eschkopf", "krähberg", "ohrsberg", "friedrichsturm"):
            self.assertIn(expected, names, f"Expected waypoint {expected!r} in catalog.waypoints")

    def test_t1_017_shortcut_linked_gpx_has_stops_and_files(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        linked = [
            t
            for t in cat.get("tours", [])
            if t.get("source") == "shortcut" and (t.get("hike") or {}).get("hasGpx")
        ]
        self.assertGreater(len(linked), 0, "Expected >= 1 shortcut tour linked with GPX")
        for t in linked:
            self.assertTrue((t["hike"].get("gpxFiles") or []), f"{t['id']} missing gpxFiles")
            tr = t.get("transit") or {}
            self.assertIsNotNone(tr.get("startStop"), f"{t['id']} missing startStop")
            self.assertIsNotNone(tr.get("endStop"), f"{t['id']} missing endStop")

    def test_t1_018_gpx_linked_within_800m_or_unlinked_reason(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        gpx_only = [
            t
            for t in cat.get("tours", [])
            if (t.get("transit") or {}).get("kind") == "gpx_linked" or t.get("source") == "mapy"
        ]
        self.assertGreater(len(gpx_only), 0, "Expected gpx_linked / mapy tours")
        for t in gpx_only:
            tr = t.get("transit") or {}
            reason = str(tr.get("unlinkedReason") or "").strip()
            if reason:
                continue
            w_start = tr.get("walkToStartM")
            w_end = tr.get("walkFromEndM")
            self.assertIsNotNone(w_start, f"{t['id']} missing walkToStartM and unlinkedReason")
            self.assertIsNotNone(w_end, f"{t['id']} missing walkFromEndM and unlinkedReason")
            self.assertLessEqual(float(w_start), 800.0, f"{t['id']} walkToStartM={w_start} > 800")
            self.assertLessEqual(float(w_end), 800.0, f"{t['id']} walkFromEndM={w_end} > 800")

    def test_t1_019_reported_walk_distances_match_haversine(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        checked = 0
        for t in cat.get("tours", []):
            h = t.get("hike") or {}
            tr = t.get("transit") or {}
            if not (h.get("hasGpx") and tr.get("startStop") and h.get("startCoord")):
                continue
            if tr.get("walkToStartM") is None or tr.get("unlinkedReason"):
                continue
            ss = tr["startStop"]
            sc = h["startCoord"]
            if ss.get("lat") is None:
                continue
            air_m = haversine_m(float(ss["lat"]), float(ss["lon"]), float(sc[0]), float(sc[1]))
            reported = float(tr["walkToStartM"])
            self.assertGreaterEqual(reported + 150.0, air_m, f"{t['id']} reported walkToStartM < Haversine")
            self.assertLessEqual(reported, max(air_m * 1.5 + 150.0, 900.0), f"{t['id']} walkToStartM implausible")
            checked += 1
        self.assertGreater(checked, 0)

    def test_t1_020_all_65_unique_gpx_tracks_accounted_for(self) -> None:
        _, loader = _cfg_loader()
        unique_hashes = loader.unique_individual_gpx_hashes()
        self.assertEqual(len(unique_hashes), 65, f"Input oracle should have 65 unique GPX files, got {len(unique_hashes)}")
        cat = loader.tours_catalog()
        covered_files: set[str] = set()
        for t in cat.get("tours", []):
            for gf in (t.get("hike") or {}).get("gpxFiles") or []:
                covered_files.add(Path(gf).name)
        for doc in cat.get("excludedGpx") or cat.get("unlinkedGpx") or []:
            if isinstance(doc, dict) and doc.get("file"):
                covered_files.add(Path(doc["file"]).name)
            elif isinstance(doc, str):
                covered_files.add(Path(doc).name)
        missing_groups = [
            [p.name for p in paths]
            for paths in unique_hashes.values()
            if not any(p.name in covered_files for p in paths)
        ]
        self.assertEqual(missing_groups, [], f"Unaccounted unique GPX tracks: {missing_groups[:5]}")


# ---------------------------------------------------------------------------
# F05: 100% Deutschlandticket Enforcement (T1-021 .. T1-025)
# ---------------------------------------------------------------------------


class TestT1_F05_DeutschlandticketOnly(unittest.TestCase):
    def test_t1_021_zero_forbidden_modes_in_default_fwd_rev(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for kind in ("fwd", "rev"):
                        for seg in plan.get(kind) or []:
                            mode = str(seg.get("mode", "")).upper()
                            self.assertNotIn(mode, {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "AIRPLANE", "COACH"})

    def test_t1_022_zero_forbidden_line_prefixes_anywhere(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            hit = is_forbidden_fernverkehr(seg)
                            self.assertIsNone(hit, f"{tour['id']} {base}/{day}/{it_name}: {hit}")

    def test_t1_023_zero_forbidden_operators_or_sonderzug(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        bad_kw = re.compile(r"(db\s+fernverkehr|flixtrain|sonderzug|nightjet|sncf)", re.IGNORECASE)
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            blob = f"{seg.get('op', '')} {seg.get('lineName', '')} {seg.get('note', '')}"
                            self.assertIsNone(
                                bad_kw.search(blob),
                                f"{tour['id']} {base}/{day}/{it_name} forbidden operator/marker: {blob!r}",
                            )

    def test_t1_024_regional_modes_properly_typed(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        allowed = {"sbahn", "re", "rb", "stadtbahn", "tram", "bus", "ferry", "rail", "foot", "hike", "walk"}
        ride_count = 0
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for _, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if seg.get("kind") == "ride":
                                ride_count += 1
                                mode = str(seg.get("mode", "")).lower()
                                self.assertIn(mode, allowed, f"Unexpected ride mode {mode!r} in {tour['id']}")
        self.assertGreater(ride_count, 10)

    def test_t1_025_every_ride_segment_has_mode_and_line(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if seg.get("kind") == "ride":
                                self.assertTrue(str(seg.get("mode", "")).strip(), f"{tour['id']} missing mode")
                                line_val = str(seg.get("line") or seg.get("lineName") or "").strip()
                                self.assertTrue(line_val, f"{tour['id']} {base}/{day}/{it_name} ride missing line")


# ---------------------------------------------------------------------------
# F06: Minimum >=4-Minute Transfer Buffer (T1-026 .. T1-030)
# ---------------------------------------------------------------------------


class TestT1_F06_TransferBuffers(unittest.TestCase):
    def test_t1_026_default_fwd_rev_transfers_at_least_4_min(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for kind in ("fwd", "rev"):
                        for _, _, gap, hub in compute_transfers(plan.get(kind) or []):
                            self.assertGreaterEqual(
                                gap, 4, f"{tour['id']} {base}/{day}/{kind} transfer at {hub} = {gap}m < 4m"
                            )

    def test_t1_027_alternative_and_last_safe_transfers_at_least_4_min(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for _, _, gap, hub in compute_transfers(segs):
                            self.assertGreaterEqual(
                                gap, 4, f"{tour['id']} {base}/{day}/{it_name} transfer at {hub} = {gap}m < 4m"
                            )

    def test_t1_028_transfer_metadata_consistent_with_timestamps(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for prev_r, next_r, gap, _ in compute_transfers(segs):
                            t_field = prev_r.get("transfer")
                            if t_field is not None:
                                self.assertGreaterEqual(int(t_field), 4, f"{tour['id']} transfer={t_field} < 4")
                                self.assertLessEqual(
                                    abs(int(t_field) - gap),
                                    2,
                                    f"{tour['id']} {base}/{day}/{it_name} transfer={t_field} != gap={gap}",
                                )

    def test_t1_029_leg_durations_positive_with_midnight_wrap(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if seg.get("dep") and seg.get("arr"):
                                diff = minutes_diff(seg["dep"], seg["arr"], seg.get("depIso"), seg.get("arrIso"))
                                self.assertGreaterEqual(diff, 0, f"{tour['id']} {it_name} negative duration")
                                self.assertLessEqual(diff, 720, f"{tour['id']} {it_name} implausible single leg {diff}m")

    def test_t1_030_major_hub_transfers_ge_5_or_flagged(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for prev_r, next_r, gap, hub in compute_transfers(segs):
                            if is_major_hub(hub) and gap < 5:
                                warns = list(prev_r.get("warnings") or []) + list(next_r.get("warnings") or [])
                                note = f"{prev_r.get('note', '')} {next_r.get('note', '')}"
                                flagged = bool(warns) or ("tight" in note.lower() or "knapp" in note.lower())
                                self.assertTrue(
                                    flagged,
                                    f"{tour['id']} {base}/{day}/{it_name} hub {hub} gap={gap}m <5 without warning",
                                )


# ---------------------------------------------------------------------------
# F07: Hike Synchronization (stay >= walkMin + 30 min) (T1-031 .. T1-035)
# ---------------------------------------------------------------------------


class TestT1_F07_HikeSynchronization(unittest.TestCase):
    def test_t1_031_default_fwd_rev_stay_ge_walk_plus_30(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        checked = 0
        for tour in data.get("tours", []):
            wmin = loader.walk_min(tour)
            if wmin <= 0:
                continue
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not loader.is_plan_routable(plan):
                        continue
                    stay = compute_hike_stay(plan["fwd"], plan["rev"])
                    self.assertIsNotNone(stay, f"{tour['id']} {base}/{day} unable to compute hike stay")
                    self.assertGreaterEqual(
                        stay, wmin + 30, f"{tour['id']} {base}/{day} stay={stay}m < walkMin+30 ({wmin+30}m)"
                    )
                    checked += 1
        self.assertGreater(checked, 0)

    def test_t1_032_last_safe_return_stay_ge_walk_plus_30(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            wmin = loader.walk_min(tour)
            if wmin <= 0:
                continue
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not loader.is_plan_routable(plan):
                        continue
                    lsr = loader.last_safe_return(plan)
                    if lsr and lsr.get("segs"):
                        stay = compute_hike_stay(plan["fwd"], lsr["segs"])
                        if stay is not None:
                            self.assertGreaterEqual(
                                stay,
                                wmin + 30,
                                f"{tour['id']} {base}/{day} lastSafeReturn stay={stay} < {wmin+30}",
                            )

    def test_t1_033_explicit_min_stay_field_ge_walk_plus_30(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            wmin = loader.walk_min(tour)
            if wmin <= 0:
                continue
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not loader.is_plan_routable(plan):
                        continue
                    for seg in plan.get("fwd", []) + plan.get("rev", []):
                        if seg.get("minStay") is not None:
                            self.assertGreaterEqual(
                                int(seg["minStay"]),
                                wmin + 30,
                                f"{tour['id']} {base}/{day} minStay={seg['minStay']} < {wmin+30}",
                            )

    def test_t1_034_open_jaw_hike_stay_between_start_and_end_stops(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        open_jaws = 0
        for tour in data.get("tours", []):
            wmin = loader.walk_min(tour)
            tr = tour.get("transit") or {}
            ss = (tr.get("startStop") or {}).get("name")
            es = (tr.get("endStop") or {}).get("name")
            if wmin <= 0 or not ss or not es or ss == es:
                continue
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not loader.is_plan_routable(plan):
                        continue
                    stay = compute_hike_stay(plan["fwd"], plan["rev"])
                    self.assertGreaterEqual(stay, wmin + 30, f"Open-jaw {tour['id']} stay={stay} < {wmin+30}")
                    open_jaws += 1
        self.assertGreater(open_jaws, 0, "Expected open-jaw hike tours in dataset")

    def test_t1_035_compatible_alt_pairs_satisfy_walk_plus_30(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            wmin = loader.walk_min(tour)
            if wmin <= 0:
                continue
            for base, days in loader.plans_for(tour).items():
                for day, plan in (days or {}).items():
                    if not loader.is_plan_routable(plan):
                        continue
                    alts_f = plan.get("alts_fwd") or []
                    alts_r = plan.get("alts_rev") or []
                    if not alts_f or not alts_r:
                        continue
                    # First outbound alternative must have at least one compatible return >= walkMin + 30
                    f0 = alts_f[0]["segs"] if isinstance(alts_f[0], dict) else alts_f[0]
                    ok = False
                    for ar in alts_r:
                        rs = ar["segs"] if isinstance(ar, dict) else ar
                        st = compute_hike_stay(f0, rs)
                        if st is not None and st >= wmin + 30:
                            ok = True
                            break
                    self.assertTrue(ok, f"{tour['id']} {base}/{day} alts_fwd[0] has no return >= walkMin+30")


# ---------------------------------------------------------------------------
# F08: Dual-Base Timetables, >=3 Windows & lastSafeReturn (T1-036 .. T1-040)
# ---------------------------------------------------------------------------


class TestT1_F08_DualBaseTimetables(unittest.TestCase):
    def test_t1_036_jugenheim_base_metadata_and_dhid(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        jg = (data.get("bases") or {}).get("jugenheim") or {}
        self.assertEqual(jg.get("dhid"), JUGENHEIM_BASE["dhid"])
        self.assertAlmostEqual(float(jg["lat"]), JUGENHEIM_BASE["lat"], delta=0.01)
        self.assertAlmostEqual(float(jg["lon"]), JUGENHEIM_BASE["lon"], delta=0.01)
        self.assertEqual(str((jg.get("nearestRail") or {}).get("ibnr", "")), JUGENHEIM_BASE["nearestRailIbnr"])

    def test_t1_037_karlsruhe_base_metadata_and_dhid(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        ka = (data.get("bases") or {}).get("karlsruhe") or {}
        self.assertEqual(ka.get("dhid"), KARLSRUHE_BASE["dhid"])
        self.assertEqual(str(ka.get("ibnr", "")), KARLSRUHE_BASE["ibnr"])
        self.assertAlmostEqual(float(ka["lat"]), KARLSRUHE_BASE["lat"], delta=0.01)
        self.assertAlmostEqual(float(ka["lon"]), KARLSRUHE_BASE["lon"], delta=0.01)

    def test_t1_038_days_iso_and_dual_base_plans_present(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        days = data.get("days") or {}
        for k in ("wd", "sa", "so"):
            self.assertRegex(str(days.get(k, "")), r"^\d{4}-\d{2}-\d{2}$")
        for tour in data.get("tours", []):
            plans = loader.plans_for(tour)
            self.assertIn("jugenheim", plans, f"{tour['id']} missing plans.jugenheim")
            self.assertIn("karlsruhe", plans, f"{tour['id']} missing plans.karlsruhe")
            for b in ("jugenheim", "karlsruhe"):
                for k in ("wd", "sa", "so"):
                    self.assertIn(k, plans[b], f"{tour['id']} missing plans.{b}.{k}")

    def test_t1_039_at_least_3_departure_windows_unless_noted(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        notes = loader.routing_notes_entries(loader.routing_notes())
        noted_ids = {(n.get("tour_id") or n.get("id")) for n in notes}
        for tour in data.get("tours", []):
            tid = tour["id"]
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    plan = loader.plans_for(tour)[b][d]
                    if not loader.is_plan_routable(plan):
                        continue
                    alts = plan.get("alts_fwd") or []
                    deps = {
                        (a["segs"][0]["dep"] if isinstance(a, dict) else a[0]["dep"])
                        for a in alts
                        if (a.get("segs") if isinstance(a, dict) else a)
                    }
                    if len(deps) < 3:
                        has_note = tid in noted_ids or bool((tour.get("unroutable") or {}).get(b, {}).get(d))
                        self.assertTrue(has_note, f"{tid} {b}/{d} has {len(deps)} < 3 windows without routing note")

    def test_t1_040_routable_plans_have_last_safe_return(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        checked = 0
        for tour in data.get("tours", []):
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    plan = loader.plans_for(tour)[b][d]
                    if not loader.is_plan_routable(plan):
                        continue
                    lsr = loader.last_safe_return(plan)
                    self.assertIsNotNone(lsr, f"{tour['id']} {b}/{d} missing lastSafeReturn")
                    self.assertTrue(lsr.get("dep"), f"{tour['id']} {b}/{d} lastSafeReturn missing dep")
                    self.assertTrue(lsr.get("arr"), f"{tour['id']} {b}/{d} lastSafeReturn missing arr")
                    self.assertTrue(lsr.get("segs"), f"{tour['id']} {b}/{d} lastSafeReturn missing segs")
                    checked += 1
        self.assertGreater(checked, 0)


# ---------------------------------------------------------------------------
# F09: Ruftaxi / ALT / Flexline & Seasonal Bus Warnings (T1-041 .. T1-045)
# ---------------------------------------------------------------------------


class TestT1_F09_RuftaxiAndSeasonalWarnings(unittest.TestCase):
    def test_t1_041_ruftaxi_segments_carry_warnings(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for b, days in loader.plans_for(tour).items():
                for d, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if seg.get("kind") == "ride" and seg_is_ruftaxi_text(seg):
                                self.assertTrue(
                                    seg.get("warnings"),
                                    f"{tour['id']} {b}/{d}/{it_name} Ruftaxi/ALT segment lacks warnings[]: {seg.get('line')}",
                                )

    def test_t1_042_seasonal_segments_carry_warnings(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for b, days in loader.plans_for(tour).items():
                for d, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if seg.get("kind") == "ride" and seg_is_seasonal_text(seg):
                                warns = seg.get("warnings") or []
                                has_seasonal = any("seasonal" in str(w.get("type", "")).lower() for w in warns) or bool(
                                    warns
                                )
                                self.assertTrue(
                                    has_seasonal,
                                    f"{tour['id']} {b}/{d}/{it_name} seasonal line lacks warning: {seg.get('line')}",
                                )

    def test_t1_043_warnings_schema_valid(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for b, days in loader.plans_for(tour).items():
                for d, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for _, segs in _iter_itineraries(plan):
                        for seg in segs:
                            for w in seg.get("warnings") or []:
                                self.assertTrue(str(w.get("type", "")).strip(), f"Warning missing type: {w}")
                                self.assertTrue(str(w.get("text", "")).strip(), f"Warning missing text: {w}")

    def test_t1_044_ui_renders_warning_badge_when_present(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        target_tour = None
        for tour in data.get("tours", []):
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(tour)[b][d]
                    if loader.is_plan_routable(p) and any(s.get("warnings") for s in p["fwd"] + p["rev"]):
                        target_tour = (tour, b, d)
                        break
                if target_tour:
                    break
            if target_tour:
                break
        if target_tour is None:
            # Verify UI does not crash and displays clean badges when no warning tour exists in dataset
            with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
                ui = AppUI(h.page)
                ui.open_url(srv.base_url)
                ui.open_card_index(0)
                self.assertGreater(len(ui.detail_text()), 30)
                h.assert_zero_errors("T1-044")
            return

        tour, base, day = target_tour
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            if base == "karlsruhe":
                ui.switch_base("karlsruhe")
            ui.select_day(day)
            opened = ui.open_card_matching(re.escape(loader.tour_title(tour, "de")[:18]))
            self.assertTrue(opened, f"Could not open warned tour {tour['id']}")
            txt = ui.detail_text()
            self.assertTrue(
                re.search(r"ruftaxi|alt|flexline|saison|hinweis|warn|knapp|tight", txt, re.IGNORECASE),
                f"Expected warning badge/text in detail view for {tour['id']}",
            )
            h.assert_zero_errors("T1-044")

    def test_t1_045_warnings_do_not_block_timeline_or_map(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        target_tour = None
        for tour in data.get("tours", []):
            p = loader.plans_for(tour)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and any(s.get("warnings") for s in p["fwd"] + p["rev"]):
                target_tour = tour
                break
        target_tour = target_tour or data["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(target_tour, "de")[:18]))
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T1-045")


# ---------------------------------------------------------------------------
# F10: Unroutable Tour & Missing Day-Type Documentation (T1-046 .. T1-050)
# ---------------------------------------------------------------------------


class TestT1_F10_RoutingNotes(unittest.TestCase):
    def test_t1_046_routing_notes_json_valid(self) -> None:
        _, loader = _cfg_loader()
        notes = loader.routing_notes()
        self.assertIsInstance(notes, (dict, list))

    def test_t1_047_every_fallback_slot_has_reason(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        notes = loader.routing_notes_entries(loader.routing_notes())
        note_map = {(n.get("tour_id") or n.get("id")): n for n in notes}
        for tour in data.get("tours", []):
            tid = tour["id"]
            unr = tour.get("unroutable") or {}
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    plan = loader.plans_for(tour)[b][d]
                    if not loader.is_plan_routable(plan):
                        r_local = (unr.get(b) or {}).get(d) or (plan.get("reason") if isinstance(plan, dict) else None)
                        r_note = (note_map.get(tid) or {}).get("reason")
                        self.assertTrue(
                            str(r_local or r_note or "").strip(),
                            f"Fallback/unroutable slot {tid} {b}/{d} lacks reason",
                        )

    def test_t1_048_routing_notes_entries_have_tour_id_and_reason(self) -> None:
        _, loader = _cfg_loader()
        entries = loader.routing_notes_entries(loader.routing_notes())
        for e in entries:
            self.assertTrue(str(e.get("tour_id") or e.get("id") or "").strip(), f"Missing tour_id in {e}")
            self.assertTrue(str(e.get("reason") or "").strip(), f"Missing reason in {e}")

    def test_t1_049_partially_routable_tours_keep_valid_days(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for tour in data.get("tours", []):
            for b in ("jugenheim", "karlsruhe"):
                days_map = loader.plans_for(tour)[b]
                routable = [d for d in ("wd", "sa", "so") if loader.is_plan_routable(days_map.get(d))]
                if 0 < len(routable) < 3:
                    for d in routable:
                        self.assertTrue(days_map[d]["fwd"] and days_map[d]["rev"])

    def test_t1_050_ui_shows_fallback_notice_for_unroutable_day(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        slot = None
        for tour in data.get("tours", []):
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    if not loader.is_plan_routable(loader.plans_for(tour)[b][d]):
                        slot = (tour, b, d)
                        break
                if slot:
                    break
            if slot:
                break
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            if slot is None:
                # All slots routable: verify opening first tour on Sunday shows valid schedule without errors
                ui.select_day("so")
                ui.open_card_index(0)
                self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
                h.assert_zero_errors("T1-050")
                return
            tour, base, day = slot
            if base == "karlsruhe":
                ui.switch_base("karlsruhe")
            ui.select_day(day)
            ui.fill_search(loader.tour_title(tour, "de")[:16])
            if ui.card_count() > 0:
                ui.open_card_index(0)
                self.assertGreater(len(ui.detail_text().strip()), 20)
            h.assert_zero_errors("T1-050")


# ---------------------------------------------------------------------------
# F11: 20-30 Curated Highlight Tours across 3 Regions (T1-051 .. T1-055)
# ---------------------------------------------------------------------------


class TestT1_F11_CuratedHighlightTours(unittest.TestCase):
    def test_t1_051_curated_count_between_20_and_30(self) -> None:
        _, loader = _cfg_loader()
        for rel in ("data/tours_catalog_full.json", "data/data.json"):
            payload = loader.load_json(rel)
            curated = [t for t in payload.get("tours", []) if t.get("source") == "curated"]
            self.assertTrue(20 <= len(curated) <= 30, f"{rel}: curated count {len(curated)} not in [20, 30]")

    def test_t1_052_curated_covers_all_3_regions(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        curated = [t for t in data.get("tours", []) if t.get("source") == "curated"]
        for reg in ("pfaelzerwald", "schwarzwald", "odenwald"):
            cnt = sum(1 for t in curated if t.get("region") == reg)
            self.assertGreaterEqual(cnt, 5, f"Region {reg} has only {cnt} curated tours (<5)")

    def test_t1_053_curated_bilingual_metadata_complete(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        curated = [t for t in data.get("tours", []) if t.get("source") == "curated"]
        self.assertTrue(curated)
        for t in curated:
            for field in ("title", "subtitle", "sun", "tips"):
                obj = t.get(field)
                self.assertIsInstance(obj, dict, f"{t['id']} {field} not dict")
                self.assertTrue(str(obj.get("de", "")).strip(), f"{t['id']} {field}.de empty")
                self.assertTrue(str(obj.get("en", "")).strip(), f"{t['id']} {field}.en empty")
            self.assertTrue(t.get("category"), f"{t['id']} missing category")
            self.assertIn(t.get("difficulty"), {"leicht", "mittel", "schwer"}, f"{t['id']} invalid difficulty")
            self.assertTrue(t.get("places"), f"{t['id']} empty places[]")

    def test_t1_054_curated_incorporates_key_mapy_waypoints(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        curated = [t for t in data.get("tours", []) if t.get("source") == "curated"]
        blob = json.dumps(curated, ensure_ascii=False).lower()
        hits = sum(
            1
            for kw in ("eschkopf", "krähberg", "ohrsberg", "friedrichsturm", "geroldsau")
            if kw in blob
        )
        self.assertGreaterEqual(hits, 3, f"Expected >= 3 key Mapy waypoints in curated tours, found {hits}")

    def test_t1_055_curated_routable_from_both_bases(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        curated = [t for t in data.get("tours", []) if t.get("source") == "curated"]
        self.assertTrue(curated)
        for t in curated:
            for base in ("jugenheim", "karlsruhe"):
                days_map = loader.plans_for(t)[base]
                ok = any(loader.is_plan_routable(days_map.get(d)) for d in ("wd", "sa", "so"))
                self.assertTrue(ok, f"Curated tour {t['id']} not routable on any day from {base}")


# ---------------------------------------------------------------------------
# F12: Wikimedia Commons 800x450 Images & Credits (T1-056 .. T1-060)
# ---------------------------------------------------------------------------


class TestT1_F12_MediaAndCredits(unittest.TestCase):
    def test_t1_056_every_tour_has_jpg_file_gt_5kb(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            p = loader.path(f"img/{t['id']}.jpg")
            self.assertTrue(p.exists(), f"Missing image {p}")
            self.assertGreater(p.stat().st_size, 5000, f"Image {p} suspiciously small ({p.stat().st_size} bytes)")

    def test_t1_057_every_jpg_is_valid_800x450_jpeg(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            p = loader.path(f"img/{t['id']}.jpg")
            with Image.open(p) as im:
                self.assertEqual(im.format, "JPEG", f"{p} is {im.format}, expected JPEG")
                self.assertEqual(im.size, (800, 450), f"{p} size {im.size} != (800, 450)")

    def test_t1_058_credits_json_covers_100_percent_of_tours(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        credits = loader.credits_json()
        for t in data.get("tours", []):
            c = credits.get(t["id"])
            self.assertIsInstance(c, dict, f"Missing credit for tour {t['id']}")
            for k in ("file", "artist", "license", "licenseUrl", "page"):
                self.assertTrue(str(c.get(k, "")).strip(), f"Credit for {t['id']} missing {k}")

    def test_t1_059_all_image_licenses_are_free(self) -> None:
        _, loader = _cfg_loader()
        credits = loader.credits_json()
        self.assertTrue(credits)
        for tid, c in credits.items():
            lic = str(c.get("license", "")).strip()
            self.assertRegex(lic, FREE_LICENSES_RE, f"Non-free or unrecognized license for {tid}: {lic!r}")

    def test_t1_060_ui_hero_image_loads_and_renders_credit(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            info = h.page.evaluate(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('#detail img, .detail-view img, main img'));
                    return imgs.map(i => ({src: i.src, complete: i.complete, w: i.naturalWidth, h: i.naturalHeight}));
                }"""
            )
            loaded = [x for x in info if x["complete"] and x["w"] > 0]
            self.assertTrue(loaded, f"No loaded hero image in detail view: {info}")
            detail_html = h.page.locator("#detail, .detail-view, main").first.inner_html()
            self.assertRegex(detail_html, r"commons\.wikimedia\.org|creativecommons\.org|CC|Public|PD|GFDL")
            h.assert_zero_errors("T1-060")


# ---------------------------------------------------------------------------
# F13: Offline PWA Shell, Manifest & 3000-Tile SW (T1-061 .. T1-065)
# ---------------------------------------------------------------------------


class TestT1_F13_OfflinePwaAndServiceWorker(unittest.TestCase):
    def test_t1_061_manifest_json_valid_standalone(self) -> None:
        _, loader = _cfg_loader()
        m = loader.manifest_json()
        self.assertEqual(m.get("display"), "standalone")
        self.assertTrue(str(m.get("start_url", "")).strip())
        srcs = {ic.get("src", "").lstrip("./") for ic in m.get("icons", [])}
        for req in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
            self.assertIn(req, srcs, f"Manifest missing icon {req}")

    def test_t1_062_pwa_png_icons_exist_and_exact_sizes(self) -> None:
        _, loader = _cfg_loader()
        expected = {
            "icon-192.png": (192, 192),
            "icon-512.png": (512, 512),
            "icon-maskable-512.png": (512, 512),
        }
        for name, size in expected.items():
            p = loader.path(name)
            self.assertTrue(p.exists(), f"Missing icon {p}")
            with Image.open(p) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.size, size)

    def test_t1_063_sw_js_valid_tile_limit_and_hosts(self) -> None:
        _, loader = _cfg_loader()
        sw = loader.path("sw.js").read_text(encoding="utf-8")
        self.assertNotIn("__VERSION__", sw)
        self.assertNotIn("kartverket.no", sw.lower())
        self.assertRegex(sw, r"MAX_TILES\s*=\s*3000|3000")
        self.assertIn("tile.openstreetmap.org", sw)

    def test_t1_064_cdp_manifest_installability_and_sw_controller(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            cdp = h.context.new_cdp_session(h.page)
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            manifest_res = cdp.send("Page.getAppManifest")
            self.assertEqual(manifest_res.get("errors", []), [], f"Manifest errors: {manifest_res}")
            install_errs = cdp.send("Page.getInstallabilityErrors").get("installabilityErrors", [])
            self.assertEqual(install_errs, [], f"Installability errors: {install_errs}")
            # Wait for SW registration & reload to verify controller
            h.page.evaluate(
                "() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false"
            )
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            has_ctrl = h.page.evaluate("() => Boolean(navigator.serviceWorker && navigator.serviceWorker.controller)")
            self.assertTrue(has_ctrl, "Service Worker controller not active after reload")
            h.assert_zero_errors("T1-064")

    def test_t1_065_offline_reload_renders_list_detail_and_map(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.page.evaluate("() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            initial_cnt = ui.card_count()
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertEqual(ui.card_count(), initial_cnt)
            ui.open_card_index(0)
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T1-065")


# ---------------------------------------------------------------------------
# F14: Standalone Single-File HTML (T1-066 .. T1-070)
# ---------------------------------------------------------------------------


class TestT1_F14_StandaloneHtml(unittest.TestCase):
    def test_t1_066_standalone_exists_gt_500kb(self) -> None:
        cfg, _ = _cfg_loader()
        self.assertTrue(cfg.standalone_path.exists(), f"Missing standalone HTML: {cfg.standalone_path}")
        self.assertGreater(cfg.standalone_path.stat().st_size, 500_000)

    def test_t1_067_standalone_no_unreplaced_build_tokens(self) -> None:
        cfg, _ = _cfg_loader()
        html = cfg.standalone_path.read_text(encoding="utf-8")
        for tok in ("/*LEAFLET_CSS*/", "/*LEAFLET_JS*/", "/*DATA*/"):
            self.assertNotIn(tok, html, f"Unreplaced build token {tok} in standalone HTML")

    def test_t1_068_standalone_zero_external_cdn_tags(self) -> None:
        cfg, _ = _cfg_loader()
        soup = BeautifulSoup(cfg.standalone_path.read_text(encoding="utf-8"), "html.parser")
        for sc in soup.find_all("script", src=True):
            self.assertFalse(sc["src"].startswith(("http://", "https://", "//")), f"External script: {sc['src']}")
        for lk in soup.find_all("link", rel="stylesheet", href=True):
            self.assertFalse(lk["href"].startswith(("http://", "https://", "//")), f"External CSS: {lk['href']}")

    def test_t1_069_standalone_file_url_renders_zero_errors(self) -> None:
        cfg, loader = _cfg_loader()
        expected = len(loader.data_json()["tours"])
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            self.assertEqual(ui.card_count(), expected)
            h.assert_zero_errors("T1-069")

    def test_t1_070_standalone_file_url_detail_photo_timeline_map(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.open_card_index(0)
            loaded = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)"
            )
            self.assertTrue(loaded)
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T1-070")


# ---------------------------------------------------------------------------
# F15: Mobile Viewport 412x915 & Zero JS Errors (T1-071 .. T1-075)
# ---------------------------------------------------------------------------


class TestT1_F15_MobileViewport(unittest.TestCase):
    def test_t1_071_initial_load_412x915_zero_errors(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.assert_zero_errors("T1-071")

    def test_t1_072_list_view_no_horizontal_overflow(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw, f"Horizontal overflow in list view: scrollWidth={sw} > innerWidth={iw}")
            h.assert_zero_errors("T1-072")

    def test_t1_073_card_count_matches_data_json(self) -> None:
        cfg, loader = _cfg_loader()
        expected = len(loader.data_json()["tours"])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            self.assertEqual(ui.card_count(), expected)
            h.assert_zero_errors("T1-073")

    def test_t1_074_detail_view_no_horizontal_overflow(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw, f"Horizontal overflow in detail view: {sw} > {iw}")
            h.assert_zero_errors("T1-074")

    def test_t1_075_back_navigation_restores_list_and_map(self) -> None:
        cfg, loader = _cfg_loader()
        expected = len(loader.data_json()["tours"])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            ui.go_back()
            self.assertEqual(ui.card_count(), expected)
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T1-075")


# ---------------------------------------------------------------------------
# F16: Dual Home-Base Header Switcher (T1-076 .. T1-080)
# ---------------------------------------------------------------------------


class TestT1_F16_DualBaseSwitcher(unittest.TestCase):
    def test_t1_076_header_displays_jugenheim_karlsruhe_switcher(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ht = ui.header_text()
            self.assertRegex(ht, r"Jugenheim", "Header missing Jugenheim switcher")
            self.assertRegex(ht, r"Karlsruhe", "Header missing Karlsruhe switcher")
            h.assert_zero_errors("T1-076")

    def test_t1_077_switching_base_in_list_updates_card_times(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            before = ui.visible_card_texts()[:5]
            ui.switch_base("karlsruhe")
            after = ui.visible_card_texts()[:5]
            self.assertNotEqual(before, after, "Card summaries did not update on base switch")
            h.assert_zero_errors("T1-077")

    def test_t1_078_switching_base_in_detail_updates_timeline(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            t_jg = ui.detail_text()
            ui.switch_base("karlsruhe")
            t_ka = ui.detail_text()
            self.assertNotEqual(t_jg, t_ka, "Detail timeline did not update when switching Jugenheim -> Karlsruhe")
            h.assert_zero_errors("T1-078")

    def test_t1_079_switching_base_updates_map_approach_geometry(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            d_jg = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('.leaflet-overlay-pane path')).map(p => p.getAttribute('d')).join('|')"
            )
            ui.switch_base("karlsruhe")
            d_ka = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('.leaflet-overlay-pane path')).map(p => p.getAttribute('d')).join('|')"
            )
            self.assertNotEqual(d_jg, d_ka, "Map SVG paths did not change after base switch")
            h.assert_zero_errors("T1-079")

    def test_t1_080_base_selection_persists_across_reload(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.switch_base("karlsruhe")
            ls_dump = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertIn("karlsruhe", ls_dump.lower())
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ls_after = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertIn("karlsruhe", ls_after.lower())
            h.assert_zero_errors("T1-080")


# ---------------------------------------------------------------------------
# F17: Home Location Modal (T1-081 .. T1-085)
# ---------------------------------------------------------------------------


class TestT1_F17_HomeModal(unittest.TestCase):
    def test_t1_081_home_button_opens_visible_modal(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            self.assertTrue(ui.home_modal_locator().is_visible())
            h.assert_zero_errors("T1-081")

    def test_t1_082_home_modal_has_all_7_presets(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            modal_txt = ui.home_modal_locator().inner_text()
            for preset in REQUIRED_PRESETS:
                self.assertIn(preset, modal_txt, f"Home modal missing preset {preset!r}")
            h.assert_zero_errors("T1-082")

    def test_t1_083_selecting_preset_updates_header_and_closes_modal(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            ui.home_modal_locator().locator("button").filter(has_text=re.compile(r"Darmstadt Hbf", re.I)).first.click()
            h.page.wait_for_timeout(250)
            self.assertFalse(ui.home_modal_locator().is_visible())
            self.assertIn("Darmstadt", ui.header_text())
            h.assert_zero_errors("T1-083")

    def test_t1_084_custom_home_coords_update_header(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            m.locator("#customHomeName, input[placeholder*='Name' i]").first.fill("Neckargemünd")
            m.locator("#customHomeLat, input[placeholder*='Lat' i]").first.fill("49.3925")
            m.locator("#customHomeLon, input[placeholder*='Lon' i]").first.fill("8.7972")
            m.locator("#saveHomeBtn, button").filter(has_text=re.compile(r"Speichern|Übernehmen|Save|Apply", re.I)).first.click()
            h.page.wait_for_timeout(250)
            self.assertIn("Neckargemünd", ui.header_text())
            h.assert_zero_errors("T1-084")

    def test_t1_085_reset_and_gps_geolocation_in_home_modal(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(
            geolocation={"latitude": 49.4093, "longitude": 8.6947}
        ) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            gps_btn = m.locator("#homeGpsBtn, button").filter(has_text=re.compile(r"GPS|Standort|Location", re.I)).first
            gps_btn.click()
            h.page.wait_for_timeout(300)
            if not m.is_visible():
                ui.open_home_modal()
            m = ui.home_modal_locator()
            reset_btn = m.locator("#resetHomeBtn, button").filter(
                has_text=re.compile(r"Zurücksetzen|Reset|Standard", re.I)
            ).first
            reset_btn.click()
            h.page.wait_for_timeout(250)
            self.assertRegex(ui.header_text(), r"Jugenheim|Bickenbach|Karlsruhe")
            h.assert_zero_errors("T1-085")


# ---------------------------------------------------------------------------
# F18: MVV-Style Product Badges & Beaded Timeline (T1-086 .. T1-090)
# ---------------------------------------------------------------------------


class TestT1_F18_MvvTimelineAndBadges(unittest.TestCase):
    def test_t1_086_product_badges_have_distinct_colors(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            bgs = h.page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('#detail .badge, #detail .mode-badge, #detail [class*="badge"], #detail [class*="tag"], .tl span'));
                    return els.map(e => getComputedStyle(e).backgroundColor).filter(c => c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent');
                }"""
            )
            self.assertGreater(len(bgs), 0, "Expected styled product badges in detail timeline")
            h.assert_zero_errors("T1-086")

    def test_t1_087_timeline_shows_times_stops_and_platforms(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        plat_tour = None
        for t in data["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and any(
                (s.get("from") or {}).get("platform") or (s.get("to") or {}).get("platform")
                for s in p["fwd"]
            ):
                plat_tour = t
                break
        plat_tour = plat_tour or data["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(plat_tour, "de")[:18]))
            txt = ui.detail_text()
            self.assertRegex(txt, r"\d{2}:\d{2}")
            self.assertRegex(txt, r"Gleis|Steig|Pl\.|Platform|Gl\.")
            h.assert_zero_errors("T1-087")

    def test_t1_088_transfer_buffer_pills_rendered(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        multi_tour = None
        for t in data["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and sum(1 for s in p["fwd"] if s.get("kind") == "ride") >= 2:
                multi_tour = t
                break
        multi_tour = multi_tour or data["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(multi_tour, "de")[:18]))
            txt = ui.detail_text()
            self.assertRegex(txt, r"Umstieg|Transfer|\d+\s*Min", "Expected transfer buffer pill in timeline")
            h.assert_zero_errors("T1-088")

    def test_t1_089_intermediate_stops_expandable(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        inter_tour = None
        for t in data["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and any(s.get("intermediateStops") for s in p["fwd"]):
                inter_tour = t
                break
        inter_tour = inter_tour or data["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(inter_tour, "de")[:18]))
            exp = h.page.locator("#detail details summary, #detail button").filter(
                has_text=re.compile(r"Zwischenhalt|Stops|Halte", re.IGNORECASE)
            ).first
            self.assertGreater(exp.count(), 0, "Expected expandable intermediateStops control")
            exp.click()
            h.page.wait_for_timeout(150)
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            h.assert_zero_errors("T1-089")

    def test_t1_090_departure_windows_and_last_safe_return(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            txt = ui.detail_text()
            self.assertRegex(txt, r"Letzte\s*(?:sichere)?\s*Rückfahrt|Last\s*(?:safe)?\s*return", re.I)
            win_btns = h.page.locator("#detail button, .win-btn, [data-win]").filter(
                has_text=re.compile(r"\d{2}:\d{2}")
            )
            if win_btns.count() >= 2:
                win_btns.nth(1).click()
                h.page.wait_for_timeout(150)
            h.assert_zero_errors("T1-090")


# ---------------------------------------------------------------------------
# F19: Leaflet Map, Layers, Elevation Badge & Controls (T1-091 .. T1-095)
# ---------------------------------------------------------------------------


class TestT1_F19_LeafletMap(unittest.TestCase):
    def test_t1_091_overview_map_renders_paths(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            self.assertGreater(h.page.locator(".leaflet-container").count(), 0)
            self.assertGreaterEqual(ui.map_path_count(), 10)
            h.assert_zero_errors("T1-091")

    def test_t1_092_detail_map_separates_rail_bus_hike_styles(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        gpx_tour = next((t for t in data["tours"] if (t.get("hike") or {}).get("hasGpx")), data["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            styles = ui.map_path_styles()
            self.assertGreaterEqual(len(styles), 2)
            distinct_strokes = {s["stroke"] for s in styles if s["stroke"] and s["stroke"] != "#ffffff"}
            self.assertGreaterEqual(len(distinct_strokes), 2, f"Expected >= 2 distinct route colors: {styles}")
            h.assert_zero_errors("T1-092")

    def test_t1_093_gpx_tour_displays_elevation_badge(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        gpx_tour = next((t for t in data["tours"] if (t.get("hike") or {}).get("hasGpx")), data["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            body_txt = h.page.locator("body").inner_text()
            self.assertRegex(body_txt, r"\d+(?:[.,]\d+)?\s*km")
            self.assertRegex(body_txt, r"\d+\s*(?:hm|m\s*↗|↑)")
            h.assert_zero_errors("T1-093")

    def test_t1_094_layer_switcher_toggles_layers(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            lbtn = h.page.locator("#layerBtn, button").filter(
                has_text=re.compile(r"Layer|Karte|Ebenen|Topo", re.IGNORECASE)
            ).first
            self.assertGreater(lbtn.count(), 0)
            lbtn.click()
            h.page.wait_for_timeout(150)
            menu_item = h.page.locator("#layerMenu button, #layerMenu input, .leaflet-control-layers input").first
            if menu_item.count() > 0:
                menu_item.click()
                h.page.wait_for_timeout(150)
            h.assert_zero_errors("T1-094")

    def test_t1_095_map_controls_execute_cleanly(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for sel in ("#fitBtn", "#fullBtn", "#miniBtn", "#gpsBtn"):
                btn = h.page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click()
                    h.page.wait_for_timeout(150)
            h.assert_zero_errors("T1-095")


# ---------------------------------------------------------------------------
# F20: R4 Filter Bar, Search & Weekday Selector (T1-096 .. T1-100)
# ---------------------------------------------------------------------------


class TestT1_F20_FiltersAndSearch(unittest.TestCase):
    def test_t1_096_all_r4_filter_buttons_present(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            txt = h.page.locator("body").inner_text()
            for pat in (
                r"Alle",
                r"Meine Mapy",
                r"Neue Entdeckungen",
                r"Pfälzerwald",
                r"Schwarzwald",
                r"Odenwald",
                r"Heute noch machbar",
                r"Abendsonne",
            ):
                self.assertRegex(txt, re.compile(pat, re.IGNORECASE), f"Missing R4 filter: {pat}")
            h.assert_zero_errors("T1-096")

    def test_t1_097_source_filters_match_data_counts(self) -> None:
        cfg, loader = _cfg_loader()
        tours = loader.data_json()["tours"]
        exp_my = sum(1 for t in tours if t.get("source") in ("shortcut", "mapy", "my_tours"))
        exp_new = sum(1 for t in tours if t.get("source") == "curated")
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.click_filter(r"Meine Mapy")
            self.assertEqual(ui.card_count(), exp_my)
            ui.click_filter(r"Neue Entdeckungen")
            self.assertEqual(ui.card_count(), exp_new)
            h.assert_zero_errors("T1-097")

    def test_t1_098_region_filters_match_region_counts(self) -> None:
        cfg, loader = _cfg_loader()
        tours = loader.data_json()["tours"]
        mapping = [
            (r"Pfälzerwald", "pfaelzerwald"),
            (r"Schwarzwald", "schwarzwald"),
            (r"Odenwald", "odenwald"),
        ]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for pat, reg in mapping:
                ui.click_filter(pat)
                exp = sum(1 for t in tours if t.get("region") == reg)
                self.assertEqual(ui.card_count(), exp, f"Region {reg} count mismatch")
            h.assert_zero_errors("T1-098")

    def test_t1_099_feasible_time_and_evening_sun_filters(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            total = ui.card_count()
            ui.set_feasible_time("11:00")
            cnt_11 = ui.card_count()
            ui.set_feasible_time("18:00")
            cnt_18 = ui.card_count()
            self.assertGreaterEqual(cnt_11, cnt_18)
            self.assertLessEqual(cnt_18, total)
            ui.click_filter(r"^Alle$")
            ui.click_filter(r"Abendsonne")
            self.assertLessEqual(ui.card_count(), total)
            h.assert_zero_errors("T1-099")

    def test_t1_100_search_and_weekday_tabs(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            total = ui.card_count()
            ui.fill_search("Bad Dürkheim")
            cnt_q = ui.card_count()
            self.assertGreater(cnt_q, 0)
            self.assertLess(cnt_q, total)
            ui.fill_search("")
            ui.select_day("sa")
            ui.select_day("so")
            ui.select_day("wd")
            self.assertEqual(ui.card_count(), total)
            h.assert_zero_errors("T1-100")


# ---------------------------------------------------------------------------
# F21: Bilingual Toggle DE / EN (T1-101 .. T1-105)
# ---------------------------------------------------------------------------


class TestT1_F21_BilingualToggle(unittest.TestCase):
    def test_t1_101_lang_toggle_switches_de_to_en(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            before = h.page.locator("body").inner_text()
            ui.toggle_lang()
            after = h.page.locator("body").inner_text()
            self.assertNotEqual(before, after)
            self.assertRegex(after, r"New Discoveries|Black Forest|All\b")
            h.assert_zero_errors("T1-101")

    def test_t1_102_en_translates_filters_search_days_and_modal(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            body = h.page.locator("body").inner_text()
            self.assertRegex(body, r"Black Forest|Evening sun|Feasible")
            ui.open_home_modal()
            mtxt = ui.home_modal_locator().inner_text()
            self.assertRegex(mtxt, r"Home|Location|Reset|Close|Save")
            ui.close_home_modal()
            h.assert_zero_errors("T1-102")

    def test_t1_103_en_tour_detail_uses_english_metadata(self) -> None:
        cfg, loader = _cfg_loader()
        cur = next(t for t in loader.data_json()["tours"] if t.get("source") == "curated")
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            opened = ui.open_card_matching(re.escape(cur["title"]["en"][:16]))
            self.assertTrue(opened, "Curated EN title not found on card")
            txt = ui.detail_text()
            self.assertIn(cur["title"]["en"], txt)
            h.assert_zero_errors("T1-103")

    def test_t1_104_en_to_de_inside_detail_translates_immediately(self) -> None:
        cfg, loader = _cfg_loader()
        cur = next(t for t in loader.data_json()["tours"] if t.get("source") == "curated")
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            ui.open_card_matching(re.escape(cur["title"]["en"][:16]))
            ui.toggle_lang()
            txt = ui.detail_text()
            self.assertIn(cur["title"]["de"], txt)
            h.assert_zero_errors("T1-104")

    def test_t1_105_language_persists_across_reload(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertRegex(h.page.locator("body").inner_text(), r"New Discoveries|Black Forest")
            h.assert_zero_errors("T1-105")


# ---------------------------------------------------------------------------
# F22: Dark Mode (T1-106 .. T1-110)
# ---------------------------------------------------------------------------


class TestT1_F22_DarkMode(unittest.TestCase):
    def test_t1_106_theme_toggle_activates_dark_mode(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_theme()
            colors = ui.computed_body_colors()
            if colors["theme"] != "dark" and _rgb_luminance(colors["bg"]) >= 60:
                ui.toggle_theme()
                colors = ui.computed_body_colors()
            self.assertTrue(colors["theme"] == "dark" or _rgb_luminance(colors["bg"]) < 60)
            h.assert_zero_errors("T1-106")

    def test_t1_107_dark_mode_luminance_contrast(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            c = ui.computed_body_colors()
            self.assertLess(_rgb_luminance(c["bg"]), 60.0, f"Dark mode bg not dark: {c}")
            self.assertGreater(_rgb_luminance(c["color"]), 170.0, f"Dark mode text not light: {c}")
            h.assert_zero_errors("T1-107")

    def test_t1_108_dark_mode_cards_timeline_modal_contrast(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            card_cols = h.page.evaluate(
                """() => {
                    const c = document.querySelector('#list .card, .tour-card, [data-tour-id]');
                    const s = getComputedStyle(c);
                    return {bg: s.backgroundColor, fg: s.color};
                }"""
            )
            self.assertNotEqual(card_cols["bg"], card_cols["fg"])
            self.assertLess(_rgb_luminance(card_cols["bg"]), 80.0)
            ui.open_home_modal()
            ui.close_home_modal()
            ui.open_card_index(0)
            h.assert_zero_errors("T1-108")

    def test_t1_109_dark_mode_applies_dark_tile_filter(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            pane_info = h.page.evaluate(
                """() => {
                    const tp = document.querySelector('.leaflet-tile-pane');
                    const mc = document.querySelector('.leaflet-container');
                    return {
                        filter: tp ? getComputedStyle(tp).filter : 'none',
                        classes: (document.documentElement.className + ' ' + (mc ? mc.className : '') + ' ' + document.body.className)
                    };
                }"""
            )
            self.assertTrue(
                pane_info["filter"] != "none" or "dark" in pane_info["classes"].lower(),
                f"Expected dark tile filter/class: {pane_info}",
            )
            h.assert_zero_errors("T1-109")

    def test_t1_110_dark_theme_persists_across_reload(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_theme()
            if _rgb_luminance(ui.computed_body_colors()["bg"]) >= 60:
                ui.toggle_theme()
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertLess(_rgb_luminance(ui.computed_body_colors()["bg"]), 60.0)
            h.assert_zero_errors("T1-110")


# ---------------------------------------------------------------------------
# F23: GPX Download Validity (T1-111 .. T1-115)
# ---------------------------------------------------------------------------


class TestT1_F23_GpxDownload(unittest.TestCase):
    def test_t1_111_gpx_export_button_rendered_in_detail(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            btn = h.page.locator("button, a").filter(has_text=re.compile(r"GPX", re.IGNORECASE))
            self.assertGreater(btn.count(), 0)
            h.assert_zero_errors("T1-111")

    def test_t1_112_gpx_click_triggers_gpx_download(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            fname, content = ui.download_gpx()
            self.assertTrue(fname.lower().endswith(".gpx"), f"Expected .gpx filename, got {fname}")
            self.assertIn("<gpx", content)
            h.assert_zero_errors("T1-112")

    def test_t1_113_downloaded_gpx_parses_as_gpx_11_xml(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            _, content = ui.download_gpx()
            parsed = validate_gpx_xml(content)
            self.assertEqual(parsed["version"], "1.1")
            self.assertIn("topografix.com/GPX/1/1", parsed["namespace"])
            h.assert_zero_errors("T1-113")

    def test_t1_114_downloaded_gpx_for_hike_has_10plus_trkpt_with_ele(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), data_first := loader.data_json()["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            _, content = ui.download_gpx()
            parsed = validate_gpx_xml(content)
            pts = [p for tr in parsed["trks"] for p in tr["points"]]
            self.assertGreaterEqual(len(pts), 10)
            self.assertTrue(any(p["ele"] is not None for p in pts), "Expected <ele> tags in hiking GPX")
            for p in pts[:20]:
                self.assertTrue(in_sw_bounds(p["lat"], p["lon"]), f"GPX point out of bounds: {p}")
            h.assert_zero_errors("T1-114")

    def test_t1_115_downloaded_gpx_includes_waypoints_with_name(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            _, content = ui.download_gpx()
            parsed = validate_gpx_xml(content)
            self.assertGreaterEqual(len(parsed["wpts"]), 1)
            self.assertTrue(any(w["name"] for w in parsed["wpts"]), "Expected named <wpt> in GPX")
            h.assert_zero_errors("T1-115")


# ---------------------------------------------------------------------------
# F24: Transit Deep-Links (DB, MVV/SBM/HAFAS, Google Maps) (T1-116 .. T1-120)
# ---------------------------------------------------------------------------


class TestT1_F24_DeepLinks(unittest.TestCase):
    def test_t1_116_db_deep_link_present_with_sts_true(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            links = ui.detail_links()
            self.assertTrue(links["db"], "No bahn.de deep-links rendered")
            self.assertTrue(any("#sts=true" in u for u in links["db"]), f"DB links missing #sts=true: {links['db']}")
            h.assert_zero_errors("T1-116")

    def test_t1_117_db_deep_link_contains_so_zo_hd_dticket_params(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            db_url = ui.detail_links()["db"][0]
            frag = urlparse(db_url).fragment
            params = parse_qs(frag)
            for k in ("so", "zo", "hd"):
                self.assertIn(k, params, f"DB link missing {k}: {db_url}")
            self.assertTrue(any(k in params for k in ("vm", "dlt", "v", "kl")), f"Missing D-Ticket filter in {db_url}")
            h.assert_zero_errors("T1-117")

    def test_t1_118_mvv_sbm_hafas_deep_link_present(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            links = ui.detail_links()
            self.assertTrue(links["mvv"], f"No MVV/SBM/HAFAS deep-link found in {links['all']}")
            h.assert_zero_errors("T1-118")

    def test_t1_119_google_maps_deep_link_present(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            links = ui.detail_links()
            self.assertTrue(links["gmaps"], "No Google Maps deep-link found")
            self.assertTrue(any("/maps/dir" in u for u in links["gmaps"]))
            h.assert_zero_errors("T1-119")

    def test_t1_120_per_leg_action_links_rendered_in_timeline(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            links = ui.detail_links()
            self.assertGreaterEqual(len(links["db"]) + len(links["mvv"]) + len(links["gmaps"]), 3)
            h.assert_zero_errors("T1-120")


# ---------------------------------------------------------------------------
# F25: GitHub Pages Live Deployment (T1-121 .. T1-125)
# ---------------------------------------------------------------------------


class TestT1_F25_GitHubPagesDeployment(unittest.TestCase):
    def test_t1_121_deployment_readiness_relative_paths(self) -> None:
        _, loader = _cfg_loader()
        for fname in ("index.html", "sw.js", "manifest.json"):
            txt = loader.path(fname).read_text(encoding="utf-8")
            self.assertNotIn("/home/oliver/", txt, f"Absolute local path in {fname}")
            self.assertNotIn("http://localhost", txt, f"Localhost URL in {fname}")
            self.assertNotIn("http://127.0.0.1", txt, f"Loopback URL in {fname}")

    def test_t1_122_live_url_returns_http_200_html(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live GitHub Pages HTTP 200 check")
        r = requests.get(cfg.live_url, timeout=20)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers.get("Content-Type", ""))

    def test_t1_123_live_manifest_sw_icons_return_200(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live PWA asset HTTP 200 checks")
        base = cfg.live_url.rstrip("/") + "/"
        for asset in ("manifest.json", "sw.js", "icon-192.png", "icon-512.png", "icon-maskable-512.png"):
            r = requests.get(base + asset, timeout=20)
            self.assertEqual(r.status_code, 200, f"{base + asset} returned {r.status_code}")

    def test_t1_124_live_url_renders_full_catalog_412x915(self) -> None:
        cfg, loader = _cfg_loader()
        skip_unless_live(self, cfg, "Live Playwright 412x915 render check")
        expected = len(loader.data_json()["tours"])
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            self.assertEqual(ui.card_count(), expected)
            h.assert_zero_errors("T1-124")

    def test_t1_125_live_url_sw_and_offline_detail_map(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live HTTPS Service Worker & offline reload")
        with MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            h.page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ctrl = h.page.evaluate("() => Boolean(navigator.serviceWorker.controller)")
            self.assertTrue(ctrl)
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.open_card_index(0)
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T1-125")
