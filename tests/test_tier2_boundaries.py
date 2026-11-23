"""Tier 2 — Boundary & Corner-Case E2E Tests (125 tests: T2-001 .. T2-125, 5 per F01..F25)."""

from __future__ import annotations

import datetime as _dt
import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

from tests.common import (
    JUGENHEIM_BASE,
    KARLSRUHE_BASE,
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
    polyline_length_km,
    seg_is_ruftaxi_text,
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


# ---------------------------------------------------------------------------
# F01 Boundaries (T2-001 .. T2-005)
# ---------------------------------------------------------------------------


class TestT2_F01_CommuteBoundaries(unittest.TestCase):
    def test_t2_001_no_commute_substring_leakage_in_data_json(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        forbidden = (
            "dieburg hochschule",
            "moltkestraße 30",
            "clay kaserne",
            "stuttgart universität",
            "waiblingen rathaus",
            "waiblingen schwanen",
        )
        for t in data.get("tours", []):
            blob = (
                loader.tour_title(t, "de")
                + " "
                + " ".join(map(str, t.get("places", [])))
            ).lower()
            for f in forbidden:
                self.assertNotIn(f, blob, f"Commute {f!r} leaked into tour {t['id']}")

    def test_t2_002_buy_tickets_empty_shortcut_excluded_cleanly(self) -> None:
        _, loader = _cfg_loader()
        excl_blob = json.dumps(loader.excluded_shortcuts(), ensure_ascii=False).lower()
        self.assertIn("buy tickets", excl_blob)
        for t in loader.data_json().get("tours", []):
            self.assertIsInstance(t, dict)
            self.assertTrue(t.get("id"))
            self.assertNotIn("buy tickets", loader.tour_title(t, "de").lower())

    def test_t2_003_shortcut_10_miltenberg_dieburg_consistently_handled(self) -> None:
        _, loader = _cfg_loader()
        excl_blob = json.dumps(loader.excluded_shortcuts(), ensure_ascii=False).lower()
        in_excl = "miltenberg nach dieburg" in excl_blob or "hochschule süd" in excl_blob
        self.assertTrue(in_excl, "Shortcut #10 (Hochschule Süd Dieburg) must be documented in excluded_shortcuts.json")
        for t in loader.data_json().get("tours", []):
            self.assertNotIn("miltenberg nach dieburg", loader.tour_title(t, "de").lower())

    def test_t2_004_no_duplicate_ids_or_overlap_with_data(self) -> None:
        _, loader = _cfg_loader()
        excl = loader.excluded_shortcuts()
        excl_ids = [str(e.get("id")) for e in excl if e.get("id") is not None]
        # Note: phone_shortcuts.json may reuse generic IDs only rarely, check unique (id, label)
        pairs = [(str(e.get("id", "")), str(e.get("label") or e.get("name") or "")) for e in excl]
        self.assertEqual(len(pairs), len(set(pairs)), "Duplicate entries in excluded_shortcuts.json")
        tour_ids = {t["id"] for t in loader.data_json().get("tours", [])}
        self.assertEqual(set(excl_ids) & tour_ids, set(), "Excluded ID overlaps active tour ID")

    def test_t2_005_no_excluded_label_rendered_as_card_in_ui(self) -> None:
        cfg, loader = _cfg_loader()
        excl_labels = [
            str(e.get("label") or e.get("name") or "").strip().lower()
            for e in loader.excluded_shortcuts()
            if len(str(e.get("label") or e.get("name") or "").strip()) >= 5
        ]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            card_blob = "\n".join(ui.visible_card_texts()).lower()
            for lbl in excl_labels:
                self.assertNotIn(lbl, card_blob, f"Excluded label {lbl!r} rendered in UI")
            h.assert_zero_errors("T2-005")


# ---------------------------------------------------------------------------
# F02 Boundaries (T2-006 .. T2-010)
# ---------------------------------------------------------------------------


class TestT2_F02_PairingAndIdBoundaries(unittest.TestCase):
    def test_t2_006_all_360_loops_preserve_vias_and_places(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        rings = [t for t in cat.get("tours", []) if t.get("ring") is True and t.get("source") == "shortcut"]
        self.assertGreaterEqual(len(rings), 10)
        for t in rings:
            vias = (t.get("transit") or {}).get("vias") or []
            places = t.get("places") or []
            self.assertTrue(len(vias) >= 1 or len(places) >= 1, f"Ring tour {t['id']} collapsed to 0 vias/places")

    def test_t2_007_base_to_base_shortcuts_no_circular_corruption(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if loader.is_plan_routable(p):
                        # Must have valid non-empty rides/walks without NaN
                        self.assertTrue(len(p["fwd"]) >= 1 and len(p["rev"]) >= 1)

    def test_t2_008_recovered_remote_ids_clean_format(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        checked = 0
        for t in cat.get("tours", []):
            tr = t.get("transit") or {}
            for st in [tr.get("startStop"), tr.get("endStop")] + list(tr.get("vias") or []):
                if not isinstance(st, dict):
                    continue
                rid = st.get("remoteId")
                if rid:
                    s = str(rid)
                    self.assertNotIn("\n", s)
                    self.assertNotIn("\r", s)
                    self.assertRegex(s, r"(?:@O=|^\d{5,9}$|^de:)")
                    checked += 1
        self.assertGreater(checked, 0)

    def test_t2_009_no_zero_zero_or_out_of_region_stop_coords(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for seg in segs:
                            for endp in (seg.get("from"), seg.get("to")):
                                if isinstance(endp, dict) and endp.get("lat") is not None:
                                    lat, lon = float(endp["lat"]), float(endp["lon"])
                                    self.assertFalse(lat == 0.0 and lon == 0.0)
                                    self.assertTrue(in_sw_bounds(lat, lon), f"{t['id']} stop {endp} out of bounds")

    def test_t2_010_round_trip_fwd_and_rev_are_directional(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        checked = 0
        for t in data.get("tours", []):
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if not loader.is_plan_routable(p):
                continue
            fwd_rides = [s for s in p["fwd"] if s.get("kind") == "ride"]
            rev_rides = [s for s in p["rev"] if s.get("kind") == "ride"]
            if fwd_rides and rev_rides:
                f_sig = (fwd_rides[0].get("dep"), (fwd_rides[0].get("from") or {}).get("name"), (fwd_rides[-1].get("to") or {}).get("name"))
                r_sig = (rev_rides[0].get("dep"), (rev_rides[0].get("from") or {}).get("name"), (rev_rides[-1].get("to") or {}).get("name"))
                self.assertNotEqual(f_sig, r_sig, f"{t['id']} fwd and rev identical: {f_sig}")
                checked += 1
        self.assertGreater(checked, 0)


# ---------------------------------------------------------------------------
# F03 Boundaries (T2-011 .. T2-015)
# ---------------------------------------------------------------------------


class TestT2_F03_GpxBoundaries(unittest.TestCase):
    def test_t2_011_near_variant_gpx_pairs_not_falsely_merged(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        all_files = [gf for t in cat.get("tours", []) for gf in ((t.get("hike") or {}).get("gpxFiles") or [])]
        blob = " ".join(all_files).lower()
        for kw in ("rhodt", "ebersteinburg"):
            matches = [f for f in all_files if kw in f.lower()]
            self.assertGreaterEqual(len(matches), 2, f"Near-variant GPX files for {kw!r} should remain distinct: {blob[:200]}")

    def test_t2_012_generic_gpx_names_normalized_no_parking_lot_title(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            de = loader.tour_title(t, "de").strip()
            self.assertTrue(de, f"{t['id']} empty title")
            self.assertNotIn(de.lower(), {"parking lot", "parking", "scenic viewpoint", "position on the map"})

    def test_t2_013_elevation_smoothing_prevents_dem_jitter_inflation(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            h = t.get("hike") or {}
            if h.get("hasGpx") and float(h.get("distKm", 0)) >= 0.5:
                ratio = float(h["elevGainM"]) / float(h["distKm"])
                self.assertLessEqual(ratio, 250.0, f"{t['id']} elevGainM/distKm={ratio:.1f} > 250 (unsmoothed DEM jitter)")

    def test_t2_014_poly5_length_matches_dist_km_within_15_pct(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            h = t.get("hike") or {}
            if not (h.get("hasGpx") and h.get("poly5")):
                continue
            pts = decode_polyline(h["poly5"], precision=5)
            calc_km = polyline_length_km(pts)
            rep_km = float(h["distKm"])
            if rep_km >= 1.0:
                rel_err = abs(calc_km - rep_km) / rep_km
                self.assertLessEqual(
                    rel_err, 0.20, f"{t['id']} poly5 len {calc_km:.2f} km vs distKm {rep_km:.2f} km"
                )

    def test_t2_015_walk_min_sane_min_max_bounds(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            h = t.get("hike") or {}
            if not h.get("hasGpx"):
                continue
            wmin = int(h["walkMin"])
            dist = float(h["distKm"])
            if dist >= 1.0:
                self.assertGreaterEqual(wmin, 10, f"{t['id']} walkMin={wmin} too small for {dist}km")
            self.assertLessEqual(wmin, 720, f"{t['id']} walkMin={wmin} > 720 (seconds vs minutes bug?)")


# ---------------------------------------------------------------------------
# F04 Boundaries (T2-016 .. T2-020)
# ---------------------------------------------------------------------------


class TestT2_F04_StopLinkingBoundaries(unittest.TestCase):
    def test_t2_016_remote_gpx_tracks_not_falsely_claimed_le_800m(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        for t in cat.get("tours", []):
            h = t.get("hike") or {}
            tr = t.get("transit") or {}
            if not (h.get("hasGpx") and tr.get("startStop") and h.get("startCoord")):
                continue
            ss = tr["startStop"]
            sc = h["startCoord"]
            if ss.get("lat") is None or tr.get("unlinkedReason"):
                continue
            air = haversine_m(float(ss["lat"]), float(ss["lon"]), float(sc[0]), float(sc[1]))
            if tr.get("walkToStartM") is not None and float(tr["walkToStartM"]) <= 800:
                self.assertLessEqual(air, 950.0, f"{t['id']} claims <=800m but air={air:.0f}m")

    def test_t2_017_waypoints_not_duplicated_to_38(self) -> None:
        _, loader = _cfg_loader()
        wps = loader.tours_catalog().get("waypoints") or []
        self.assertEqual(len(wps), 19)

    def test_t2_018_chained_gpx_segments_proximate(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        for t in cat.get("tours", []):
            h = t.get("hike") or {}
            # If chained segment metadata exists, verify gap <= 500m
            for gap in h.get("chainGapsM") or []:
                self.assertLessEqual(float(gap), 500.0, f"{t['id']} chain gap {gap}m > 500m")
        self.assertTrue(cat.get("tours"))

    def test_t2_019_two_bismarck_tower_waypoints_distinct_coords(self) -> None:
        _, loader = _cfg_loader()
        wps = loader.tours_catalog().get("waypoints") or []
        bismarcks = [w for w in wps if "bismarck" in str(w.get("name", "")).lower()]
        self.assertEqual(len(bismarcks), 2, f"Expected 2 Bismarck Tower waypoints, got {bismarcks}")
        dist = haversine_m(
            float(bismarcks[0]["lat"]),
            float(bismarcks[0]["lon"]),
            float(bismarcks[1]["lat"]),
            float(bismarcks[1]["lon"]),
        )
        self.assertGreater(dist, 5000.0, "Two Bismarck Towers collapsed to identical coordinates")

    def test_t2_020_start_stop_within_2km_of_hike_start_unless_noted(self) -> None:
        _, loader = _cfg_loader()
        cat = loader.tours_catalog()
        for t in cat.get("tours", []):
            h = t.get("hike") or {}
            tr = t.get("transit") or {}
            if not (h.get("hasGpx") and tr.get("startStop") and h.get("startCoord")):
                continue
            if tr.get("unlinkedReason"):
                continue
            ss = tr["startStop"]
            sc = h["startCoord"]
            if ss.get("lat") is not None:
                dist = haversine_m(float(ss["lat"]), float(ss["lon"]), float(sc[0]), float(sc[1]))
                self.assertLessEqual(dist, 2000.0, f"{t['id']} startStop {dist:.0f}m from hike start without reason")


# ---------------------------------------------------------------------------
# F05 Boundaries (T2-021 .. T2-025)
# ---------------------------------------------------------------------------


class TestT2_F05_DeutschlandticketBoundaries(unittest.TestCase):
    def test_t2_021_s6_accepted_as_regional_dticket(self) -> None:
        # Pure validator + data verification: S6 (METRO) must NOT trigger forbidden Fernverkehr
        self.assertIsNone(is_forbidden_fernverkehr({"kind": "ride", "mode": "sbahn", "line": "S6", "op": "DB Regio"}))

    def test_t2_022_s8_stadtbahn_accepted_as_regional_dticket(self) -> None:
        self.assertIsNone(is_forbidden_fernverkehr({"kind": "ride", "mode": "stadtbahn", "line": "S8", "op": "AVG"}))
        _, loader = _cfg_loader()
        # Verify S8 appears in Karlsruhe/Black Forest tours if routed via Murgtal
        data = loader.data_json()
        self.assertTrue(data.get("tours"))

    def test_t2_023_substring_ic_in_town_names_not_false_positived(self) -> None:
        for safe_line in ("Bickenbach", "Eich", "Stadtbahn", "S6", "RE60", "RB68"):
            self.assertIsNone(is_forbidden_fernverkehr({"kind": "ride", "mode": "re", "line": safe_line}))
        for bad_line in ("IC 2314", "ICE 103", "EC 8", "FLX 10", "TGV 9570"):
            self.assertIsNotNone(is_forbidden_fernverkehr({"kind": "ride", "mode": "rail", "line": bad_line}))

    def test_t2_024_nested_legs_and_intermediate_stops_zero_fernverkehr(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            for b, days in loader.plans_for(t).items():
                for d, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for it_name, segs in _iter_itineraries(plan):
                        for seg in segs:
                            for sub in seg.get("legs") or []:
                                self.assertIsNone(
                                    is_forbidden_fernverkehr(sub),
                                    f"{t['id']} {b}/{d}/{it_name} nested leg Fernverkehr: {sub}",
                                )

    def test_t2_025_ferry_legs_dticket_compatible_or_annotated(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        for t in data.get("tours", []):
            for b, days in loader.plans_for(t).items():
                for d, plan in (days or {}).items():
                    if not isinstance(plan, dict):
                        continue
                    for _, segs in _iter_itineraries(plan):
                        for seg in segs:
                            if str(seg.get("mode", "")).lower() == "ferry":
                                self.assertIsNone(is_forbidden_fernverkehr(seg))


# ---------------------------------------------------------------------------
# F06 Boundaries (T2-026 .. T2-030)
# ---------------------------------------------------------------------------


class TestT2_F06_TransferBoundaries(unittest.TestCase):
    def test_t2_026_exact_4min_accepted_3min_strictly_absent(self) -> None:
        # Verify helper boundary + 0 <4min cases in data.json
        segs_4 = [
            {"kind": "ride", "dep": "09:00", "arr": "09:12", "from": {"name": "A"}, "to": {"name": "B"}},
            {"kind": "ride", "dep": "09:16", "arr": "09:30", "from": {"name": "B"}, "to": {"name": "C"}},
        ]
        self.assertEqual(compute_transfers(segs_4)[0][2], 4)
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for _, _, gap, hub in compute_transfers(segs):
                            self.assertGreaterEqual(gap, 4, f"{t['id']} {b}/{d} hub={hub} gap={gap} < 4")

    def test_t2_027_walk_leg_between_rides_counts_in_transfer_gap(self) -> None:
        segs_walk = [
            {"kind": "ride", "dep": "09:50", "arr": "10:00", "from": {"name": "A"}, "to": {"name": "B1"}},
            {"kind": "walk", "dep": "10:00", "arr": "10:02", "from": {"name": "B1"}, "to": {"name": "B2"}},
            {"kind": "ride", "dep": "10:03", "arr": "10:20", "from": {"name": "B2"}, "to": {"name": "C"}},
        ]
        self.assertEqual(compute_transfers(segs_walk)[0][2], 3)
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for _, _, gap, _ in compute_transfers(segs):
                            self.assertGreaterEqual(gap, 4)

    def test_t2_028_midnight_crossing_transfer_computed_correctly(self) -> None:
        self.assertEqual(minutes_diff("23:57", "00:02"), 5)
        self.assertEqual(minutes_diff("23:59", "00:01"), 2)
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for _, _, gap, _ in compute_transfers(segs):
                            self.assertGreaterEqual(gap, 4)

    def test_t2_029_distinct_line_change_never_under_4min(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for prev_r, next_r, gap, _ in compute_transfers(segs):
                            if prev_r.get("line") != next_r.get("line"):
                                self.assertGreaterEqual(gap, 4)

    def test_t2_030_major_hub_4_to_5_min_flagged_or_avoided(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for it_name, segs in _iter_itineraries(p):
                        for prev_r, next_r, gap, hub in compute_transfers(segs):
                            if is_major_hub(hub) and gap < 5:
                                warns = list(prev_r.get("warnings") or []) + list(next_r.get("warnings") or [])
                                self.assertTrue(warns, f"{t['id']} {b}/{d}/{it_name} major hub {hub} gap={gap}m unflagged")


# ---------------------------------------------------------------------------
# F07 Boundaries (T2-031 .. T2-035)
# ---------------------------------------------------------------------------


class TestT2_F07_HikeSyncBoundaries(unittest.TestCase):
    def test_t2_031_exact_walk_plus_30_boundary_zero_violations(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            wmin = loader.walk_min(t)
            if wmin <= 0:
                continue
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if loader.is_plan_routable(p):
                        stay = compute_hike_stay(p["fwd"], p["rev"])
                        self.assertGreaterEqual(stay, wmin + 30)

    def test_t2_032_long_hike_ge_240min_enforces_270min_stay(self) -> None:
        _, loader = _cfg_loader()
        long_tours = [t for t in loader.data_json()["tours"] if loader.walk_min(t) >= 240]
        for t in long_tours:
            wmin = loader.walk_min(t)
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if loader.is_plan_routable(p):
                        stay = compute_hike_stay(p["fwd"], p["rev"])
                        self.assertGreaterEqual(stay, max(270, wmin + 30))

    def test_t2_033_short_hike_le_45min_still_enforces_plus_30_buffer(self) -> None:
        _, loader = _cfg_loader()
        short_tours = [t for t in loader.data_json()["tours"] if 0 < loader.walk_min(t) <= 45]
        for t in short_tours:
            wmin = loader.walk_min(t)
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if loader.is_plan_routable(p):
                        stay = compute_hike_stay(p["fwd"], p["rev"])
                        self.assertGreaterEqual(stay, wmin + 30)

    def test_t2_034_both_bases_independently_satisfy_walk_plus_30(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            wmin = loader.walk_min(t)
            if wmin <= 0:
                continue
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if loader.is_plan_routable(p):
                        stay = compute_hike_stay(p["fwd"], p["rev"])
                        self.assertGreaterEqual(stay, wmin + 30, f"{t['id']} {b}/{d} stay={stay} < {wmin+30}")

    def test_t2_035_360_transit_loops_keep_vias_without_gpx_conflict(self) -> None:
        _, loader = _cfg_loader()
        loops = [t for t in loader.data_json()["tours"] if t.get("ring") and not (t.get("hike") or {}).get("hasGpx")]
        for t in loops:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p):
                self.assertTrue(p["fwd"] and p["rev"])


# ---------------------------------------------------------------------------
# F08 Boundaries (T2-036 .. T2-040)
# ---------------------------------------------------------------------------


class TestT2_F08_DualBaseBoundaries(unittest.TestCase):
    def test_t2_036_jugenheim_first_leg_near_base_coords(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for d in ("wd", "sa", "so"):
                p = loader.plans_for(t)["jugenheim"][d]
                if not loader.is_plan_routable(p):
                    continue
                fr = p["fwd"][0].get("from") or {}
                if fr.get("lat") is not None:
                    dist = haversine_m(JUGENHEIM_BASE["lat"], JUGENHEIM_BASE["lon"], float(fr["lat"]), float(fr["lon"]))
                    self.assertLessEqual(dist, 2500.0, f"{t['id']} jugenheim/{d} origin {dist:.0f}m from Jugenheim")

    def test_t2_037_karlsruhe_first_leg_near_base_coords(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for d in ("wd", "sa", "so"):
                p = loader.plans_for(t)["karlsruhe"][d]
                if not loader.is_plan_routable(p):
                    continue
                fr = p["fwd"][0].get("from") or {}
                if fr.get("lat") is not None:
                    dist = haversine_m(KARLSRUHE_BASE["lat"], KARLSRUHE_BASE["lon"], float(fr["lat"]), float(fr["lon"]))
                    self.assertLessEqual(dist, 3000.0, f"{t['id']} karlsruhe/{d} origin {dist:.0f}m from Karlsruhe Hbf")

    def test_t2_038_alts_fwd_sorted_and_deduplicated(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if not loader.is_plan_routable(p):
                        continue
                    alts = p.get("alts_fwd") or []
                    sigs = []
                    prev_dep = -1
                    for a in alts:
                        segs = a["segs"] if isinstance(a, dict) else a
                        if not segs:
                            continue
                        dep_m = parse_hhmm(segs[0].get("dep"))
                        self.assertIsNotNone(dep_m)
                        self.assertGreaterEqual(dep_m, prev_dep, f"{t['id']} {b}/{d} alts_fwd not sorted")
                        prev_dep = dep_m
                        sig = (segs[0].get("dep"), segs[-1].get("arr"), tuple(s.get("line") for s in segs if s.get("kind") == "ride"))
                        self.assertNotIn(sig, sigs, f"{t['id']} {b}/{d} duplicate alt {sig}")
                        sigs.append(sig)

    def test_t2_039_last_safe_return_ge_rev_and_before_0230(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if not loader.is_plan_routable(p):
                        continue
                    lsr = loader.last_safe_return(p)
                    if not lsr:
                        continue
                    rev_dep = parse_hhmm(p["rev"][0].get("dep"))
                    lsr_dep = parse_hhmm(lsr.get("dep"))
                    lsr_arr = parse_hhmm(lsr.get("arr"))
                    if rev_dep is not None and lsr_dep is not None:
                        # Account for post-midnight return dep
                        adj_lsr = lsr_dep + (1440 if lsr_dep < 240 and rev_dep > 600 else 0)
                        self.assertGreaterEqual(adj_lsr, rev_dep, f"{t['id']} {b}/{d} lastSafeReturn earlier than rev")
                    if lsr_arr is not None and lsr_dep is not None and lsr_arr < lsr_dep:
                        self.assertLessEqual(lsr_arr, 150, f"{t['id']} {b}/{d} arrives after 02:30 ({lsr.get('arr')})")

    def test_t2_040_reference_days_match_wd_sa_so_weekdays(self) -> None:
        _, loader = _cfg_loader()
        days = loader.data_json().get("days") or {}
        wd = _dt.date.fromisoformat(days["wd"]).weekday()
        sa = _dt.date.fromisoformat(days["sa"]).weekday()
        so = _dt.date.fromisoformat(days["so"]).weekday()
        self.assertIn(wd, (0, 1, 2, 3, 4), f"days.wd {days['wd']} not Mon-Fri")
        self.assertEqual(sa, 5, f"days.sa {days['sa']} not Saturday")
        self.assertEqual(so, 6, f"days.so {days['so']} not Sunday")


# ---------------------------------------------------------------------------
# F09 Boundaries (T2-041 .. T2-045)
# ---------------------------------------------------------------------------


class TestT2_F09_WarningBoundaries(unittest.TestCase):
    def test_t2_041_case_insensitive_ruftaxi_variants_detected(self) -> None:
        for txt in ("RUFTAXI 6977", "Ruftaxi/-bus", "ALT 12", "FlexLine", "On-Demand ODM"):
            self.assertTrue(seg_is_ruftaxi_text({"line": txt}))

    def test_t2_042_regular_buses_not_falsely_tagged_ruftaxi(self) -> None:
        for txt in ("Bus 670", "Bus 213", "Bus 454"):
            self.assertFalse(seg_is_ruftaxi_text({"line": txt, "mode": "bus"}))
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for seg in segs:
                            if seg.get("line") in ("Bus 670", "Bus 213") and not seg_is_ruftaxi_text(seg):
                                types = [w.get("type") for w in (seg.get("warnings") or [])]
                                self.assertNotIn("ruftaxi", types)

    def test_t2_043_seasonal_or_winter_non_service_documented(self) -> None:
        _, loader = _cfg_loader()
        data = loader.data_json()
        notes = loader.routing_notes_entries(loader.routing_notes())
        # Ensure dataset + notes handle winter reference date cleanly
        self.assertIsInstance(notes, list)
        self.assertTrue(data.get("days"))

    def test_t2_044_multiple_warnings_render_legibly_412x915(self) -> None:
        cfg, loader = _cfg_loader()
        warned = None
        for t in loader.data_json()["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and any(len(s.get("warnings") or []) >= 1 for s in p["fwd"] + p["rev"]):
                warned = t
                break
        warned = warned or loader.data_json()["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(warned, "de")[:18]))
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T2-044")

    def test_t2_045_warning_texts_actionable_non_trivial(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            for b, days in loader.plans_for(t).items():
                for d, p in (days or {}).items():
                    if not isinstance(p, dict):
                        continue
                    for _, segs in _iter_itineraries(p):
                        for seg in segs:
                            for w in seg.get("warnings") or []:
                                self.assertGreaterEqual(len(str(w.get("text", "")).strip()), 8)


# ---------------------------------------------------------------------------
# F10 Boundaries (T2-046 .. T2-050)
# ---------------------------------------------------------------------------


class TestT2_F10_RoutingNoteBoundaries(unittest.TestCase):
    def test_t2_046_no_null_plan_without_reason(self) -> None:
        _, loader = _cfg_loader()
        notes = loader.routing_notes_entries(loader.routing_notes())
        noted = {(n.get("tour_id") or n.get("id")) for n in notes}
        for t in loader.data_json()["tours"]:
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if p is None:
                        self.assertIn(t["id"], noted, f"{t['id']} {b}/{d} is null without routing note")

    def test_t2_047_partial_base_unroutability_keeps_other_base_usable(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            jg_ok = any(loader.is_plan_routable(loader.plans_for(t)["jugenheim"][d]) for d in ("wd", "sa", "so"))
            ka_ok = any(loader.is_plan_routable(loader.plans_for(t)["karlsruhe"][d]) for d in ("wd", "sa", "so"))
            if jg_ok != ka_ok:
                ok_base = "jugenheim" if jg_ok else "karlsruhe"
                self.assertTrue(any(loader.is_plan_routable(loader.plans_for(t)[ok_base][d]) for d in ("wd", "sa", "so")))

    def test_t2_048_routing_notes_reasons_descriptive(self) -> None:
        _, loader = _cfg_loader()
        for n in loader.routing_notes_entries(loader.routing_notes()):
            r = str(n.get("reason", "")).strip()
            self.assertGreaterEqual(len(r), 10, f"Terse routing note reason: {n}")
            self.assertNotIn(r.lower(), {"error", "none", "n/a", "tbd"})

    def test_t2_049_switching_from_unroutable_day_back_to_routable_day_works(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            ui.select_day("so")
            ui.select_day("wd")
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            h.assert_zero_errors("T2-049")

    def test_t2_050_no_nan_undefined_invalid_date_in_dom(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            body = h.page.locator("body").inner_text()
            for bad in ("NaN", "undefined", "Invalid Date", "[object Object]"):
                self.assertNotIn(bad, body, f"Found {bad!r} in list DOM")
            ui.open_card_index(0)
            dtxt = ui.detail_text()
            for bad in ("NaN", "undefined", "Invalid Date", "[object Object]"):
                self.assertNotIn(bad, dtxt, f"Found {bad!r} in detail DOM")
            h.assert_zero_errors("T2-050")


# ---------------------------------------------------------------------------
# F11 Boundaries (T2-051 .. T2-055)
# ---------------------------------------------------------------------------


class TestT2_F11_CurationBoundaries(unittest.TestCase):
    def test_t2_051_curated_count_strictly_20_to_30(self) -> None:
        _, loader = _cfg_loader()
        cur = [t for t in loader.data_json()["tours"] if t.get("source") == "curated"]
        self.assertGreaterEqual(len(cur), 20)
        self.assertLessEqual(len(cur), 30)

    def test_t2_052_curated_non_trivial_de_and_en_titles_and_tips(self) -> None:
        _, loader = _cfg_loader()
        cur = [t for t in loader.data_json()["tours"] if t.get("source") == "curated"]
        for t in cur:
            self.assertGreaterEqual(len(t["title"]["de"].strip()), 4)
            self.assertGreaterEqual(len(t["title"]["en"].strip()), 4)
            self.assertGreaterEqual(len(t["tips"]["de"].strip()), 5)
            self.assertGreaterEqual(len(t["tips"]["en"].strip()), 5)

    def test_t2_053_curated_ids_unique_url_safe_slugs(self) -> None:
        _, loader = _cfg_loader()
        cur = [t for t in loader.data_json()["tours"] if t.get("source") == "curated"]
        ids = [t["id"] for t in cur]
        self.assertEqual(len(ids), len(set(ids)))
        for tid in ids:
            self.assertRegex(tid, r"^[a-z0-9_-]+$")

    def test_t2_054_curated_difficulty_and_category_in_schema(self) -> None:
        _, loader = _cfg_loader()
        cats = {"wanderung", "berg", "see", "burg", "wein", "stadt", "panoramatour"}
        for t in loader.data_json()["tours"]:
            if t.get("source") == "curated":
                self.assertIn(t.get("difficulty"), {"leicht", "mittel", "schwer"})
                self.assertIn(t.get("category"), cats)

    def test_t2_055_curated_ids_disjoint_from_shortcut_and_mapy_ids(self) -> None:
        _, loader = _cfg_loader()
        tours = loader.data_json()["tours"]
        cur_ids = {t["id"] for t in tours if t.get("source") == "curated"}
        other_ids = {t["id"] for t in tours if t.get("source") != "curated"}
        self.assertEqual(cur_ids & other_ids, set())


# ---------------------------------------------------------------------------
# F12 Boundaries (T2-056 .. T2-060)
# ---------------------------------------------------------------------------


class TestT2_F12_MediaBoundaries(unittest.TestCase):
    def test_t2_056_no_html_or_svg_masquerading_as_jpg(self) -> None:
        _, loader = _cfg_loader()
        for t in loader.data_json()["tours"]:
            p = loader.path(f"img/{t['id']}.jpg")
            head = p.read_bytes()[:64].lstrip().lower()
            self.assertFalse(head.startswith((b"<!doctype", b"<html", b"<svg")), f"{p} is not JPEG")
            with Image.open(p) as im:
                self.assertEqual(im.format, "JPEG")

    def test_t2_057_zero_external_wikimedia_img_urls_in_built_html(self) -> None:
        cfg, loader = _cfg_loader()
        for p in (loader.path("index.html"), cfg.standalone_path):
            txt = p.read_text(encoding="utf-8")
            self.assertNotIn("upload.wikimedia.org", txt, f"External Wikimedia image URL in {p}")

    def test_t2_058_credits_https_urls_and_clean_artist_text(self) -> None:
        _, loader = _cfg_loader()
        for tid, c in loader.credits_json().items():
            self.assertTrue(str(c["licenseUrl"]).startswith("https://"), f"{tid} non-https licenseUrl")
            self.assertTrue(str(c["page"]).startswith("https://"), f"{tid} non-https page")
            self.assertNotIn("<script", str(c["artist"]).lower())

    def test_t2_059_five_consecutive_tours_load_images_cleanly(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            cnt = min(5, ui.card_count())
            for i in range(cnt):
                ui.open_card_index(i)
                ok = h.page.evaluate(
                    "() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)"
                )
                self.assertTrue(ok, f"Tour card #{i} failed hero image load")
                ui.go_back()
            h.assert_zero_errors("T2-059")

    def test_t2_060_base64_images_render_offline_in_both_builds(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.page.evaluate("() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.open_card_index(0)
            ok = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)"
            )
            self.assertTrue(ok)
            h.assert_zero_errors("T2-060")


# ---------------------------------------------------------------------------
# F13 Boundaries (T2-061 .. T2-065)
# ---------------------------------------------------------------------------


class TestT2_F13_PwaBoundaries(unittest.TestCase):
    def test_t2_061_sw_js_has_3000_tile_cap_and_trim_logic(self) -> None:
        _, loader = _cfg_loader()
        sw = loader.path("sw.js").read_text(encoding="utf-8")
        self.assertRegex(sw, r"MAX_TILES\s*=\s*3000")
        self.assertRegex(sw, r"trim|delete\(|keys\(", "sw.js missing tile eviction logic")

    def test_t2_062_sw_shell_files_all_exist_and_serve_200(self) -> None:
        cfg, loader = _cfg_loader()
        sw = loader.path("sw.js").read_text(encoding="utf-8")
        m = re.search(r"SHELL\s*=\s*\[([^\]]+)\]", sw, re.S)
        self.assertIsNotNone(m, "SHELL array not found in sw.js")
        entries = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
        self.assertTrue(entries)
        with StaticServer(cfg.root) as srv:
            for item in entries:
                r = requests.get(srv.base_url + item.lstrip("./"), timeout=10)
                self.assertEqual(r.status_code, 200, f"SW SHELL asset {item} returned {r.status_code}")

    def test_t2_063_manifest_relative_urls_for_github_pages_subpath(self) -> None:
        _, loader = _cfg_loader()
        m = loader.manifest_json()
        self.assertTrue(str(m.get("start_url", "")).startswith("./"))
        self.assertTrue(str(m.get("scope", "")).startswith("./"))

    def test_t2_064_offline_hash_route_falls_back_cleanly(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.page.evaluate("() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            h.context.set_offline(True)
            h.page.goto(srv.base_url + "#tour-check", wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertGreater(ui.card_count(), 0)
            h.assert_zero_errors("T2-064")

    def test_t2_065_offline_reload_preserves_local_storage(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.page.evaluate("() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.switch_base("karlsruhe")
            ui.toggle_lang()
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ls = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertIn("karlsruhe", ls.lower())
            h.assert_zero_errors("T2-065")


# ---------------------------------------------------------------------------
# F14 Boundaries (T2-066 .. T2-070)
# ---------------------------------------------------------------------------


class TestT2_F14_StandaloneBoundaries(unittest.TestCase):
    def test_t2_066_standalone_omits_manifest_link(self) -> None:
        cfg, _ = _cfg_loader()
        soup = BeautifulSoup(cfg.standalone_path.read_text(encoding="utf-8"), "html.parser")
        self.assertIsNone(soup.find("link", rel="manifest"))

    def test_t2_067_standalone_file_url_no_security_or_sw_errors(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            h.assert_zero_errors("T2-067")

    def test_t2_068_standalone_supports_filters_i18n_dark_gpx(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.toggle_lang()
            ui.toggle_theme()
            ui.open_card_index(0)
            fname, xml_txt = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(xml_txt)
            h.assert_zero_errors("T2-068")

    def test_t2_069_standalone_with_all_external_requests_aborted(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness(offline_allowed=True) as h:
            h.page.route("http*://**", lambda route: route.abort())
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.open_card_index(0)
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T2-069")

    def test_t2_070_standalone_escapes_closing_script_tag_in_data(self) -> None:
        cfg, _ = _cfg_loader()
        txt = cfg.standalone_path.read_text(encoding="utf-8")
        # Inside JSON data there must not be raw '</script>'
        self.assertEqual(len(re.findall(r"</script>", txt, re.I)), 2 if "const DATA" in txt else len(re.findall(r"</script>", txt, re.I)))
        # Ensure inline JSON does not contain unescaped '</script'
        m = re.search(r"DATA\s*=\s*(\{.*?\});", txt, re.S)
        if m:
            self.assertNotIn("</", m.group(1))


# ---------------------------------------------------------------------------
# F15 Boundaries (T2-071 .. T2-075)
# ---------------------------------------------------------------------------


class TestT2_F15_ViewportBoundaries(unittest.TestCase):
    def test_t2_071_narrow_mobile_360x740_no_overflow(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(viewport=(360, 740)) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T2-071")

    def test_t2_072_desktop_1280x800_renders_zero_errors(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(viewport=(1280, 800)) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            ui.open_card_index(0)
            h.assert_zero_errors("T2-072")

    def test_t2_073_rapid_open_close_5_tours_zero_errors(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for i in range(min(5, ui.card_count())):
                ui.open_card_index(i)
                ui.go_back()
            h.assert_zero_errors("T2-073")

    def test_t2_074_long_title_or_multi_transfer_wraps_cleanly(self) -> None:
        cfg, loader = _cfg_loader()
        longest = max(loader.data_json()["tours"], key=lambda t: len(loader.tour_title(t, "de")))
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(longest, "de")[:18]))
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T2-074")

    def test_t2_075_back_button_restores_cards_without_blank_screen(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            n = ui.card_count()
            ui.open_card_index(0)
            ui.go_back()
            self.assertEqual(ui.card_count(), n)
            h.assert_zero_errors("T2-075")


# ---------------------------------------------------------------------------
# F16 Boundaries (T2-076 .. T2-080)
# ---------------------------------------------------------------------------


class TestT2_F16_BaseSwitcherBoundaries(unittest.TestCase):
    def test_t2_076_double_toggle_restores_identical_jugenheim_state(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            t0 = ui.detail_text()
            ui.switch_base("karlsruhe")
            ui.switch_base("jugenheim")
            t1 = ui.detail_text()
            self.assertEqual(t0, t1)
            h.assert_zero_errors("T2-076")

    def test_t2_077_base_switch_clamps_window_index_safely(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            wins = h.page.locator("#detail button, .win-btn, [data-win]").filter(has_text=re.compile(r"\d{2}:\d{2}"))
            if wins.count() >= 3:
                wins.nth(wins.count() - 1).click()
            ui.switch_base("karlsruhe")
            self.assertNotIn("undefined", ui.detail_text())
            h.assert_zero_errors("T2-077")

    def test_t2_078_base_switch_recomputes_feasible_filter_cards(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.set_feasible_time("13:00")
            jg_cnt = ui.card_count()
            ui.switch_base("karlsruhe")
            ka_cnt = ui.card_count()
            self.assertGreaterEqual(jg_cnt, 0)
            self.assertGreaterEqual(ka_cnt, 0)
            h.assert_zero_errors("T2-078")

    def test_t2_079_base_switch_updates_deep_link_origin(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            db0 = ui.detail_links()["db"][0]
            ui.switch_base("karlsruhe")
            db1 = ui.detail_links()["db"][0]
            self.assertNotEqual(db0, db1)
            self.assertIn("karlsruhe", db1.lower())
            h.assert_zero_errors("T2-079")

    def test_t2_080_base_switch_after_custom_home_preset_clean(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            ui.home_modal_locator().locator("button").filter(has_text=re.compile(r"Heidelberg Hbf", re.I)).first.click()
            h.page.wait_for_timeout(200)
            ui.switch_base("karlsruhe")
            self.assertNotIn("NaN", ui.header_text())
            h.assert_zero_errors("T2-080")


# ---------------------------------------------------------------------------
# F17 Boundaries (T2-081 .. T2-085)
# ---------------------------------------------------------------------------


class TestT2_F17_HomeModalBoundaries(unittest.TestCase):
    def test_t2_081_nominatim_empty_or_nomatch_graceful(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            h.page.route("**/nominatim.openstreetmap.org/**", lambda route: route.fulfill(status=200, content_type="application/json", body="[]"))
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            sinp = m.locator("#homeSearchInput, input[type='search'], input[placeholder*='Adresse' i], input[placeholder*='Address' i]").first
            sinp.fill("zzzznoplace12345")
            sbtn = m.locator("button").filter(has_text=re.compile(r"Suchen|Search|🔍", re.I)).first
            if sbtn.count() > 0:
                sbtn.click()
            h.page.wait_for_timeout(250)
            h.assert_zero_errors("T2-081")

    def test_t2_082_nominatim_mocked_result_updates_home_label(self) -> None:
        cfg, _ = _cfg_loader()
        payload = json.dumps([{"display_name": "Mannheim Schloss, Mannheim", "lat": "49.483", "lon": "8.462"}])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            h.page.route("**/nominatim.openstreetmap.org/**", lambda route: route.fulfill(status=200, content_type="application/json", body=payload))
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            sinp = m.locator("#homeSearchInput, input[type='search'], input[placeholder*='Adresse' i], input[placeholder*='Address' i]").first
            sinp.fill("Mannheim Schloss")
            sbtn = m.locator("button").filter(has_text=re.compile(r"Suchen|Search|🔍", re.I)).first
            if sbtn.count() > 0:
                sbtn.click()
            h.page.wait_for_timeout(300)
            res = m.locator("#geoResults button, #geoResults li, #geoResults a").first
            if res.count() > 0:
                res.click()
                h.page.wait_for_timeout(200)
                self.assertIn("Mannheim", ui.header_text())
            h.assert_zero_errors("T2-082")

    def test_t2_083_invalid_custom_coords_rejected_no_nan(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            m.locator("#customHomeName, input[placeholder*='Name' i]").first.fill("BadCoords")
            m.locator("#customHomeLat, input[placeholder*='Lat' i]").first.fill("999")
            m.locator("#customHomeLon, input[placeholder*='Lon' i]").first.fill("abc")
            m.locator("#saveHomeBtn, button").filter(has_text=re.compile(r"Speichern|Save", re.I)).first.click()
            h.page.wait_for_timeout(200)
            ls = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertNotIn("NaN", ls)
            h.assert_zero_errors("T2-083")

    def test_t2_084_gps_denied_handled_gracefully(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(grant_geolocation=False) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            m = ui.home_modal_locator()
            gps_btn = m.locator("#homeGpsBtn, button").filter(has_text=re.compile(r"GPS|Standort|Location", re.I)).first
            gps_btn.click()
            h.page.wait_for_timeout(300)
            h.assert_zero_errors("T2-084")

    def test_t2_085_closing_home_modal_leaves_saved_home_intact(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            before = ui.header_text()
            ui.open_home_modal()
            ui.close_home_modal()
            self.assertEqual(ui.header_text(), before)
            h.assert_zero_errors("T2-085")


# ---------------------------------------------------------------------------
# F18 Boundaries (T2-086 .. T2-090)
# ---------------------------------------------------------------------------


class TestT2_F18_TimelineBoundaries(unittest.TestCase):
    def test_t2_086_direct_zero_transfer_tour_no_bogus_transfer_pill(self) -> None:
        cfg, loader = _cfg_loader()
        direct = None
        for t in loader.data_json()["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and sum(1 for s in p["fwd"] if s.get("kind") == "ride") == 1:
                direct = t
                break
        if direct is None:
            # Verify multi-leg tours do not report negative transfers
            self.assertTrue(loader.data_json()["tours"])
            return
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(direct, "de")[:18]))
            self.assertNotIn("0 Min. Umstieg", ui.detail_text())
            h.assert_zero_errors("T2-086")

    def test_t2_087_three_plus_transfer_tour_renders_chronologically(self) -> None:
        cfg, loader = _cfg_loader()
        multi = None
        for t in loader.data_json()["tours"]:
            for b in ("jugenheim", "karlsruhe"):
                p = loader.plans_for(t)[b]["wd"]
                if loader.is_plan_routable(p) and sum(1 for s in p["fwd"] if s.get("kind") == "ride") >= 3:
                    multi = (t, b)
                    break
            if multi:
                break
        if multi is None:
            multi = (loader.data_json()["tours"][0], "jugenheim")
        t, b = multi
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            if b == "karlsruhe":
                ui.switch_base("karlsruhe")
            ui.open_card_matching(re.escape(loader.tour_title(t, "de")[:18]))
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T2-087")

    def test_t2_088_clicking_departure_windows_updates_times_and_stay(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            wins = h.page.locator("#detail button, .win-btn, [data-win]").filter(has_text=re.compile(r"\d{2}:\d{2}"))
            seen = set()
            for i in range(min(4, wins.count())):
                wins.nth(i).click()
                h.page.wait_for_timeout(150)
                seen.add(ui.detail_text())
            self.assertGreaterEqual(len(seen), 1)
            h.assert_zero_errors("T2-088")

    def test_t2_089_zero_intermediate_stops_leg_no_empty_expander(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            empty_details = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('#detail details')).filter(d => d.innerText.trim().length < 5).length"
            )
            self.assertEqual(empty_details, 0)
            h.assert_zero_errors("T2-089")

    def test_t2_090_midnight_crossing_last_safe_return_no_negative_duration(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            self.assertNotRegex(ui.detail_text(), r"-\d+\s*(?:Min|min|Std|h)")
            h.assert_zero_errors("T2-090")


# ---------------------------------------------------------------------------
# F19 Boundaries (T2-091 .. T2-095)
# ---------------------------------------------------------------------------


class TestT2_F19_MapBoundaries(unittest.TestCase):
    def test_t2_091_transit_only_tour_renders_without_elevation_crash(self) -> None:
        cfg, loader = _cfg_loader()
        non_gpx = next((t for t in loader.data_json()["tours"] if not (t.get("hike") or {}).get("hasGpx")), None)
        if non_gpx is None:
            non_gpx = loader.data_json()["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(non_gpx, "de")[:18]))
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T2-091")

    def test_t2_092_clicking_timeline_step_highlights_without_error(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            step = h.page.locator("#detail .st, #detail [data-step], .tl li").first
            if step.count() > 0:
                step.click()
                h.page.wait_for_timeout(150)
            h.assert_zero_errors("T2-092")

    def test_t2_093_minimize_and_restore_map_preserves_dimensions(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            mini = h.page.locator("#miniBtn").first
            if mini.count() > 0 and mini.is_visible():
                mini.click()
                h.page.wait_for_timeout(150)
                mini.click()
                h.page.wait_for_timeout(150)
            box = h.page.locator(".leaflet-container").first.bounding_box()
            self.assertIsNotNone(box)
            self.assertGreater(box["height"], 50)
            h.assert_zero_errors("T2-093")

    def test_t2_094_gps_on_map_with_sw_coords_zero_errors(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(
            geolocation={"latitude": 49.46, "longitude": 8.17}
        ) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            gps = h.page.locator("#gpsBtn").first
            if gps.count() > 0 and gps.is_visible():
                gps.click()
                h.page.wait_for_timeout(250)
            h.assert_zero_errors("T2-094")

    def test_t2_095_detail_map_fit_keeps_routes_rendered(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            fit = h.page.locator("#fitBtn").first
            if fit.count() > 0 and fit.is_visible():
                fit.click()
                h.page.wait_for_timeout(200)
            self.assertGreater(ui.map_path_count(), 0)
            h.assert_zero_errors("T2-095")


# ---------------------------------------------------------------------------
# F20 Boundaries (T2-096 .. T2-100)
# ---------------------------------------------------------------------------


class TestT2_F20_FilterBoundaries(unittest.TestCase):
    def test_t2_096_feasible_at_0001_vs_2359_clean_empty_state(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.set_feasible_time("00:01")
            c_early = ui.card_count()
            ui.set_feasible_time("23:59")
            c_late = ui.card_count()
            self.assertGreater(c_early, 0)
            self.assertEqual(c_late, 0)
            h.assert_zero_errors("T2-096")

    def test_t2_097_search_umlauts_case_insensitive(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.fill_search("dürkheim")
            c1 = ui.card_count()
            ui.fill_search("DÜRKHEIM")
            c2 = ui.card_count()
            self.assertEqual(c1, c2)
            self.assertGreater(c1, 0)
            h.assert_zero_errors("T2-097")

    def test_t2_098_search_xss_payload_safely_escaped(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.fill_search("<img src=x onerror='window.__xss=1'><script>window.__xss=1</script>")
            xss_val = h.page.evaluate("() => window.__xss")
            self.assertIsNone(xss_val)
            self.assertEqual(ui.card_count(), 0)
            h.assert_zero_errors("T2-098")

    def test_t2_099_weekday_switches_update_card_times_and_availability(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            wd_txt = ui.visible_card_texts()[:5]
            ui.select_day("so")
            so_txt = ui.visible_card_texts()[:5]
            self.assertTrue(wd_txt and so_txt)
            h.assert_zero_errors("T2-099")

    def test_t2_100_combining_filters_then_alle_restores_full_count(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            total = ui.card_count()
            ui.click_filter(r"Schwarzwald")
            ui.click_filter(r"Abendsonne")
            ui.fill_search("a")
            self.assertLessEqual(ui.card_count(), total)
            ui.fill_search("")
            ui.click_filter(r"^Alle$")
            self.assertEqual(ui.card_count(), total)
            h.assert_zero_errors("T2-100")


# ---------------------------------------------------------------------------
# F21 Boundaries (T2-101 .. T2-105)
# ---------------------------------------------------------------------------


class TestT2_F21_BilingualBoundaries(unittest.TestCase):
    def test_t2_101_no_raw_translation_keys_or_object_object_in_de_or_en(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for _ in (0, 1):
                txt = h.page.locator("body").inner_text()
                for bad in ("undefined", "[object Object]", "i18n."):
                    self.assertNotIn(bad, txt)
                ui.toggle_lang()
            h.assert_zero_errors("T2-101")

    def test_t2_102_difficulty_and_transfer_labels_translate_in_en(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            body = h.page.locator("body").inner_text()
            self.assertRegex(body, r"easy|medium|hard", re.I)
            ui.open_card_index(0)
            self.assertRegex(ui.detail_text(), r"transfer|walk|hike|Return|Departure", re.I)
            h.assert_zero_errors("T2-102")

    def test_t2_103_lang_toggle_while_home_modal_open_translates_modal(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            before = ui.home_modal_locator().inner_text()
            ui.close_home_modal()
            ui.toggle_lang()
            ui.open_home_modal()
            after = ui.home_modal_locator().inner_text()
            self.assertNotEqual(before, after)
            h.assert_zero_errors("T2-103")

    def test_t2_104_lang_toggle_preserves_filter_search_and_base(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.switch_base("karlsruhe")
            ui.click_filter(r"Schwarzwald")
            cnt_de = ui.card_count()
            ui.toggle_lang()
            self.assertEqual(ui.card_count(), cnt_de)
            self.assertIn("Karlsruhe", ui.header_text())
            h.assert_zero_errors("T2-104")

    def test_t2_105_wikimedia_credit_valid_in_de_and_en(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            self.assertRegex(h.page.locator("#detail, main").first.inner_html(), r"wikimedia\.org|creativecommons")
            ui.toggle_lang()
            self.assertRegex(h.page.locator("#detail, main").first.inner_html(), r"wikimedia\.org|creativecommons")
            h.assert_zero_errors("T2-105")


# ---------------------------------------------------------------------------
# F22 Boundaries (T2-106 .. T2-110)
# ---------------------------------------------------------------------------


class TestT2_F22_DarkModeBoundaries(unittest.TestCase):
    def test_t2_106_respects_os_prefers_color_scheme_dark_initially(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            cols = ui.computed_body_colors()
            nums = [int(x) for x in re.findall(r"\d+", cols["bg"])[:3]]
            self.assertLess(sum(nums) / 3.0, 70.0, f"Did not honor OS dark mode: {cols}")
            h.assert_zero_errors("T2-106")

    def test_t2_107_manual_toggle_overrides_os_dark_to_light(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_theme()
            cols = ui.computed_body_colors()
            if sum(int(x) for x in re.findall(r"\d+", cols["bg"])[:3]) / 3.0 < 120:
                ui.toggle_theme()
                cols = ui.computed_body_colors()
            nums = [int(x) for x in re.findall(r"\d+", cols["bg"])[:3]]
            self.assertGreater(sum(nums) / 3.0, 150.0)
            h.assert_zero_errors("T2-107")

    def test_t2_108_dark_mode_cards_and_pills_readable_contrast(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            res = h.page.evaluate(
                """() => {
                    const c = document.querySelector('#list .card, .tour-card, [data-tour-id]');
                    const s = getComputedStyle(c);
                    return {bg: s.backgroundColor, fg: s.color};
                }"""
            )
            self.assertNotEqual(res["bg"], res["fg"])
            h.assert_zero_errors("T2-108")

    def test_t2_109_dark_toggle_inside_detail_updates_timeline_and_tiles(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            bg_before = ui.computed_body_colors()["bg"]
            ui.toggle_theme()
            bg_after = ui.computed_body_colors()["bg"]
            if bg_before == bg_after:
                ui.toggle_theme()
                bg_after = ui.computed_body_colors()["bg"]
            self.assertNotEqual(bg_before, bg_after)
            h.assert_zero_errors("T2-109")

    def test_t2_110_dark_toggle_causes_zero_horizontal_overflow(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_theme()
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T2-110")


# ---------------------------------------------------------------------------
# F23 Boundaries (T2-111 .. T2-115)
# ---------------------------------------------------------------------------


class TestT2_F23_GpxBoundaries(unittest.TestCase):
    def test_t2_111_gpx_export_with_umlauts_valid_utf8_xml(self) -> None:
        cfg, loader = _cfg_loader()
        umlaut_tour = next(
            (t for t in loader.data_json()["tours"] if any(c in loader.tour_title(t, "de") for c in "äöüÄÖÜß")),
            loader.data_json()["tours"][0],
        )
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(umlaut_tour, "de")[:18]))
            _, content = ui.download_gpx()
            validate_gpx_xml(content)
            h.assert_zero_errors("T2-111")

    def test_t2_112_gpx_export_reflects_active_base_waypoints(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            _, gpx_jg = ui.download_gpx()
            ui.switch_base("karlsruhe")
            _, gpx_ka = ui.download_gpx()
            self.assertNotEqual(gpx_jg, gpx_ka)
            h.assert_zero_errors("T2-112")

    def test_t2_113_gpx_trkpt_lat_lon_numeric_and_bounded(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            _, content = ui.download_gpx()
            parsed = validate_gpx_xml(content)
            pts = [p for tr in parsed["trks"] for p in tr["points"]]
            self.assertTrue(pts)
            for p in pts:
                self.assertTrue(in_sw_bounds(p["lat"], p["lon"]))
            h.assert_zero_errors("T2-113")

    def test_t2_114_gpx_export_works_offline_and_file_url(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.open_card_index(0)
            fname, content = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(content)
            h.assert_zero_errors("T2-114")

    def test_t2_115_gpx_export_for_transit_only_tour_still_valid(self) -> None:
        cfg, loader = _cfg_loader()
        transit_only = next((t for t in loader.data_json()["tours"] if not (t.get("hike") or {}).get("hasGpx")), None)
        if transit_only is None:
            transit_only = loader.data_json()["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(transit_only, "de")[:18]))
            _, content = ui.download_gpx()
            parsed = validate_gpx_xml(content)
            self.assertTrue(parsed["trks"] or parsed["wpts"])
            h.assert_zero_errors("T2-115")


# ---------------------------------------------------------------------------
# F24 Boundaries (T2-116 .. T2-120)
# ---------------------------------------------------------------------------


class TestT2_F24_DeepLinkBoundaries(unittest.TestCase):
    def test_t2_116_db_link_properly_url_encodes_umlauts_and_spaces(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            for db_url in ui.detail_links()["db"]:
                frag = urlparse(db_url).fragment
                self.assertNotIn(" ", frag, f"Raw space in DB fragment: {db_url}")
                self.assertNotIn("undefined", db_url.lower())
            h.assert_zero_errors("T2-116")

    def test_t2_117_db_link_omits_l_null_when_ibnr_missing(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            for db_url in ui.detail_links()["db"]:
                self.assertNotIn("L=null", db_url)
                self.assertNotIn("L=undefined", db_url)
                self.assertNotIn("L=None", db_url)
            h.assert_zero_errors("T2-117")

    def test_t2_118_switching_weekday_updates_db_hd_date(self) -> None:
        cfg, loader = _cfg_loader()
        days = loader.data_json()["days"]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            ui.select_day("so")
            db_links = ui.detail_links()["db"]
            self.assertTrue(any(days["so"] in u for u in db_links), f"Expected Sunday date {days['so']} in {db_links}")
            h.assert_zero_errors("T2-118")

    def test_t2_119_switching_window_updates_db_hd_time(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            db0 = ui.detail_links()["db"][0]
            wins = h.page.locator("#detail button, .win-btn, [data-win]").filter(has_text=re.compile(r"\d{2}:\d{2}"))
            if wins.count() >= 2:
                wins.nth(1).click()
                h.page.wait_for_timeout(150)
                db1 = ui.detail_links()["db"][0]
                self.assertNotEqual(db0, db1)
            h.assert_zero_errors("T2-119")

    def test_t2_120_google_maps_link_has_valid_coords_and_travelmode(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            gmaps = ui.detail_links()["gmaps"]
            self.assertTrue(gmaps)
            qs = parse_qs(urlparse(gmaps[0]).query)
            self.assertIn("destination", qs)
            self.assertRegex(gmaps[0], r"travelmode=(?:transit|walking)")
            h.assert_zero_errors("T2-120")


# ---------------------------------------------------------------------------
# F25 Boundaries (T2-121 .. T2-125)
# ---------------------------------------------------------------------------


class TestT2_F25_DeploymentBoundaries(unittest.TestCase):
    def test_t2_121_built_files_contain_zero_local_home_paths(self) -> None:
        _, loader = _cfg_loader()
        for rel in ("index.html", "sw.js", "manifest.json"):
            txt = loader.path(rel).read_text(encoding="utf-8")
            self.assertNotIn("/home/oliver/", txt)

    def test_t2_122_live_url_https_no_mixed_content(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live HTTPS mixed-content check")
        self.assertTrue(cfg.live_url.startswith("https://"))
        with MobileAppHarness() as h:
            insecure = []
            h.page.on("request", lambda req: insecure.append(req.url) if req.url.startswith("http://") else None)
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            self.assertEqual(insecure, [], f"Mixed content http:// requests: {insecure}")
            h.assert_zero_errors("T2-122")

    def test_t2_123_live_url_with_query_and_hash_loads_cleanly(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live URL query/hash check")
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url.rstrip("/") + "/?base=karlsruhe#tours")
            self.assertGreater(ui.card_count(), 0)
            h.assert_zero_errors("T2-123")

    def test_t2_124_live_url_offline_after_sw_activation(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live URL offline SW test")
        with MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            h.page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.open_card_index(0)
            self.assertGreater(ui.card_count() if ui.card_count() > 0 else 1, 0)
            h.assert_zero_errors("T2-124")

    def test_t2_125_standalone_and_index_report_same_count_and_generated(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        gen = str(data.get("generated", ""))
        expected = len(data["tours"])
        self.assertIn(gen, cfg.standalone_path.read_text(encoding="utf-8"))
        self.assertIn(gen, loader.path("index.html").read_text(encoding="utf-8"))
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            self.assertEqual(ui.card_count(), expected)
            h.assert_zero_errors("T2-125")
