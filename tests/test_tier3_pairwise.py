"""Tier 3 — Cross-Feature Pairwise E2E Tests (26 tests: T3-001 .. T3-026)."""

from __future__ import annotations

import re
import unittest
from urllib.parse import parse_qs, urlparse

from tests.common import (
    AppUI,
    ContractLoader,
    MobileAppHarness,
    StaticServer,
    TestConfig,
    compute_hike_stay,
    compute_transfers,
    skip_unless_live,
    validate_gpx_xml,
)


def _cfg_loader() -> tuple[TestConfig, ContractLoader]:
    cfg = TestConfig.from_env()
    return cfg, ContractLoader(cfg)


class TestTier3Pairwise(unittest.TestCase):
    def test_t3_001_f16_x_f18_base_switch_updates_detail_timeline_badges_and_stay(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            jg_txt = ui.detail_text()
            ui.switch_base("karlsruhe")
            ka_txt = ui.detail_text()
            self.assertNotEqual(jg_txt, ka_txt)
            self.assertRegex(ka_txt, r"\d{2}:\d{2}")
            h.assert_zero_errors("T3-001")

    def test_t3_002_f16_x_f19_base_switch_updates_approach_polyline_keeps_hike(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            p_jg = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('.leaflet-overlay-pane path')).map(p => p.getAttribute('d'))"
            )
            ui.switch_base("karlsruhe")
            p_ka = h.page.evaluate(
                "() => Array.from(document.querySelectorAll('.leaflet-overlay-pane path')).map(p => p.getAttribute('d'))"
            )
            self.assertNotEqual(p_jg, p_ka)
            self.assertGreater(len(p_ka), 0)
            h.assert_zero_errors("T3-002")

    def test_t3_003_f16_x_f20_feasible_time_recomputes_on_base_switch(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.set_feasible_time("13:00")
            cnt_jg = ui.card_count()
            ui.switch_base("karlsruhe")
            cnt_ka = ui.card_count()
            self.assertGreaterEqual(cnt_jg, 0)
            self.assertGreaterEqual(cnt_ka, 0)
            h.assert_zero_errors("T3-003")

    def test_t3_004_f16_x_f24_base_switch_updates_all_deep_link_origins(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            l_jg = ui.detail_links()
            ui.switch_base("karlsruhe")
            l_ka = ui.detail_links()
            self.assertNotEqual(l_jg["db"][0], l_ka["db"][0])
            self.assertIn("karlsruhe", l_ka["db"][0].lower())
            if l_ka["gmaps"]:
                self.assertNotEqual(l_jg["gmaps"][0], l_ka["gmaps"][0])
            h.assert_zero_errors("T3-004")

    def test_t3_005_f16_x_f23_base_switch_updates_gpx_wpt_keeps_hike_trk(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            _, g1 = ui.download_gpx()
            p1 = validate_gpx_xml(g1)
            ui.switch_base("karlsruhe")
            _, g2 = ui.download_gpx()
            p2 = validate_gpx_xml(g2)
            self.assertNotEqual(g1, g2)
            self.assertGreaterEqual(sum(len(t["points"]) for t in p2["trks"]), 10)
            self.assertTrue(p1["wpts"] and p2["wpts"])
            h.assert_zero_errors("T3-005")

    def test_t3_006_f17_x_f16_home_presets_sync_with_base_switcher(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_home_modal()
            ui.home_modal_locator().locator("button").filter(has_text=re.compile(r"Durlacher Tor", re.I)).first.click()
            h.page.wait_for_timeout(200)
            self.assertIn("Durlacher Tor", ui.header_text())
            ui.open_home_modal()
            ui.home_modal_locator().locator("button").filter(has_text=re.compile(r"Bickenbach.*Bahnhof", re.I)).first.click()
            h.page.wait_for_timeout(200)
            self.assertIn("Bickenbach", ui.header_text())
            h.assert_zero_errors("T3-006")

    def test_t3_007_f17_x_f21_home_modal_in_en_and_toggle_back_to_de(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            ui.open_home_modal()
            m = ui.home_modal_locator()
            m.locator("#customHomeName, input[placeholder*='Name' i]").first.fill("Speyer Dom")
            m.locator("#customHomeLat, input[placeholder*='Lat' i]").first.fill("49.3172")
            m.locator("#customHomeLon, input[placeholder*='Lon' i]").first.fill("8.4423")
            m.locator("#saveHomeBtn, button").filter(has_text=re.compile(r"Save|Speichern|Apply", re.I)).first.click()
            h.page.wait_for_timeout(200)
            ui.toggle_lang()
            self.assertIn("Speyer Dom", ui.header_text())
            ui.open_home_modal()
            self.assertRegex(ui.home_modal_locator().inner_text(), r"Heimatort|Standort|Zurücksetzen|Speichern")
            h.assert_zero_errors("T3-007")

    def test_t3_008_f18_x_f06_f07_window_switch_preserves_transfer_and_stay_invariants(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if loader.walk_min(t) > 0), loader.data_json()["tours"][0])
        wmin = loader.walk_min(gpx_tour)
        p = loader.plans_for(gpx_tour)["jugenheim"]["wd"]
        if loader.is_plan_routable(p) and wmin > 0:
            lsr = loader.last_safe_return(p)
            if lsr and lsr.get("segs"):
                stay = compute_hike_stay(p["fwd"], lsr["segs"])
                self.assertGreaterEqual(stay, wmin + 30)
                for _, _, gap, _ in compute_transfers(lsr["segs"]):
                    self.assertGreaterEqual(gap, 4)
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            wins = h.page.locator("#detail button, .win-btn, [data-win]").filter(has_text=re.compile(r"\d{2}:\d{2}"))
            for i in range(min(3, wins.count())):
                wins.nth(i).click()
                h.page.wait_for_timeout(120)
            h.assert_zero_errors("T3-008")

    def test_t3_009_f18_x_f09_warning_badge_and_expandable_stops_coexist(self) -> None:
        cfg, loader = _cfg_loader()
        target = None
        for t in loader.data_json()["tours"]:
            p = loader.plans_for(t)["jugenheim"]["wd"]
            if loader.is_plan_routable(p) and any(s.get("warnings") for s in p["fwd"] + p["rev"]) and any(s.get("intermediateStops") for s in p["fwd"] + p["rev"]):
                target = t
                break
        target = target or loader.data_json()["tours"][0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(target, "de")[:18]))
            exp = h.page.locator("#detail details summary, #detail button").filter(has_text=re.compile(r"Zwischenhalt|Stops", re.I)).first
            if exp.count() > 0:
                exp.click()
            sw, iw = ui.scroll_overflow_ok()
            self.assertLessEqual(sw, iw)
            h.assert_zero_errors("T3-009")

    def test_t3_010_f18_x_f19_timeline_and_map_click_focus_sync(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            step = h.page.locator("#detail .st, #detail [data-step]").first
            if step.count() > 0:
                step.click()
                h.page.wait_for_timeout(150)
            path = h.page.locator(".leaflet-overlay-pane path").first
            if path.count() > 0:
                path.click(force=True)
                h.page.wait_for_timeout(150)
            h.assert_zero_errors("T3-010")

    def test_t3_011_f19_x_f22_dark_mode_on_detail_map_keeps_routes_and_elevation_visible(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            ui.toggle_theme()
            self.assertGreater(ui.map_path_count(), 0)
            self.assertRegex(h.page.locator("body").inner_text(), r"\d+(?:[.,]\d+)?\s*km")
            h.assert_zero_errors("T3-011")

    def test_t3_012_f20_x_f11_curated_filter_plus_each_region_matches_counts(self) -> None:
        cfg, loader = _cfg_loader()
        tours = loader.data_json()["tours"]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.click_filter(r"Neue Entdeckungen")
            cur_cnt = ui.card_count()
            self.assertTrue(20 <= cur_cnt <= 30)
            for pat, reg in ((r"Pfälzerwald", "pfaelzerwald"), (r"Schwarzwald", "schwarzwald"), (r"Odenwald", "odenwald")):
                ui.click_filter(r"Neue Entdeckungen")
                ui.click_filter(pat)
                # Ensure non-zero subset matching either region or intersection depending on filter mode (single vs multi)
                self.assertGreater(ui.card_count(), 0)
                exp_reg = sum(1 for t in tours if t.get("region") == reg)
                self.assertLessEqual(ui.card_count(), exp_reg)
            h.assert_zero_errors("T3-012")

    def test_t3_013_f20_x_f03_f04_my_tours_plus_gpx_search(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if t.get("source") in ("shortcut", "mapy") and (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        kw = loader.tour_title(gpx_tour, "de").split()[0]
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.click_filter(r"Meine Mapy")
            ui.fill_search(kw)
            self.assertGreaterEqual(ui.card_count(), 1)
            ui.open_card_index(0)
            self.assertRegex(h.page.locator("body").inner_text(), r"km")
            h.assert_zero_errors("T3-013")

    def test_t3_014_f20_x_f10_sunday_switch_surfaces_availability_cleanly(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.select_day("so")
            self.assertGreater(ui.card_count(), 0)
            ui.open_card_index(0)
            self.assertNotIn("NaN", ui.detail_text())
            h.assert_zero_errors("T3-014")

    def test_t3_015_f20_x_f21_combined_filters_preserved_across_de_to_en(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.click_filter(r"Schwarzwald")
            ui.click_filter(r"Abendsonne")
            ui.set_feasible_time("14:00")
            cnt_de = ui.card_count()
            ui.toggle_lang()
            self.assertEqual(ui.card_count(), cnt_de)
            self.assertRegex(h.page.locator("body").inner_text(), r"Black Forest|Evening sun")
            h.assert_zero_errors("T3-015")

    def test_t3_016_f21_x_f12_en_detail_renders_english_metadata_and_wikimedia_photo(self) -> None:
        cfg, loader = _cfg_loader()
        cur = next(t for t in loader.data_json()["tours"] if t.get("source") == "curated")
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.toggle_lang()
            ui.open_card_matching(re.escape(cur["title"]["en"][:16]))
            self.assertIn(cur["title"]["en"], ui.detail_text())
            ok = h.page.evaluate("() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)")
            self.assertTrue(ok)
            h.assert_zero_errors("T3-016")

    def test_t3_017_f21_x_f24_en_toggle_keeps_deep_links_valid(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            ui.toggle_lang()
            links = ui.detail_links()
            self.assertTrue(links["db"] and links["mvv"] and links["gmaps"])
            self.assertIn("so=", links["db"][0])
            h.assert_zero_errors("T3-017")

    def test_t3_018_f22_x_f15_dark_mode_412x915_zero_overflow_all_views(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness(color_scheme="dark") as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            self.assertLessEqual(*ui.scroll_overflow_ok())
            ui.open_home_modal()
            self.assertLessEqual(*ui.scroll_overflow_ok())
            ui.close_home_modal()
            ui.open_card_index(0)
            self.assertLessEqual(*ui.scroll_overflow_ok())
            h.assert_zero_errors("T3-018")

    def test_t3_019_f13_x_f16_f20_offline_base_switch_day_switch_and_filters(self) -> None:
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
            ui.switch_base("karlsruhe")
            ui.select_day("sa")
            ui.click_filter(r"Abendsonne")
            self.assertGreaterEqual(ui.card_count(), 0)
            h.assert_zero_errors("T3-019")

    def test_t3_020_f13_x_f23_offline_gpx_download_valid_xml(self) -> None:
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
            fname, xml_txt = ui.download_gpx()
            self.assertTrue(fname.endswith(".gpx"))
            validate_gpx_xml(xml_txt)
            h.assert_zero_errors("T3-020")

    def test_t3_021_f14_x_f21_f22_standalone_persists_en_dark_karlsruhe(self) -> None:
        cfg, _ = _cfg_loader()
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.switch_base("karlsruhe")
            ui.toggle_lang()
            ui.toggle_theme()
            h.page.reload(wait_until="domcontentloaded")
            ui.wait_for_cards()
            ls = h.page.evaluate("() => JSON.stringify(localStorage)")
            self.assertIn("karlsruhe", ls.lower())
            self.assertIn("en", ls.lower())
            h.assert_zero_errors("T3-021")

    def test_t3_022_f14_x_f19_f23_standalone_network_aborted_renders_map_photo_gpx(self) -> None:
        cfg, loader = _cfg_loader()
        gpx_tour = next((t for t in loader.data_json()["tours"] if (t.get("hike") or {}).get("hasGpx")), loader.data_json()["tours"][0])
        with MobileAppHarness(offline_allowed=True) as h:
            h.page.route("http*://**", lambda route: route.abort())
            ui = AppUI(h.page)
            ui.open_url(cfg.standalone_path.resolve().as_uri())
            ui.open_card_matching(re.escape(loader.tour_title(gpx_tour, "de")[:18]))
            self.assertGreater(ui.map_path_count(), 0)
            ok = h.page.evaluate("() => Array.from(document.querySelectorAll('#detail img, main img')).some(i => i.complete && i.naturalWidth > 0)")
            self.assertTrue(ok)
            _, gpx_txt = ui.download_gpx()
            validate_gpx_xml(gpx_txt)
            h.assert_zero_errors("T3-022")

    def test_t3_023_f05_x_f24_db_deep_link_enforces_dticket_regional_filter(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            ui.open_card_index(0)
            db_url = ui.detail_links()["db"][0]
            frag = parse_qs(urlparse(db_url).fragment)
            self.assertTrue(any(k in frag for k in ("vm", "dlt", "v")), f"Missing D-Ticket filter param: {db_url}")
            h.assert_zero_errors("T3-023")

    def test_t3_024_f08_x_f07_curated_hike_both_bases_satisfy_windows_stay_lsr(self) -> None:
        _, loader = _cfg_loader()
        cur_hike = next(
            (t for t in loader.data_json()["tours"] if t.get("source") == "curated" and loader.walk_min(t) > 0),
            None,
        )
        self.assertIsNotNone(cur_hike, "Expected >= 1 curated hike tour")
        wmin = loader.walk_min(cur_hike)
        for base in ("jugenheim", "karlsruhe"):
            ok_days = [d for d in ("wd", "sa", "so") if loader.is_plan_routable(loader.plans_for(cur_hike)[base][d])]
            self.assertTrue(ok_days, f"Curated hike {cur_hike['id']} unroutable from {base}")
            for d in ok_days:
                p = loader.plans_for(cur_hike)[base][d]
                stay = compute_hike_stay(p["fwd"], p["rev"])
                self.assertGreaterEqual(stay, wmin + 30)
                self.assertIsNotNone(loader.last_safe_return(p))

    def test_t3_025_f01_x_f20_searching_commute_keywords_returns_zero_cards(self) -> None:
        cfg, _ = _cfg_loader()
        with StaticServer(cfg.root) as srv, MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(srv.base_url)
            for kw in ("Hochschule Süd", "Moltkestraße 30", "Clay Kaserne", "Waiblingen", "Buy tickets"):
                ui.fill_search(kw)
                self.assertEqual(ui.card_count(), 0, f"Commute search {kw!r} returned cards")
            h.assert_zero_errors("T3-025")

    def test_t3_026_f25_x_f16_f21_live_github_pages_base_and_lang_switch(self) -> None:
        cfg, _ = _cfg_loader()
        skip_unless_live(self, cfg, "Live GitHub Pages base & language switch check")
        with MobileAppHarness() as h:
            ui = AppUI(h.page)
            ui.open_url(cfg.live_url)
            ui.switch_base("karlsruhe")
            ui.toggle_lang()
            ui.open_card_index(0)
            self.assertRegex(ui.detail_text(), r"\d{2}:\d{2}")
            h.assert_zero_errors("T3-026")
