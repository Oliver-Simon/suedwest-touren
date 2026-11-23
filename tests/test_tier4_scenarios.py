"""Tier 4 — Real-World End-to-End Scenario Tests (13 tests: T4-001 .. T4-013)."""

from __future__ import annotations

import json
import re
import unittest

from tests.common import (
    AppUI,
    ContractLoader,
    MobileAppHarness,
    StaticServer,
    TestConfig,
    compute_hike_stay,
    compute_transfers,
    is_forbidden_fernverkehr,
    skip_unless_live,
    validate_gpx_xml,
)


def _cfg_loader() -> tuple[TestConfig, ContractLoader]:
    cfg = TestConfig.from_env()
    return cfg, ContractLoader(cfg)


class TestTier4Scenarios(unittest.TestCase):
    def test_t4_001_weekend_pfaelzerwald_hike_from_jugenheim(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.select_day("sa")
            ui.click_filter(r"Pfälzerwald")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            txt = ui.detail_text()
            self.assertRegex(txt, r"\d{2}:\d{2}")
            self.assertGreater(ui.map_path_count(), 0)
            exp = h.page.locator("#detail details summary, #detail button").filter(
                has_text=re.compile(r"Zwischenhalt|Stops", re.I)
            ).first
            if exp.count() > 0:
                exp.click()
            fname, gpx_txt = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(gpx_txt)
            links = ui.detail_links()
            self.assertTrue(links["db"], "Missing DB deep link")
            self.assertIn("#sts=true", links["db"][0])
            h.assert_zero_errors("T4-001")

    def test_t4_002_spontaneous_after_work_evening_sun_from_karlsruhe(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.switch_base("karlsruhe")
            ui.select_day("wd")
            ui.click_filter(r"Abendsonne")
            ui.set_feasible_time("15:30")
            if ui.card_count() == 0:
                ui.set_feasible_time("12:00")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            links = ui.detail_links()
            self.assertTrue(links["gmaps"] and links["mvv"])
            h.assert_zero_errors("T4-002")

    def test_t4_003_visiting_heidelberg_home_modal_and_dark_mode(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            ui.home_modal_locator().locator("button").filter(has_text=re.compile(r"Heidelberg Hbf", re.I)).first.click()
            h.page.wait_for_timeout(200)
            self.assertIn("Heidelberg", ui.header_text())
            ui.toggle_theme()
            ui.click_filter(r"Odenwald")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            loaded = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)"
            )
            self.assertTrue(loaded)
            ui.go_back()
            ui.open_home_modal()
            ui.home_modal_locator().locator("#resetHomeBtn, button").filter(
                has_text=re.compile(r"Zurücksetzen|Reset", re.I)
            ).first.click()
            h.assert_zero_errors("T4-003")

    def test_t4_004_english_guest_full_journey_with_reload_persistence(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.switch_base("karlsruhe")
            ui.toggle_lang()
            ui.click_filter(r"New Discoveries")
            ui.click_filter(r"Black Forest")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            self.assertNotIn("undefined", ui.detail_text())
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertIn("Karlsruhe", ui.header_text())
            self.assertRegex(h.page.locator("body").inner_text(), r"Black Forest|New Discoveries")
            h.assert_zero_errors("T4-004")

    def test_t4_005_true_offline_trail_usage_in_pwa(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            h.page.evaluate("() => navigator.serviceWorker ? navigator.serviceWorker.ready.then(() => true) : false")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.open_card_index(0)
            ui.go_back()
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ui.click_filter(r"Meine Mapy")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            self.assertGreater(ui.map_path_count(), 0)
            _, gpx_txt = ui.download_gpx()
            validate_gpx_xml(gpx_txt)
            h.assert_zero_errors("T4-005")

    def test_t4_006_standalone_single_file_full_offline_journey(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness(offline_allowed=True) as h:
            h.page.route("http*://**", lambda route: route.abort())
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.switch_base("karlsruhe")
            ui.select_day("sa")
            ui.click_filter(r"Schwarzwald")
            ui.click_filter(r"^Alle$")
            ui.fill_search("Bad")
            ui.fill_search("")
            ui.toggle_lang()
            ui.toggle_theme()
            ui.open_card_index(0)
            fname, gpx_txt = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(gpx_txt)
            self.assertEqual(h.external_requests, [])
            h.assert_zero_errors("T4-006")

    def test_t4_007_rural_sunday_and_ruftaxi_or_unroutable_journey(self) -> None:
        cfg, loader = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.select_day("so")
            ui.open_card_index(0)
            ui.select_day("sa")
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            h.assert_zero_errors("T4-007")

    def test_t4_008_exhaustive_catalog_and_ui_smoke_sweep(self) -> None:
        cfg, loader = _cfg_loader()
        data = loader.data_json()
        for t in data["tours"]:
            wmin = loader.walk_min(t)
            for b in ("jugenheim", "karlsruhe"):
                for d in ("wd", "sa", "so"):
                    p = loader.plans_for(t)[b][d]
                    if not loader.is_plan_routable(p):
                        continue
                    for seg in p["fwd"] + p["rev"]:
                        self.assertIsNone(is_forbidden_fernverkehr(seg))
                    for _, _, gap, _ in compute_transfers(p["fwd"]) + compute_transfers(p["rev"]):
                        self.assertGreaterEqual(gap, 4)
                    if wmin > 0:
                        self.assertGreaterEqual(compute_hike_stay(p["fwd"], p["rev"]), wmin + 30)
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for idx in range(min(10, ui.card_count())):
                ui.open_card_index(idx)
                ok = h.page.evaluate(
                    "() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)"
                )
                self.assertTrue(ok)
                ui.go_back()
            h.assert_zero_errors("T4-008")

    def test_t4_009_map_timeline_navigation_and_gps_tracking(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        sc = (gpx_tour.get("hike") or {}).get("startCoord") or [49.46, 8.17]
        with StaticServer(cfg.root) as srv, MobileAppHarness(
            geolocation={"latitude": float(sc[0]), "longitude": float(sc[1])}
        ) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            for sel in ("#layerBtn", "#fitBtn", "#fullBtn", "#miniBtn", "#gpsBtn"):
                b = h.page.locator(sel).first
                if b.count() > 0 and b.is_visible():
                    b.click()
                    h.page.wait_for_timeout(120)
            h.assert_zero_errors("T4-009")

    def test_t4_010_commute_hygiene_and_gpx_dedup_e2e_audit(self) -> None:
        cfg, loader = _cfg_loader()
        excl_blob = json.dumps(loader.excluded_shortcuts(), ensure_ascii=False).lower()
        for kw in ("hochschule süd", "moltkestraße 30", "clay kaserne", "stuttgart universität", "waiblingen", "buy tickets"):
            self.assertIn(kw, excl_blob)
        cat = loader.tours_catalog()
        self.assertEqual(len(cat.get("waypoints") or []), 19)
        loehrbach = [
            gf
            for t in cat.get("tours", [])
            for gf in ((t.get("hike") or {}).get("gpxFiles") or [])
            if "löhrbach" in gf.lower() and "abtsteinach" in gf.lower()
        ]
        self.assertEqual(len(loehrbach), 1)
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for kw in ("Hochschule Süd", "Moltkestraße 30", "Clay Kaserne", "Buy tickets"):
                ui.fill_search(kw)
                self.assertEqual(ui.card_count(), 0)
            h.assert_zero_errors("T4-010")

    def test_t4_011_responsive_transitions_412_360_1280(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(viewport=(412, 915)) as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            for w, ht in ((412, 915), (360, 740), (1280, 800)):
                h.page.set_viewport_size({"width": w, "height": ht})
                h.page.wait_for_timeout(200)
                sw, iw = ui.scroll_overflow_ok()
                self.assertLessEqual(sw, iw, f"Overflow at {w}x{ht}: {sw} > {iw}")
            h.assert_zero_errors("T4-011")

    def test_t4_012_persistence_and_clean_state_recovery_across_reloads(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.switch_base("karlsruhe")
            ui.toggle_lang()
            ui.toggle_theme()
            ui.select_day("sa")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ls = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertIn("karlsruhe", ls.lower())
            self.assertIn("en", ls.lower())
            # Restore defaults
            ui.switch_base("jugenheim")
            ui.toggle_lang()
            ui.toggle_theme()
            ui.select_day("wd")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            h.assert_zero_errors("T4-012")

    def test_t4_013_live_github_pages_e2e_acceptance_journey(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live GitHub Pages full acceptance journey")
        with MobileAppHarness(offline_allowed=True) as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            h.page.evaluate("() => navigator.serviceWorker.ready.then(() => true)")
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            self.assertTrue(h.page.evaluate("() => Boolean(navigator.serviceWorker.controller)"))
            ui.switch_base("karlsruhe")
            ui.click_filter(r"Neue Entdeckungen")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            fname, gpx_txt = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(gpx_txt)
            links = ui.detail_links()
            self.assertTrue(links["db"] and links["mvv"] and links["gmaps"])
            h.context.set_offline(True)
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            h.assert_zero_errors("T4-013")
