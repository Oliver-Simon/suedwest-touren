#!/usr/bin/env python3
"""Integrate all 30 Deep Research Deutschlandticket tours into Südwest-Touren.

Phases (idempotent, incremental — preserves all 151 existing tours intact):
1. Build 30 tour definitions (24 multi-stop Rundkurse `shortcut_360` + 6 viewpoint
   short-walk tours `curated`) with verified Transitous stop metadata, updating
   `data/tours_catalog_full.json` (151 -> 181) without modifying M1 `tours_catalog.json`.
2. Fetch/crop 800x450 JPEGs + free-license Wikimedia Commons attributions via
   `scripts.curate_and_media.run_media_pipeline` (skips the 151 already-valid tours).
3. Compute timetabled middle stages (`data/midlegs.json`) for the 24 new Rundkurse
   using `planer.midlegs`, validating zero forbidden Fernverkehr/SNCF legs.
4. Compute dual-base Transitous/VRN timetables (`jugenheim` & `karlsruhe`,
   `wd`/`sa`/`so`), keeping `min_stay >= mid_dur_min + 25` on Rundkurse so default
   day itineraries have zero middle-stage conflicts, and merge cleanly into
   `data/data.json` + `data/routing_notes.json`.

Usage:
  python3 scripts/integrate_deep_research_tours.py [--catalog-only] [--skip-media] [--only-route]
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import planner  # noqa: E402
from planer import midlegs as midlegs_mod  # noqa: E402
from planer.slim import dedupe_and_sort_alts, slim_data_dict  # noqa: E402
from planer.validate import (  # noqa: E402
    compute_hike_stay,
    in_sw_bounds,
    is_forbidden_fernverkehr,
    minutes_diff,
    parse_hhmm,
)
from scripts.curate_and_media import IMG_DIR, run_media_pipeline, is_tour_media_valid  # noqa: E402

BERLIN_TZ = ZoneInfo("Europe/Berlin")
FULL_CATALOG_PATH = ROOT_DIR / "data" / "tours_catalog_full.json"
DATA_PATH = ROOT_DIR / "data" / "data.json"
NOTES_PATH = ROOT_DIR / "data" / "routing_notes.json"
MIDLEGS_PATH = ROOT_DIR / "data" / "midlegs.json"
CREDITS_PATH = ROOT_DIR / "img_src" / "credits.json"


def _stop(
    name: str,
    lat: float,
    lon: float,
    dhid: str,
    transitous_id: str | None = None,
    ibnr: str | None = None,
) -> dict[str, Any]:
    assert 47.0 <= lat <= 51.5 and 6.0 <= lon <= 11.0, f"Stop {name} out of SW bounds: ({lat}, {lon})"
    return {
        "name": name,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "dhid": dhid,
        "transitousId": transitous_id,
        "ibnr": ibnr,
    }


STOPS: dict[str, dict[str, Any]] = {
    "bad_herrenalb": _stop("Bad Herrenalb", 48.802450, 8.439070, "de:08235:9558", "de-KVV_Pde:08235:9558", "8007011"),
    "gernsbach": _stop("Gernsbach Bahnhof", 48.768307, 8.336714, "de:08216:32142", "de-DELFI_de:08216:32142"),
    "kirchzarten": _stop("Kirchzarten Bahnhof", 47.968132, 7.959909, "de:08315:6519", "de-DELFI_de:08315:6519"),
    "todtnau": _stop("Todtnau, Busbahnhof", 47.828804, 7.941925, "de:08336:3667", "de-DELFI_de:08336:3667:0:1"),
    "titisee": _stop("Titisee, Bahnhof", 47.903360, 8.155598, "de:08315:6548", "de-DELFI_de:08315:6548"),
    "bad_wildbad": _stop("Bad Wildbad, Kurpark", 48.748623, 8.550515, "de:08235:8961", "de-KVV_Pde:08235:8961"),
    "fds_stadt": _stop("Freudenstadt Stadtbahnhof", 48.467567, 8.410694, "de:08237:1002", "de-DELFI_de:08237:1002"),
    "fds_hbf": _stop("Freudenstadt Hauptbahnhof", 48.460323, 8.428803, "de:08237:1050", "de-DELFI_de:08237:1050"),
    "wolfach": _stop("Wolfach, Bahnhof", 48.294727, 8.221818, "de:08317:18010", "de-DELFI_de:08317:18010"),
    "rippoldsau": _stop("Bad Rippoldsau Kirche", 48.427200, 8.329550, "de:08237:2601", "de-DELFI_de:08237:2601:0:1"),
    "st_peter": _stop("St. Peter Zähringer Eck", 48.015820, 8.033903, "de:08315:262", "de-DELFI_de:08315:262:0:1"),
    "kandel": _stop("Waldkirch Kandel Rasthaus", 48.064890, 8.016503, "de:08316:401", "de-DELFI_de:08316:401:0:1"),
    "alzey": _stop("Alzey, Bahnhof", 49.750140, 8.109784, "de:07331:55", "de-DELFI_de:07331:55:1:1"),
    "bad_kreuznach": _stop("Bad Kreuznach, Bahnhof", 49.842400, 7.866435, "de:07133:8178", "de-DELFI_de:07133:8178_G"),
    "wald_michelbach": _stop("Wald-Michelbach, ZOB", 49.574020, 8.822947, "de:06431:3981", "de-DELFI_de:06431:3981:0:Ri O"),
    "hirschhorn": _stop("Hirschhorn, Bahnhof", 49.442670, 8.896055, "de:06431:1590", "de-DELFI_de:06431:1590_G"),
    "schiltach": _stop("Schiltach Bahnhof", 48.291180, 8.334875, "de:08325:6585", "de-DELFI_de:08325:6585_G"),
    "offenburg": _stop("Offenburg", 48.476475, 7.946723, "de:08317:14506", "de-DELFI_de:08317:14506_G"),
    "lauterecken": _stop("Lauterecken, P+R", 49.653710, 7.590710, "de:07336:8034", "de-DELFI_de:07336:8034_G"),
    "bad_sobernheim": _stop("Bad Sobernheim, Bahnhof", 49.782978, 7.649397, "de:07133:30624", "de-DELFI_de:07133:30624:1:1"),
    "boppard": _stop("Boppard Hauptbahnhof", 50.231422, 7.586014, "de:07140:060039", "de-DELFI_de:07140:060039"),
    "emmelshausen": _stop("Emmelshausen Bahnhof", 50.157673, 7.560494, "de:07140:060101", "de-DELFI_de:07140:060101"),
    "bingen_hbf": _stop("Bingen, Hauptbahnhof", 49.969147, 7.882717, "de:07339:8183", "de-DELFI_de:07339:8183:1:102"),
    "michelstadt": _stop("Michelstadt Bahnhof", 49.684856, 9.001453, "de:06437:11222", "de-DELFI_de:06437:11222"),
    "eberbach": _stop("Eberbach", 49.465607, 8.984476, "de:08226:534", "de-DELFI_de:08226:534:4:2"),
    "calw": _stop("Calw, Bahnhof/ZOB", 48.714480, 8.741811, "de:08235:9030", "de-DELFI_de:08235:9030"),
    "renningen": _stop("Renningen", 48.775120, 8.932350, "de:08115:7302", "de-DELFI_de:08115:7302"),
    "bad_bergzabern": _stop("Bad Bergzabern, Bahnhof", 49.103214, 8.003270, "de:07337:4561", "de-DELFI_de:07337:4561:1:1"),
    "landau_hbf": _stop("Landau (Pfalz) Hbf", 49.198223, 8.126150, "de:07313:8304", None, "8000216"),
    "wissembourg": _stop("Wissembourg Gare", 49.031647, 7.950014, "fr:24067:466", "de-DELFI_fr:24067:466"),
    "winden": _stop("Winden (Pfalz)", 49.095450, 8.122479, "de:07334:35000", "de-DELFI_de:07334:35000_G"),
    "lauterbourg": _stop("Lauterbourg", 48.967663, 8.182904, "fr:24067:479", "de-DELFI_fr:24067:479"),
    "speyer": _stop("Speyer, Hbf/ZOB", 49.324585, 8.427976, "de:07318:3603", "de-DELFI_de:07318:3603"),
    "schwetzingen_schloss": _stop("Schwetzingen, Schlossplatz", 49.383797, 8.572353, "de:08226:3499", "de-DELFI_de:08226:3499:0:1"),
    "bretten": _stop("Bretten", 49.036900, 8.693448, "de:08215:1551", "de-DELFI_de:08215:1551"),
    "maulbronn_kloster": _stop("Maulbronn Kloster", 49.000175, 8.812239, "de:08236:2213", "de-DELFI_de:08236:2213:1:1"),
    "muehlacker": _stop("Mühlacker ZOB", 48.953100, 8.845881, "de:08236:3090", "de-DELFI_de:08236:3090"),
    "wiesbaden": _stop("Wiesbaden Hauptbahnhof", 50.070790, 8.243731, "de:06414:6907", "de-DELFI_de:06414:6907"),
    "limburg": _stop("Limburg (Lahn) Bahnhof", 50.384117, 8.062342, "de:06533:11025", "de-DELFI_de:06533:11025"),
    "idstein": _stop("Idstein Bahnhof", 50.215990, 8.257264, "de:06439:11318", "de-DELFI_de:06439:11318"),
    "seebrugg": _stop("Seebrugg Bahnhof", 47.803486, 8.190991, "de:08315:6547", "de-DELFI_de:08315:6547"),
    "donaueschingen": _stop("Donaueschingen", 47.947872, 8.499141, "de:08326:6537", "de-DELFI_de:08326:6537"),
    "miltenberg": _stop("Miltenberg", 49.706400, 9.248388, "de:09676:1254", "de-DELFI_de:09676:1254"),
    "seckach": _stop("Seckach, Bahnhof", 49.441105, 9.338446, "de:08225:8203", "de-DELFI_de:08225:8203"),
    "bleibach": _stop("Bleibach Bahnhof", 48.124030, 8.005004, "de:08316:6503", "de-DELFI_de:08316:6503"),
    "furtwangen": _stop("Furtwangen Rößleplatz", 48.052654, 8.207332, "de:08326:1181", "de-DELFI_de:08326:1181:0:1"),
    "triberg": _stop("Triberg", 48.140034, 8.236886, "de:08326:6591", "de-DELFI_de:08326:6591_G"),
    "idar_oberstein": _stop("Idar-Oberstein, Bahnhof", 49.699425, 7.321180, "de:07134:35000", "de-DELFI_de:07134:35000:1:1"),
    "erbeskopf": _stop("Hilscheid, H'haus/Erbeskopf", 49.737354, 7.083108, "de:07231:40416", "de-DELFI_de:07231:40416"),
    "neubruecke": _stop("Neubrücke (Nahe), Bahnhof", 49.605920, 7.172790, "de:07134:35406", None),
    "staufen": _stop("Staufen (Brsg) Bahnhof", 47.885426, 7.728117, "de:08315:6526", "de-DELFI_de:08315:6526"),
    "breisach": _stop("Breisach Bahnhof", 48.027150, 7.589839, "de:08315:6504", "de-DELFI_de:08315:6504"),
    "villingen": _stop("Villingen Bahnhof/ZOB", 48.057804, 8.465526, "de:08326:6592", "de-DELFI_de:08326:6592"),
    "sigmaringen": _stop("Sigmaringen", 48.086983, 9.221851, "de:08437:42111", "de-DELFI_de:08437:42111_G"),
    "ulm": _stop("Ulm Hbf", 48.399345, 9.982933, "de:08421:1008", None, "8000170"),
    "aschaffenburg": _stop("Aschaffenburg Hbf", 49.980556, 9.143697, "de:09661:99082", "de-DELFI_de:09661:99082"),
    "seligenstadt": _stop("Seligenstadt Bahnhof", 50.041460, 8.967472, "de:06438:6601", "de-DELFI_de:06438:6601"),
    "esslingen": _stop("Esslingen (N)", 48.738540, 9.300520, "de:08116:7800", "de-DELFI_de:08116:7800"),
    "ludwigsburg": _stop("Ludwigsburg", 48.891860, 9.185420, "de:08118:7402", "de-DELFI_de:08118:7402"),
    "bad_muenster": _stop("Bad Münster, Bahnhof", 49.813427, 7.846989, "de:07133:8177", "de-DELFI_de:07133:8177"),
    "freiburg_oberlinden": _stop("Freiburg, Oberlinden", 47.993900, 7.853378, "de:08311:30118", "de-DELFI_de:08311:30118:0:1"),
    "weiler_sinsheim": _stop("Weiler, Ortsmitte", 49.211254, 8.874502, "de:08226:3569", "de-DELFI_de:08226:3569:1:RiN"),
    "neckarzimmern": _stop("Neckarzimmern", 49.316650, 9.137394, "de:08225:8223", "de-DELFI_de:08225:8223"),
    "bingen_stadt": _stop("Bingen, Stadtbahnhof", 49.969746, 7.902985, "de:07339:8199", "de-DELFI_de:07339:8199"),
    "koblenz_festung": _stop("Koblenz Ehrenbreitstein", 50.362050, 7.610030, "de:07111:010063", "de-DELFI_de:07111:010063:03:02_G"),
}


def _transit_hike_empty() -> dict[str, Any]:
    return {
        "hasGpx": False,
        "gpxFiles": [],
        "distKm": 0.0,
        "elevGainM": 0,
        "elevLossM": 0,
        "walkMin": 0,
        "startCoord": None,
        "endCoord": None,
        "poly5": "",
        "elevProfile": [],
    }


def _viewpoint_hike(dist_km: float, gain_m: int, loss_m: int, walk_min: int) -> dict[str, Any]:
    return {
        "hasGpx": False,
        "gpxFiles": [],
        "distKm": dist_km,
        "elevGainM": gain_m,
        "elevLossM": loss_m,
        "walkMin": walk_min,
        "startCoord": None,
        "endCoord": None,
        "poly5": "",
        "elevProfile": [],
    }


def _make_tour(
    tid: str,
    region: str,
    category: str,
    difficulty: str,
    ring: bool,
    evening_sun: bool,
    title_de: str,
    title_en: str,
    sub_de: str,
    sub_en: str,
    sun_de: str,
    sun_en: str,
    tips_de: str,
    tips_en: str,
    places: list[str],
    start_key: str,
    end_key: str,
    via_keys: list[str],
    kind: str,
    wikimedia_query: str,
    hike: dict[str, Any],
    walk_to_start: int = 0,
    walk_from_end: int = 0,
) -> dict[str, Any]:
    return {
        "id": tid,
        "source": "shortcut",
        "tag": "deep_research",
        "region": region,
        "category": category,
        "difficulty": difficulty,
        "ring": ring,
        "title": {"de": title_de, "en": title_en},
        "subtitle": {"de": sub_de, "en": sub_en},
        "sun": {"de": sun_de, "en": sun_en},
        "tips": {"de": tips_de, "en": tips_en},
        "eveningSun": evening_sun,
        "places": places,
        "transit": {
            "kind": kind,
            "startStop": copy.deepcopy(STOPS[start_key]),
            "endStop": copy.deepcopy(STOPS[end_key]),
            "vias": [copy.deepcopy(STOPS[k]) for k in via_keys],
            "walkToStartM": walk_to_start,
            "walkFromEndM": walk_from_end,
            "unlinkedReason": None,
        },
        "hike": hike,
        "wikimediaQuery": wikimedia_query,
    }


NEW_TOURS: list[dict[str, Any]] = [
    # --- 1..24: Multi-Stop Rundkurse (shortcut_360, walkMin=0, midlegs-enabled) ---
    _make_tour(
        "busrunde-albtal-kaeppele-murgtal-x44",
        "schwarzwald", "panoramatour", "leicht", True, True,
        "Albtal – Loffenau Käppele – Murgtal-Passrunde (S1 + X44 + S8)",
        "Alb Valley – Loffenau Käppele – Murg Valley Pass Loop (S1 + X44 + S8)",
        "Winterfeste Schwarzwald-Passrunde im Regiobus X44 über Käppele-Serpentinen nach Gernsbach & ins Murgtal",
        "All-year Black Forest pass loop via Regiobus X44 over Käppele hairpins to Gernsbach & Murg valley rail",
        "Freie Panoramablicke über Loffenau hinab ins Murgtal und zur Rheinebene; Gernsbacher Altstadt sonnig.",
        "Open panoramic views over Loffenau into the Murg Valley and Rhine plain; sunny Gernsbach old town.",
        "Ab Karlsruhe S1 nach Bad Herrenalb (ca. 40 Min.), dann KVV bwregiobus X44 über Loffenau Käppele nach Gernsbach (26 Min., täglich stündlich) und per Murgtalbahn S8 zurück.",
        "From Karlsruhe take S1 to Bad Herrenalb (~40 min), then KVV X44 bus over Loffenau Käppele to Gernsbach (26 min, hourly daily), returning via Murg Valley rail S8.",
        ["Bad Herrenalb", "Loffenau Käppele", "Loffenau Marktplatz", "Gernsbach Bahnhof", "Rastatt"],
        "bad_herrenalb", "gernsbach", [], "shortcut_360", "Teufelsmühle (Schwarzwald)", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-schauinsland-notschrei-feldberg-140-7300",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Hochschwarzwald-Zweipässe-Busrunde: Schauinsland/Notschrei & Feldberg (140 + 7300)",
        "High Black Forest Two-Pass Bus Loop: Notschrei & Feldberg Pass (140 + 7300)",
        "Zwei Hochschwarzwald-Pässe (1.119 m & 1.231 m) komplett per Linienbus ohne teure Bergbahn",
        "Traverse two High Black Forest mountain passes (1,119 m & 1,231 m) by regional bus in one loop",
        "Hochflächen an Notschrei/Halde und Feldbergpass mit weitem Belchen-, Vogesen- und Alpenpanorama.",
        "High-altitude panoramas across Notschrei and Feldberg Pass with Vosges and Alpine views.",
        "Schnellste Anfahrt ab Karlsruhe über RE7 Freiburg + S1 Kirchzarten; Bus 140 (ehem. 7215) über Notschrei nach Todtnau und Feldbergbus 7300 nach Titisee im Stundentakt.",
        "Take RE7 to Freiburg + S1 to Kirchzarten, then Bus 140 over Notschrei to Todtnau and Bus 7300 over Feldberg Pass to Titisee; all covered by Deutschlandticket.",
        ["Kirchzarten Bahnhof", "Schauinsland Halde", "Notschrei Passhöhe (1.119 m)", "Todtnau Busbahnhof", "Feldbergpass Hebelhof", "Titisee Bahnhof"],
        "kirchzarten", "titisee", ["todtnau"], "shortcut_360", "Notschrei", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-enztal-x78-murgtal",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Enztal – Hochschwarzwald-Plateau (X78) – Murgtal-Runde",
        "Enz Valley – Black Forest Plateau (X78) – Murg Valley Loop",
        "S6 Enztalbahn ab Bad Wildbad, im Erlebnisbus X78 über Enzklösterle & Besenfeld und per S8 durchs Murgtal",
        "S6 Enz Valley train to Bad Wildbad, scenic Erlebnisbus X78 across the plateau, and Murg Valley S8 return",
        "Abwechslungsreiche Wald- & Hochmoor-Passagen rund um Enzklösterle sowie freie Talblicke auf den Murg-Viadukten.",
        "Mixed pine plateaus around Enzklösterle paired with open viaduct views down the Murg Valley.",
        "X78 (ehem. 7780) fährt ganzjährig täglich stündlich von Bad Wildbad Kurpark nach Freudenstadt Stadtbf (~60 Min.); ideal kombinierbar mit dem Kurpark Bad Wildbad.",
        "Bus X78 (formerly 7780) runs hourly year-round from Bad Wildbad Kurpark to Freudenstadt Stadtbahnhof (~60 min); pair with a stroll in Bad Wildbad spa gardens.",
        ["Pforzheim Hbf", "Bad Wildbad Kurpark", "Enzklösterle", "Besenfeld", "Freudenstadt Stadtbahnhof", "Forbach"],
        "bad_wildbad", "fds_stadt", [], "shortcut_360", "Baumwipfelpfad Schwarzwald", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-wolftal-kniebis-murgtal-f13",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Kinzigtal – Wolftal – Kniebis – Murgtal-Runde (7266 / Freizeitbus F13)",
        "Kinzig Valley – Wolf Valley – Kniebis – Murg Valley Loop (7266 / F13)",
        "Durchs wilde Wolftal vorbei am Bärenpark über den Kniebis (900+ m) nach Freudenstadt & ins Murgtal",
        "Scenic gorge bus through the Wolf Valley past the Bear Park and over 900 m Kniebis Pass",
        "Schattiges Felsen- und Bachtal an der Wolf; weite Höhenblicke auf der Kniebis-Passhöhe.",
        "Sheltered forested Wolf gorge rising to airy summit views atop the Kniebis ridge.",
        "Mo–Fr Bus 7266, Sa/So/Feiertag ganzjährig Freizeitbus F13 im 2h-Takt (Kniebis/Bad Rippoldsau – Schapbach – Wolfach).",
        "Weekdays covered by Bus 7266; weekends & public holidays year-round by Freizeitbus F13 (every 2 hours).",
        ["Hausach", "Wolfach Bahnhof", "Schapbach Bärenpark", "Bad Rippoldsau Kirche", "Kniebis", "Freudenstadt Stadtbahnhof"],
        "wolfach", "fds_stadt", ["rippoldsau"], "shortcut_360", "Kniebis", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-kandel-glottertal-st-maergen-120-112",
        "schwarzwald", "panoramatour", "leicht", True, True,
        "Glottertal – St. Peter – Kandel-Gipfelkamm (1.200 m) Panorama-Busrunde",
        "Glotter Valley – St. Peter – Kandel Summit Ridge (1,200 m) Bus Loop",
        "Seit Sept. 2026 mit den neuen RVF-Bergbussen 120 & 112 aus dem Glottertal direkt hinauf zum Kandel-Rasthaus",
        "Ride RVF Buses 120 & 112 through the Glotter Valley to St. Peter and right up to 1,200 m Kandel ridge",
        "Freier Spätnachmittagsblick vom Kandel auf Rheinebene, Kaiserstuhl und Vogesen.",
        "Open afternoon views from the exposed Kandel summit terrace across the Rhine plain and Vosges.",
        "RE7 bis Denzlingen → Bus 120 nach St. Peter → Bus 112 zum Kandel Rasthaus (1.200 m) → über St. Peter / Bus 110 nach Kirchzarten; im Hochwinter Schneewetter prüfen.",
        "RE7 to Denzlingen → Bus 120 to St. Peter → Bus 112 up to Kandel Rasthaus (1,200 m), returning via Bus 110 through the Dreisam Valley to Kirchzarten.",
        ["Denzlingen", "Glottertal", "St. Peter Zähringer Eck", "Waldkirch Kandel Rasthaus", "St. Märgen", "Kirchzarten Bahnhof"],
        "st_peter", "kirchzarten", ["kandel"], "shortcut_360", "Kandel (Berg)", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-rheinhessen-hiwwel-schweiz-660-440",
        "weitere", "wein", "leicht", True, True,
        "Rheinhessen-Hiwwel & Rheinhessische Schweiz (Bus 660 + 440)",
        "Rheinhessen Wine Hills & Hessian Switzerland Bus Loop (Bus 660 + 440)",
        "Entspannte Überlandbus-Tour über das rheinhessische Rebenmeer von Alzey nach Bad Kreuznach",
        "Relaxed rural bus loop across Rheinhessen vineyard hills and porphyry cliffs to Bad Kreuznach",
        "Weitgehend offene Weinberghügel mit viel Sonne den ganzen Tag über.",
        "Mostly open rolling vineyard landscape enjoying abundant sunshine throughout the day.",
        "Ab Jugenheim empfiehlt sich der Start per Bus 660 direkt von Mainz Hbf quer durch Rheinhessen nach Alzey (~62 Min.), ab dort RNN Bus 440 durch die Rheinhessische Schweiz nach Bad Kreuznach.",
        "From Jugenheim start via Bus 660 directly from Mainz Hbf across the wine hills to Alzey (~62 min), then RNN Bus 440 through the Rheinhessische Schweiz to Bad Kreuznach.",
        ["Alzey Bahnhof", "Rheinhessische Schweiz", "Undenheim", "Wendelsheim", "Siefersheim", "Bad Kreuznach Bahnhof"],
        "alzey", "bad_kreuznach", [], "shortcut_360", "Rheinhessen Weinberge", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-ueberwald-neckartal-681-685",
        "odenwald", "panoramatour", "leicht", True, False,
        "Gorxheimertal – Überwald – Ulfenbachtal – Neckartal (Bus 681 + 685)",
        "Gorxheim Valley – Überwald – Ulfenbach Valley Loop (Bus 681 + 685)",
        "Nahe Odenwald-Busrunde über Wald-Michelbach durchs stille Ulfenbachtal nach Burg Hirschhorn am Neckar",
        "Close-range Odenwald bus crossing from Wald-Michelbach through the quiet Ulfenbach valley to Hirschhorn",
        "Abwechslung aus grünen Wiesentälern im Überwald und sonnigem Neckar-Burgblick in Hirschhorn.",
        "Mix of tranquil Odenwald meadow valleys and sunny castle views above the Neckar at Hirschhorn.",
        "Ab Jugenheim über Weinheim + Bus 681 hinauf nach Wald-Michelbach, per Bus 685 nach Hirschhorn (Neckar) und via S1 über Heidelberg zurück; auf Randkursen Ruftaxi beachten.",
        "Ride via Weinheim & Bus 681 up to Wald-Michelbach, descend on Bus 685 through Ulfenbach Valley to Hirschhorn, and return via Heidelberg.",
        ["Weinheim Hbf", "Gorxheimertal", "Wald-Michelbach ZOB", "Heddesbach", "Hirschhorn (Neckar)", "Heidelberg Hbf"],
        "wald_michelbach", "hirschhorn", [], "shortcut_360", "Burg Hirschhorn", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-grosse-schwarzwald-achterbahn",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Große Schwarzwald-Achterbahn: Murgtalbahn (S8/RE40) & Kinzigtalbahn (RS1)",
        "Grand Black Forest Figure-Eight Rail Loop: Murg Valley & Kinzig Valley",
        "Ohne einen doppelten Schienenmeter über Murg-Viadukte nach Freudenstadt & durch Fachwerkstädte bis Offenburg",
        "Zero-overlap scenic rail loop riding Murg Valley stone viaducts to Freudenstadt and half-timbered Kinzig towns",
        "Morgens schattig-tiefe Steiltäler bei Forbach, nachmittags weite Wiesen & Rebberge zwischen Haslach und Offenburg.",
        "Dramatic steep gorges above Forbach transitioning into broad sunlit Kinzig meadows toward Offenburg.",
        "Kinzigtalbahn-Sperrung ist beendet (stündlicher Takt Freudenstadt – Schiltach – Hausach – Offenburg); herrliche Zwischenstopps in Schiltach oder Gengenbach.",
        "Hourly connections all the way from Freudenstadt past Alpirsbach, Schiltach, Haslach and Gengenbach to Offenburg.",
        ["Forbach (Schwarzw)", "Freudenstadt Hbf", "Alpirsbach", "Schiltach Bahnhof", "Haslach", "Gengenbach", "Offenburg"],
        "fds_hbf", "offenburg", ["schiltach"], "shortcut_360", "Schiltach", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-nordpfalz-lautertal-glan-alsenz",
        "pfaelzerwald", "panoramatour", "leicht", True, False,
        "Nordpfalz Vier-Täler-Loop: Lautertal – Meisenheim (Bus 260) – Nahe – Alsenz",
        "North Palatinate Four-Valley Loop: Lauter, Glan (Bus 260), Nahe & Alsenz",
        "Geschlossener ÖPNV-Kreis durch vier Flusstäler inkl. Fachwerkjuwel Meisenheim am Glan",
        "Seamless four-valley rail & bus loop linking Lauter, Glan, Nahe and Alsenz rivers with medieval Meisenheim",
        "Offene Wiesen- und Weinbergslandschaft entlang von Glan und Nahe mit steiler Rotenfels-Kulisse.",
        "Open river meadows and vineyards along Glan and Nahe rivers framed by Rotenfels cliffs.",
        "Ab Kaiserslautern mit RB66 nach Lauterecken, Anschlussbus 260 (wartet bei Verspätung planmäßig 3–5 Min.) über Meisenheim nach Bad Sobernheim, RE3 nach Bad Kreuznach & RB65 durchs Alsenztal.",
        "From Kaiserslautern take RB66 to Lauterecken, timed Bus 260 through medieval Meisenheim to Bad Sobernheim, RE3 to Bad Kreuznach, and RB65 via the Alsenz Valley.",
        ["Kaiserslautern Hbf", "Lauterecken-Grumbach", "Meisenheim", "Bad Sobernheim", "Bad Kreuznach", "Rockenhausen"],
        "lauterecken", "bad_kreuznach", ["bad_sobernheim"], "shortcut_360", "Schlosskirche (Meisenheim)", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-mittelrhein-hunsrueckbahn-rheinhessen",
        "weitere", "panoramatour", "leicht", True, True,
        "Steilstrecke Hunsrückbahn (RB37) & Rheinhessenbahn (RB35)-Loop",
        "Steep Hunsrückbahn Mountain Rail (RB37) & Rheinhessen (RB35) Loop",
        "UNESCO-Mittelrheintal bis Boppard, 6-%-Steilstrecke nach Emmelshausen & komplett per Bahn durch Rheinhessen",
        "UNESCO Rhine Gorge to Boppard, Germany's steepest adhesion railway (6%) to Emmelshausen & Rheinhessen return",
        "Sonnige Rheinterrassen links des Rheins und weite Hochflächen über dem Hubertusviadukt.",
        "Sunny Left Rhine terraces and airy plateau vistas atop the Hubertus viaduct.",
        "Hinweis Nov. 2026: Rechte Rheinstrecke (RB10) bis 12.12.2026 im SEV – daher verläuft der Rückweg ab Boppard über Bingen und die Rheinhessenbahn RB35 (Alzey – Worms) komplett auf der Schiene!",
        "Avoids Right Rhine rail works (through Dec 12, 2026) by looping back via Left Rhine RE2/RB26 to Bingen and Rheinhessenbahn RB35 to Alzey & Worms.",
        ["Boppard Hbf", "Hubertusviadukt", "Emmelshausen Bahnhof", "Bingen (Rhein) Hbf", "Alzey Bahnhof", "Worms Hbf"],
        "boppard", "alzey", ["emmelshausen", "bingen_hbf"], "shortcut_360", "Hunsrückbahn", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-odenwaldbahn-neckartal-bergstrasse",
        "odenwald", "panoramatour", "leicht", True, False,
        "Odenwaldbahn (Himbächel-Viadukt) – Neckartal (S1) – Bergstraße-Dreieck",
        "Odenwald Railway Viaduct – Neckar Valley (S1) – Bergstraße Triangle",
        "Vom Mümlingtal (Michelstadt/Erbach) über das 43 m hohe Himbächel-Viadukt ins Neckar-Burgentor bei Eberbach",
        "Ride the Odenwald Railway from Michelstadt across the 43 m Himbächel Viaduct into the Neckar Valley at Eberbach",
        "Lichte Waldhochflächen im Mümlingtal und romantisches Neckartal zwischen Eberbach, Hirschhorn und Heidelberg.",
        "Airy valley pastures in central Odenwald transitioning into castle-lined Neckar gorge scenery.",
        "Ab Jugenheim über Darmstadt per VIAS Odenwaldbahn nach Michelstadt, weiter über Erbach & Himbächel-Viadukt nach Eberbach (WE 2h-Takt zwischen Erbach & Eberbach) und S1 über Heidelberg zurück.",
        "VIAS RE80/RB82 reaches Michelstadt via Darmstadt; continue over Erbach and Himbächel Viaduct to Eberbach (2h cadence on weekends) and return via Neckar S1.",
        ["Darmstadt Hbf", "Michelstadt Bahnhof", "Erbach (Odw)", "Himbächel-Viadukt", "Eberbach", "Hirschhorn (Neckar)", "Heidelberg Hbf"],
        "michelstadt", "eberbach", [], "shortcut_360", "Himbächel-Viadukt", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-nagoldtal-hermann-hesse-bahn",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Nagoldtalbahn (RB74) & neue Hermann-Hesse-Bahn (RB76) über Calw",
        "Nagold Valley Rail (RB74) & New Hermann-Hesse-Bahn (RB76) Loop via Calw",
        "Bahn-Neuheit 2026: Durchs Nagoldtal nach Calw und auf der reaktivierten Hermann-Hesse-Bahn nach Renningen",
        "2026 railway reopening: ride through the Nagold Valley to Calw & the newly reopened Hermann-Hesse-Bahn to Renningen",
        "Tief eingeschnittenes Waldtal entlang der Nagold und offene Heckengäu-Landschaft bei Weil der Stadt.",
        "Wooded meanders along the Nagold river giving way to sunlit Heckengäu meadows around Weil der Stadt.",
        "Leise Akku-Triebzüge auf der historischen Württembergischen Schwarzwaldbahn RB76 Calw – Renningen (Mo–Fr 30-Min.-, Sa/So 60-Min.-Takt); Rückweg ab Renningen über Mühlacker/Bretten oder Stuttgart.",
        "Quiet battery trains operate the reopened RB76 Calw–Renningen tunnel line (30 min weekday / 60 min weekend cadence); return via Mühlacker/Pforzheim.",
        ["Pforzheim Hbf", "Kloster Hirsau", "Calw Bahnhof/ZOB", "Forst- & Hirsauer Tunnel", "Renningen", "Mühlacker"],
        "calw", "renningen", [], "shortcut_360", "Calw", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-suedpfalz-weintor-wissembourg-lauterbourg",
        "pfaelzerwald", "wein", "leicht", True, True,
        "Südpfalz, Deutsches Weintor & Elsass-Fachwerk: Wissembourg & Lauterbourg",
        "South Palatinate Wine Gate & Alsace Half-Timbered Towns: Wissembourg & Lauterbourg",
        "100 % D-Ticket über Bad Bergzabern am Weintor vorbei nach Wissembourg & über Winden ins Festungsstädtchen Lauterbourg",
        "100% Deutschlandticket cross-border loop via German Wine Gate, Wissembourg and Lauterbourg",
        "Offene Weinberge an der Südlichen Weinstraße sowie gemütliche Fachwerkgassen an der Lauter.",
        "Sunny Southern Wine Route vineyards and charming riverside streets in Alsace.",
        "100 % D-Ticket bis Wissembourg (Bus 543 / RB53 DB Regio) & Lauterbourg (RB52 DB Regio). Wichtig: In Wissembourg & Lauterbourg keine französischen TER/Fluo-Busse tiefer ins Elsass besteigen!",
        "Fully Deutschlandticket-valid to Wissembourg (Bus 543 / DB Regio RB53) and Lauterbourg (DB Regio RB52); do not board domestic French TER/Fluo buses deeper into Alsace.",
        ["Landau (Pfalz) Hbf", "Wissembourg Gare", "Schweigen Deutsches Weintor", "Bad Bergzabern Bahnhof", "Wörth (Rhein)", "Lauterbourg"],
        "landau_hbf", "lauterbourg", ["wissembourg", "bad_bergzabern"], "shortcut_360", "Deutsches Weintor", _transit_hike_empty(),
    ),
    _make_tour(
        "stadt-speyer-schwetzingen-kurpfalz",
        "weitere", "stadt", "leicht", True, True,
        "UNESCO-Kaiserdom Speyer & Schloss Schwetzingen (S3 + Bus 717 + S9)",
        "UNESCO Imperial Cathedral Speyer & Schwetzingen Palace Loop",
        "Kultur-Rundkurs ganz ohne Wanderpflicht: Vom romanischen Kaiserdom über den Rhein direkt vors Schwetzinger Schloss",
        "Zero-hike heritage loop connecting Speyer's UNESCO cathedral across the Rhine to Schwetzingen Palace",
        "Weitläufiger Domplatz und sonniger Schwetzinger Schlossgarten mit Orangerie und Moscheegarten.",
        "Open cathedral square in Speyer and sunlit baroque palace gardens in Schwetzingen.",
        "Ab Speyer Hbf/Domplatz fährt Bus 717 über Ketsch bis direkt zum Schwetzinger Schlossplatz (~35 Min.); ab dort stündlich/halbstündlich S9 nach Karlsruhe bzw. Mannheim/Jugenheim.",
        "VRN Bus 717 links Speyer Hbf/Domplatz with Schwetzingen Schlossplatz in ~35 min; frequent S9 rail connects onward to Karlsruhe and Jugenheim.",
        ["Speyer Hbf", "Speyer Kaiserdom & SchUM-Viertel", "Ketsch", "Schwetzingen Schlossplatz", "Mannheim Hbf"],
        "speyer", "schwetzingen_schloss", [], "shortcut_360", "Speyerer Dom", _transit_hike_empty(),
    ),
    _make_tour(
        "stadt-maulbronn-bretten-kraichgau",
        "weitere", "stadt", "leicht", True, False,
        "UNESCO-Kloster Maulbronn & Melanchthonstadt Bretten (S4 + Bus 700 + IRE1)",
        "UNESCO Maulbronn Abbey & Bretten Half-Timbered Town Loop",
        "Per Bus 700 direkt bis vors mittelalterliche Klostertor und im Bogen über Mühlacker durch den Kraichgau",
        "Ride S4 to Bretten, Bus 700 directly to medieval Maulbronn Monastery gate, and return via Mühlacker",
        "Geschützter Klosterhof, weite Kraichgauer Hügel und historischer Bretter Marktplatz; wetterfest bei Herbstwind.",
        "Sheltered medieval cloisters paired with rolling Kraichgau countryside and Bretten's old square.",
        "Wichtig: Kloster Maulbronn hat Nov–Feb montags Ruhetag (Di–So 9:30–17 Uhr offen); Bus 700 fährt auch im Winter täglich bis direkt vor die Klostermauer.",
        "Note: Maulbronn Abbey is closed Mondays in Nov–Feb (open Tue–Sun 9:30–17:00); VPE Bus 700 stops right outside the monastery walls daily.",
        ["Bretten", "Maulbronn Kloster", "Mühlacker ZOB", "Pforzheim Hbf", "Bruchsal"],
        "bretten", "muehlacker", ["maulbronn_kloster"], "shortcut_360", "Kloster Maulbronn", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-taunus-x72-idstein-lahn",
        "weitere", "panoramatour", "leicht", True, False,
        "Wiesbaden – Taunus-Expressbus (X72) – Limburg (Lahn) – Idstein",
        "Wiesbaden – Taunus Express Bus (X72) – Limburg Cathedral – Idstein Loop",
        "Quer über den Taunus-Kamm im Schnellbus X72 zum sieben-türmigen Limburger Dom & zurück durchs Fachwerk-Idstein",
        "Cross the Taunus crest on Express Bus X72 to 7-spired Limburg Cathedral and return through historic Idstein",
        "Offene Taunus-Höhenrücken zwischen Taunusstein & Kirberg sowie sonniger Domfelsen über der Lahn.",
        "Open Taunus ridges on Express Bus X72, cathedral cliff overlooking the Lahn river, and Idstein old town.",
        "RMV-Expressbus X72 verkehrt ganzjährig täglich (auch Sa/So stündlich) ohne Zuschlag zwischen Wiesbaden Hbf & Limburg (71 Min.); Rückweg über die Main-Lahn-Bahn RE20/RB22 nach Idstein & Frankfurt.",
        "RMV Express Bus X72 runs hourly daily year-round (71 min Wiesbaden–Limburg) at no extra cost; return on Main-Lahn regional trains via Idstein.",
        ["Wiesbaden Hbf", "Limburger Dom", "Taunusstein", "Idstein Bahnhof", "Frankfurt (M) Hbf"],
        "wiesbaden", "idstein", ["limburg"], "shortcut_360", "Limburger Dom", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-hoellental-dreiseen-schwarzwaldbahn",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Höllentalbahn (S10), Dreiseenbahn (Seebrugg) & Schwarzwaldbahn-Kehrtunnel",
        "Höllental Gorge Rail, Three-Lakes Line (Seebrugg) & Black Forest Railway Loop",
        "Die steilsten Gebirgsbahnen Südwestdeutschlands: 5,5-%-Rampe, 967 m höchster DB-Bahnhof & 36 Triberger Tunnel",
        "Three iconic mountain railways in one day: Höllental gorge, Germany's highest standard-gauge station & 36 tunnels",
        "Wilde Felsschlucht am Ravenna-Viadukt, weiter Seespiegel am Schluchsee & Panorama-Kehrschleifen im Gutachtal.",
        "Dramatic ravine at Ravenna Viaduct, snowy mountain lakes around Schluchsee, and sweeping loops above Triberg.",
        "Alle Herbstbauarbeiten auf Höllental- und Schwarzwaldbahn sind beendet; Abstecher per S1 ab Titisee bis Seebrugg am Schluchsee dauert nur 30 Min.",
        "All autumn rail works are complete; the S1 branch from Titisee climbs past Feldberg-Bärental (967 m) to Lake Schluchsee (Seebrugg).",
        ["Höllental (Schwarzwald)", "Ravennaschlucht", "Titisee", "Feldberg-Bärental (967 m)", "Seebrugg (Schluchsee)", "Donaueschingen", "Triberg"],
        "titisee", "donaueschingen", ["seebrugg"], "shortcut_360", "Ravennaschlucht", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-westfranken-main-madonnenland-neckar",
        "odenwald", "panoramatour", "leicht", True, False,
        "Maintalbahn (RE87), Madonnenlandbahn (RB84) & Neckartal-Dreieck",
        "Main Valley, Madonnenland Rural Rail (RB84) & Neckar Castle Triangle",
        "Diesel-Nebenbahnromantik von Aschaffenburg & Miltenberg über Amorbach nach Seckach und per S1 durchs Neckartal",
        "Country branch-line loop from Miltenberg past Amorbach and Walldürn to Seckach, returning down the Neckar",
        "Weite Odenwald-Hochfläche rund um Walldürn und steile Waldhänge über Main und Neckar.",
        "Rolling Odenwald plateau scenery around Amorbach and Walldürn paired with Main and Neckar river valleys.",
        "Ab Jugenheim ideal per RB75 nach Aschaffenburg & RE87 nach Miltenberg (~2h). Ab Miltenberg fährt die RB84 ganzjährig direkt bis Seckach (~91 Min., Sa/So 2h-Takt beachten!).",
        "Reaches Miltenberg cleanly via Darmstadt & Aschaffenburg; branch line RB84 runs across the eastern Odenwald to Seckach (2h cadence on weekends) connecting straight to Neckar S1.",
        ["Aschaffenburg Hbf", "Miltenberg", "Kloster Amorbach", "Walldürn", "Seckach Bahnhof", "Eberbach", "Heidelberg Hbf"],
        "miltenberg", "seckach", [], "shortcut_360", "Kloster Amorbach", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-elztal-simonswald-furtwangen-triberg",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Elztal – Simonswäldertal (Bus 240) – Furtwangen – Triberg (Bus 550)",
        "Elz Valley – Simonswald (Bus 240) – Furtwangen Clock Museum – Triberg",
        "West-Ost-Querung über die zentralen Schwarzwaldpässe kombiniert mit dem Deutschen Uhrenmuseum & RE2-Kehrtunneln",
        "West-to-east Black Forest ridge bus crossing via Furtwangen Clock Museum down to Triberg & Schwarzwaldbahn",
        "Tief eingeschnittenes Simonswäldertal bei Bleibach, sonnige Höhenweiden um Schönwald/Furtwangen.",
        "Deep scenic Simonswald valley followed by open mountain pastures around Furtwangen and Schönwald.",
        "Mo–Sa im Stundentakt über move Bus 240 & 550; Achtung sonntags: Bus 550 Furtwangen→Triberg verkehrt erst nachmittags (~ab 13 Uhr) teils als Rufbus – früh planen!",
        "Hourly Mon–Sat via Buses 240 & 550; on Sundays Bus 550 runs afternoon departures only (~13:00+).",
        ["Bleibach Bahnhof", "Simonswäldertal", "Furtwangen Rößleplatz", "Deutsches Uhrenmuseum", "Schönwald", "Triberg Bahnhof", "Offenburg"],
        "bleibach", "triberg", ["furtwangen"], "shortcut_360", "Simonswäldertal", _transit_hike_empty(),
    ),
    _make_tour(
        "busrunde-nationalpark-erbeskopf-800-890",
        "weitere", "panoramatour", "leicht", True, True,
        "Nationalpark Hunsrück-Hochwald & Erbeskopf (816 m): Bus 800 + 890",
        "Hunsrück-Hochwald National Park & Erbeskopf (816 m): Buses 800 + 890",
        "Doppelstockbus 800 bis zum höchsten Gipfel von Rheinland-Pfalz (816 m) & per Nationalparkbus 890 nach Neubrücke",
        "Double-decker Bus 800 up to Rhineland-Palatinate's highest summit (816 m) and National Park Bus 890 down to Neubrücke",
        "Exponierte Gipfelwiese am Erbeskopf mit Skulptur ‚Windklang‘ und weitem Hunsrück-Fernblick.",
        "Open 816 m summit plateau at Erbeskopf with iconic Windklang sculpture and wide Hunsrück views.",
        "Ideal ab Jugenheim via Mainz + Nahe-Express RE3 nach Idar-Oberstein (~3h). Hochwald-Schnellbus 800 & Nationalparkbus 890 fahren ganzjährig täglich im 1–2h-Takt.",
        "Best reached from Jugenheim via RE3 along the Nahe valley to Idar-Oberstein; express Bus 800 (double-decker) and Bus 890 run year-round.",
        ["Idar-Oberstein Bahnhof", "Hilscheid Hunsrückhaus/Erbeskopf (816 m)", "Birkenfeld", "Neubrücke (Nahe)", "Mainz Hbf"],
        "idar_oberstein", "neubruecke", ["erbeskopf"], "shortcut_360", "Erbeskopf", _transit_hike_empty(),
    ),
    _make_tour(
        "stadt-staufen-breisach-kaiserstuhl",
        "schwarzwald", "stadt", "leicht", True, True,
        "Fauststadt Staufen (Burgruine) & Münsterstadt Breisach am Kaiserstuhl",
        "Historic Staufen Castle Town & Breisach Minster atop the Rhine Loop",
        "Farbige Altstadt & Burgruine Staufen (+15 Min. Spaziergang) + Breisacher Münsterberg hoch über Rhein & Kaiserstuhl",
        "Colorfully painted Faust town of Staufen plus Breisach's Romanesque cathedral terrace high above the Rhine",
        "Sonnenverwöhntes Markgräflerland & Kaiserstuhl mit goldenen Spätnachmittagsblicken zu den Vogesen.",
        "Sun-warmed Markgräflerland and Kaiserstuhl terraces glowing above the Upper Rhine toward Alsace.",
        "RE7 + Münstertalbahn S3 nach Staufen; weiter mit Breisacher Bahn S1 oder Kaiserstuhlbahn S5 um den Vulkan-Rücken; beide Stadtberge jederzeit kostenlos begehbar.",
        "RE7 + S3 serve Staufen directly; continue via Freiburg on S1 to Breisach (and return around Kaiserstuhl volcano if desired).",
        ["Bad Krozingen", "Staufen Bahnhof & Burgruine", "Freiburg Hbf", "Breisach Münsterberg", "Riegel-Malterdingen"],
        "staufen", "breisach", [], "shortcut_360", "Breisacher Stephansmünster", _transit_hike_empty(),
    ),
    _make_tour(
        "bahnrunde-oberes-donautal-schwarzwaldbahn",
        "schwarzwald", "panoramatour", "leicht", True, False,
        "Schwäbischer Grand Canyon: Oberes Donautal (RE55) & Schwarzwaldbahn",
        "Upper Danube Gorge (RE55) & Black Forest Railway (RE2) Grand Loop",
        "Schwarzwaldbahn-Tunnel bis Villingen, weiter an 200 m hohen Kalkfelsen vorbei über Beuron & Sigmaringen bis Ulm",
        "Black Forest Railway into Villingen, then cliff-walled Upper Danube valley past Beuron Abbey & Sigmaringen Castle to Ulm",
        "Dramatische, weiß leuchtende Jurakalkfelsen im Durchbruchstal der jungen Donau und prächtiges Hohenzollernschloss Sigmaringen.",
        "Towering white limestone crags in the Upper Danube breakthrough valley and Sigmaringen palace cliff.",
        "Ab Karlsruhe RE2 bis Villingen (~1h56) und direkt auf dem Donautal-Express RE55 nach Sigmaringen/Ulm (Rückweg per RE5/RE1 über Stuttgart; bis 12.12.26 schneller als Zollernbahn).",
        "Out via RE2 Schwarzwaldbahn to Villingen, through the Danube gorge on RE55 to Sigmaringen & Ulm, returning swiftly via RE5/RE1.",
        ["Triberg", "Villingen (Schwarzw)", "Tuttlingen", "Kloster Beuron", "Sigmaringen", "Ulm Hbf"],
        "villingen", "ulm", ["sigmaringen"], "shortcut_360", "Kloster Beuron", _transit_hike_empty(),
    ),
    _make_tour(
        "stadt-seligenstadt-aschaffenburg-main",
        "weitere", "stadt", "leicht", True, False,
        "Einhardstadt Seligenstadt (Kloster) & Schlossstadt Aschaffenburg (RB75 + RB86)",
        "Seligenstadt Abbey Old Town & Aschaffenburg Johannisburg Palace Loop",
        "Rotes Mainschloss Johannisburg & historische Einhard-Basilika mit Fachwerkmarkt in kurzer Distanz ab Jugenheim",
        "Red-sandstone Renaissance Johannisburg Palace on the Main paired with Seligenstadt's Carolingian abbey town",
        "Uferpromenaden am Main und sonniger barocker Klostergarten in Seligenstadt; perfekte Halbtages-/Wintertour.",
        "Main riverbank promenades and sheltered Baroque abbey garden in Seligenstadt; great any season.",
        "Ab Jugenheim in ~80–95 Min. via Darmstadt nach Aschaffenburg; Schloss-Innenräume Nov–März Di–So 10–16 Uhr (Pompejanum Winterpause; Außenanlagen jederzeit frei).",
        "Quick ~85 min ride from Jugenheim to Aschaffenburg via Darmstadt; hop RB75+RB86 to Seligenstadt and return via Hanau & Frankfurt.",
        ["Darmstadt Hbf", "Aschaffenburg Hbf & Schloss Johannisburg", "Babenhausen (Hess)", "Seligenstadt Kloster & Altstadt", "Hanau Hbf"],
        "aschaffenburg", "seligenstadt", [], "shortcut_360", "Schloss Johannisburg", _transit_hike_empty(),
    ),
    _make_tour(
        "stadt-esslingen-ludwigsburg-barock",
        "weitere", "stadt", "leicht", True, True,
        "Mittelalterstadt Esslingen („Klein-Venedig“) & Barockresidenz Ludwigsburg",
        "Medieval Esslingen Half-Timbered Town & Baroque Ludwigsburg Palace Loop",
        "Verwinkelte Kanäle & Weinbergstaffeln hinauf zur Esslinger Burg kombiniert mit dem Residenzschloss Ludwigsburg",
        "Canals & hilltop castle ramparts in medieval Esslingen combined with the grand Baroque palace of Ludwigsburg",
        "Süd-West-Weinbergstaffeln unterhalb der Esslinger Burg mit weitem Neckartal-Abendblick.",
        "South-facing vineyard steps below Esslingen Castle and vast palace courtyards in Ludwigsburg.",
        "Ab Karlsruhe stündlich/halbstündlich per IRE1/RE1 + S1/S4/S5 erreichbar; im Advent (ab 24.11.) bieten sowohl Esslingens Mittelaltermarkt als auch Ludwigsburg besondere Höhepunkte.",
        "Easy hourly RE1/IRE1 + S-Bahn connections from Karlsruhe; both towns also host celebrated historic Christmas markets from late November.",
        ["Stuttgart Hbf", "Esslingen (Neckar) Klein-Venedig", "Esslinger Burg", "Ludwigsburg Residenzschloss", "Bietigheim-Bissingen"],
        "esslingen", "ludwigsburg", [], "shortcut_360", "Esslinger Burg", _transit_hike_empty(),
    ),

    # --- 25..30: Aussichtspunkte / Panorama-Spaziergänge (curated, walkMin > 0) ---
    _make_tour(
        "aussicht-rotenfels-bastei-nahe",
        "weitere", "berg", "mittel", False, True,
        "Rotenfels-Bastei: Höchste Steilwand nördlich der Alpen & Salinental",
        "Rotenfels Cliff Bastion & Bad Kreuznach Salinental Viewpoint",
        "Kurze, steile Aussichts-Wanderung ab Bad Münster (35–45 Min.) auf die 202 m hohe Porphyr-Felswand über der Nahe",
        "Short steep ascent (35–45 min) to Europe's highest porphyry cliff face north of the Alps above the Nahe River",
        "Südost-exponierte Felskanzel auf über 200 m über dem Fluss mit eindrucksvollem Fernblick nach Rheinhessen.",
        "Sun-warmed south-east porphyry crag dropping 202 m sheer toward Ebernburg Castle and the Nahe valley.",
        "Handfähre Rheingrafenstein ruht im Winter → daher Aufstieg direkt ab Bad Münster Bahnhof zur Bastei (~35–45 Min., +230 Hm) und gemütlicher Tal-Rückweg durchs Salinental nach Bad Kreuznach.",
        "Seasonal ferry pauses in winter, so hike directly from Bad Münster station to the Rotenfels cliff rim and descend via the Salinental saltworks to Bad Kreuznach.",
        ["Bad Münster am Stein", "Rotenfels-Bastei (327 m)", "Salinental Gradierwerke", "Bad Kreuznach Bahnhof"],
        "bad_muenster", "bad_kreuznach", [], "curated", "Rotenfels (Nahe)", _viewpoint_hike(6.2, 230, 240, 105), 150, 100,
    ),
    _make_tour(
        "aussicht-freiburg-schlossbergturm",
        "schwarzwald", "berg", "mittel", True, True,
        "Freiburg Schlossberg: Kanonenplatz & 35-m-Schlossbergturm",
        "Freiburg Schlossberg Cannon Terrace & 35 m Observation Tower",
        "Direkt per RE7 & Tram 1 ohne teure Seilbahn in 30 Min. über den Kanonenplatz auf den 35 m hohen Aussichtsturm",
        "Reach Freiburg's 35 m panoramic steel tower via RE7 and Tram 1 without needing the paid cable car",
        "Traumhafter West- und Sonnenuntergangsblick über Münsterspitze, Kaiserstuhl und die Vogesen.",
        "Unbeatable sunset panorama over Freiburg Minster spire, the Kaiserstuhl hills, and Alsace across the Rhine.",
        "24/7 kostenlos zugänglich! Per VAG Tram 1 (im D-Ticket) bis Oberlinden am Schwabentor, ~30–35 Min. Stadtberg-Anstieg (+185 Hm) zum Turm und über Stadtgarten zurück.",
        "Free 24/7 open-air access! Take VAG Tram 1 to Oberlinden near Schwabentor, climb ~30 min (+185 m) past Kanonenplatz to the tower, and loop back to the old town.",
        ["Freiburg Hbf", "Oberlinden / Schwabentor", "Kanonenplatz", "Schlossbergturm (35 m)", "Freiburg Münster"],
        "freiburg_oberlinden", "freiburg_oberlinden", [], "curated", "Schlossbergturm (Freiburg im Breisgau)", _viewpoint_hike(3.8, 185, 185, 75), 50, 50,
    ),
    _make_tour(
        "aussicht-burg-steinsberg-kraichgau",
        "weitere", "burg", "leicht", False, True,
        "Burg Steinsberg („Kompass des Kraichgaus“) mit 30-m-Bergfried",
        "Steinsberg Castle ('Compass of the Kraichgau') & 30 m Donjon Viewpoint",
        "Nur 15 Min. Weinbergs-Spaziergang ab Bushalt Weiler auf den Vulkan-Kegel (333 m) mit 360°-Weitblick",
        "Quick 15-min vineyard walk from Weiler bus stop up a volcanic cone (333 m) to a panoramic 360° octagonal keep",
        "Freistehender Gipfel über allen Nachbartälern – ideal für goldene Nachmittags- und Abendsonne.",
        "Free-standing volcanic peak bathed in afternoon sun with 360° views from Black Forest to Odenwald.",
        "Burg & achteckiger Bergfried ganzjährig kostenlos offen! Sa/So verkehrt Bus 763 ab Sinsheim Hbf teils als VRN-Ruftaxi (bitte 30–60 Min. vorab bestellen, im D-Ticket enthalten).",
        "Castle grounds and octagonal keep are free & open daily year-round; on weekends check whether Bus 763 from Sinsheim runs as on-demand VRN Ruftaxi.",
        ["Sinsheim (Elsenz) Hbf", "Weiler Ortsmitte", "Burg Steinsberg (333 m)", "30-m-Bergfried"],
        "weiler_sinsheim", "weiler_sinsheim", [], "curated", "Burg Steinsberg", _viewpoint_hike(2.0, 85, 85, 45), 100, 100,
    ),
    _make_tour(
        "aussicht-burg-hornberg-neckarzimmern",
        "odenwald", "burg", "leicht", False, True,
        "Burg Hornberg & 33-m-Bergfried über dem Neckartal (S1/S2)",
        "Hornberg Castle & 33 m Keep above Steep Neckar Vineyards",
        "Besterhaltene Neckar-Ritterburg nur 15–20 Min. oberhalb des Bahnhofs Neckarzimmern inmitten steiler Weinberge",
        "Largest and best-preserved castle in the Neckar valley just 15–20 min walk above Neckarzimmern S-Bahn station",
        "Sonnige Süd- und West-Terrassenlagen über der Neckarschleife am späten Nachmittag.",
        "Sunlit south- and west-facing castle terraces overlooking steep vineyard bends of the Neckar river.",
        "Direkt per S1/S2 ab Heidelberg/Eberbach bzw. S41/RE10a ab Heilbronn erreichbar. Im November Mi–So 11–16 Uhr offen (Mo/Di Ruhetag; 10 % D-Ticket/bwegtPlus-Rabatt vor Ort).",
        "Served directly by S1/S2 from Heidelberg or RE10a/S41 from Heilbronn. In November castle grounds open Wed–Sun 11:00–16:00 (10% discount with bwegtPlus/D-Ticket).",
        ["Neckarzimmern Bahnhof", "Weinberg-Steig", "Burg Hornberg", "33-m-Bergfried"],
        "neckarzimmern", "neckarzimmern", [], "curated", "Burg Hornberg (Neckarzimmern)", _viewpoint_hike(2.6, 90, 90, 55), 250, 250,
    ),
    _make_tour(
        "aussicht-bingen-klopp-rochusberg",
        "weitere", "burg", "leicht", True, True,
        "Bingen: Burg Klopp & Rochusberg-Panorama über Mäuseturm & Rheingau",
        "Bingen: Klopp Castle & Rochusberg Overlook above Mäuseturm & Rheingau",
        "Kurzer Stadtspaziergang hinauf zur Burg Klopp und über den sonnigen Rochusberg-Kamm (per Stadtbus 607 abkürzbar)",
        "Easy town panorama climb to Klopp Castle and along sunny Rochusberg ridge above the Rhine–Nahe confluence",
        "Sonniger Süd- und Westblick über das Binger Loch, Mäuseturm und das gegenüberliegende Niederwalddenkmal.",
        "Panoramic sweep across the Nahe mouth, Bingen Mouse Tower island, and Rheingau vineyard slopes.",
        "Stündliche RB26/RE2-Verbindungen bis Bingen (Rhein) Stadt; Außenhof Burg Klopp & Rochuskapellen-Plateau ganzjährig 24/7 frei begehbar (Stadtbus 606/607 im D-Ticket).",
        "Direct hourly trains to Bingen Stadt; Klopp Castle courtyard and Rochusberg ridge stay freely open year-round, with City Bus 606/607 saving the uphill climb if desired.",
        ["Bingen (Rhein) Stadt", "Burg Klopp", "Rochuskapelle", "Rochusberg Weinberge"],
        "bingen_stadt", "bingen_stadt", [], "curated", "Burg Klopp", _viewpoint_hike(4.6, 140, 140, 85), 150, 150,
    ),
    _make_tour(
        "aussicht-ehrenbreitstein-koblenz",
        "weitere", "burg", "leicht", False, True,
        "Festung Ehrenbreitstein & „Rhein-Mosel-Blick“ (Bus + Festungsaufzug)",
        "Ehrenbreitstein Fortress & Rhine-Moselle Overlook (without Cable Car)",
        "118 m hoch über dem Deutschen Eck ohne teure Seilbahn: koveb-Bus nach Ehrenbreitstein + Schrägaufzug im D-Ticket",
        "Towering 118 m above the Rhine–Moselle confluence using local bus + inclined elevator covered by Deutschlandticket",
        "West- und Südwest-Plateau mit ikonischem Blick auf Koblenz, Moselmündung und das Deutsche Eck.",
        "Iconic late-afternoon view straight across the confluence of the Moselle and Rhine rivers.",
        "Hinweis 2026: Anfahrt ab Mainz per Linker Rheinstrecke (RE2/RB26; Rechtsrhein bis 12.12.26 im SEV). Ab Koblenz Hbf Bus 9/10/19/460 nach Ehrenbreitstein – der Schrägaufzug ist im D-Ticket inklusive (Seilbahn nicht!).",
        "Travel via Left Rhine RE2/RB26 to Koblenz Hbf, then bus 9/10/19/460 to Ehrenbreitstein; the inclined Festungsaufzug is included in Deutschlandticket (cable car is paid extra).",
        ["Koblenz Hbf", "Koblenz Ehrenbreitstein", "Schrägaufzug Festungsaufzug", "Plattform Rhein-Mosel-Blick"],
        "koblenz_festung", "koblenz_festung", [], "curated", "Festung Ehrenbreitstein", _viewpoint_hike(2.2, 40, 40, 60), 200, 200,
    ),
]


def _iter_itins_in_plan(plan: dict[str, Any]):
    for key in ("fwd", "rev"):
        if plan.get(key):
            yield key, plan[key]
    for key in ("alts_fwd", "alts_rev"):
        for i, a in enumerate(plan.get(key) or []):
            segs = a.get("segs") if isinstance(a, dict) else a
            if segs:
                yield f"{key}[{i}]", segs
    lsr = plan.get("lastSafeReturn")
    if isinstance(lsr, dict) and lsr.get("segs"):
        yield "lastSafeReturn", lsr["segs"]


def update_catalog() -> dict[str, Any]:
    assert FULL_CATALOG_PATH.exists(), f"Missing {FULL_CATALOG_PATH}"
    cat = json.loads(FULL_CATALOG_PATH.read_text(encoding="utf-8"))
    new_ids = {t["id"] for t in NEW_TOURS}
    assert len(new_ids) == 30, f"Expected 30 unique new tour IDs, got {len(new_ids)}"

    kept = [t for t in cat["tours"] if t["id"] not in new_ids]
    assert len(kept) == 151, f"Expected 151 baseline tours in catalog, got {len(kept)}"
    kept_ids = {t["id"] for t in kept}
    assert not (kept_ids & new_ids), f"Duplicate tour IDs: {kept_ids & new_ids}"

    cat["tours"] = kept + NEW_TOURS
    cat["generated"] = dt.datetime.now(BERLIN_TZ).isoformat(timespec="seconds")
    FULL_CATALOG_PATH.write_text(
        json.dumps(cat, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[1/4] Catalog updated: {len(cat['tours'])} tours written to {FULL_CATALOG_PATH}", flush=True)
    return cat


def update_media(cat: dict[str, Any]) -> None:
    credits = json.loads(CREDITS_PATH.read_text(encoding="utf-8")) if CREDITS_PATH.exists() else {}
    pending = [t for t in NEW_TOURS if not is_tour_media_valid(t["id"], credits)]
    if pending:
        print(f"[2/4] Fetching Wikimedia media for {len(pending)} new tours...", flush=True)
        run_media_pipeline(cat, force=False)
    else:
        print("[2/4] All 30 new tours already have valid 800x450 JPEGs & credits.", flush=True)

    credits = json.loads(CREDITS_PATH.read_text(encoding="utf-8"))
    for t in cat["tours"]:
        assert is_tour_media_valid(t["id"], credits), f"Invalid media/credits for {t['id']}"


def _mid_finish_min(fwd_arr_hhmm: str | None, hops: list[list[list[dict[str, Any]]]]) -> int | None:
    """Return earliest completion minute (midnight=0) of middle hops after `fwd_arr_hhmm`, matching JS getMiddle()."""
    t0 = parse_hhmm(fwd_arr_hhmm)
    if t0 is None:
        return None
    for its in hops or []:
        ok = [it for it in its if it and (parse_hhmm(it[0].get("dep")) or -1) >= t0 + 3]
        if not ok:
            return None
        t0 = parse_hhmm(ok[0][-1].get("arr"))
        if t0 is None:
            return None
    return t0


def _min_mid_duration_min(tour_entry: dict[str, Any]) -> dict[str, int]:
    """Compute realistic minimum traversal time (minutes + hop buffers) per day."""
    first_base = next((k for k, v in tour_entry.items() if isinstance(v, dict) and "days" in v), None)
    if not first_base:
        return {"wd": 45, "sa": 45, "so": 45}
    days_map = tour_entry[first_base]["days"]
    out: dict[str, int] = {}
    for dk in ("wd", "sa", "so"):
        hops = days_map.get(dk) or []
        if not hops or any(not h for h in hops):
            out[dk] = 60
            continue
        total = 0
        for its in hops:
            durs = [minutes_diff(it[0]["dep"], it[-1]["arr"]) for it in its if it]
            total += min(durs) if durs else 45
        out[dk] = max(45, total + 10 * len(hops))
    return out


def compute_and_merge_midlegs(
    router: planner.DualEngineRouter,
) -> tuple[dict[str, dict[str, int]], dict[tuple[str, str, str], list[Any]]]:
    """Compute midlegs for all 24 new shortcut_360 tours and merge into data/midlegs.json."""
    existing = (
        json.loads(MIDLEGS_PATH.read_text(encoding="utf-8"))
        if MIDLEGS_PATH.exists()
        else {"v": 2, "days": planner.DAYS, "tours": {}, "polys": []}
    )
    existing_tours = dict(existing.get("tours") or {})
    polys: list[str] = list(existing.get("polys") or [])
    pidx: dict[str, int] = {p: i for i, p in enumerate(polys)}

    mid_durs: dict[str, dict[str, int]] = {}
    mid_hops_map: dict[tuple[str, str, str], list[Any]] = {}
    n_hops = n_empty = 0

    for t in NEW_TOURS:
        tid = t["id"]
        if (t.get("transit") or {}).get("kind") not in midlegs_mod.KINDS:
            continue
        per_base: dict[str, Any] = {}
        for base in ("jugenheim", "karlsruhe"):
            chain = midlegs_mod.middle_chain(t, base)
            if not chain:
                continue
            days_out: dict[str, Any] = {}
            for day in planner.DAYS:
                hops = []
                for x, y in zip(chain, chain[1:]):
                    its = midlegs_mod.hop_itineraries(router, x, y, day)
                    for it in its:
                        for seg in it:
                            hit = is_forbidden_fernverkehr(seg)
                            assert hit is None, f"Forbidden segment in midlegs {tid}/{day}: {hit} ({seg.get('op')})"
                    n_hops += 1
                    if not its:
                        n_empty += 1
                    hops.append(its)
                days_out[day] = hops
                mid_hops_map[(tid, base, day)] = hops
            per_base[base] = {
                "chain": [{k: s.get(k) for k in ("name", "lat", "lon", "dhid")} for s in chain],
                "days": days_out,
            }

        mid_durs[tid] = _min_mid_duration_min(per_base)

        for base in list(per_base):
            for day_hops in (per_base[base].get("days") or {}).values():
                for its in day_hops:
                    for itin in its:
                        for seg in itin:
                            for lg in seg.get("legs") or []:
                                p5 = lg.pop("p5", None)
                                if p5 is None and isinstance(lg.get("p"), int) and 0 <= lg["p"] < len(router.polys):
                                    p5 = router.polys[lg["p"]]
                                if p5 is not None:
                                    if p5 not in pidx:
                                        pidx[p5] = len(polys)
                                        polys.append(p5)
                                    lg["p"] = pidx[p5]

        if "jugenheim" in per_base and "karlsruhe" in per_base and per_base["karlsruhe"].get("days") == per_base["jugenheim"].get("days"):
            per_base["karlsruhe"] = {"ref": "jugenheim"}

        existing_tours[tid] = per_base
        first = next(iter(per_base.values()))
        if "ref" in first:
            first = per_base[first["ref"]]
        chain_names = " -> ".join(s["name"] for s in first["chain"])
        cnts = [str(len(h)) for h in first["days"]["wd"]]
        print(f"  [mid] {tid}: {chain_names} (wd: {', '.join(cnts)} | est_mid_wd={mid_durs[tid]['wd']}m)", flush=True)

    out_mid = {"v": 2, "days": planner.DAYS, "tours": existing_tours, "polys": polys}
    MIDLEGS_PATH.write_text(json.dumps(out_mid, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[3/4] Midlegs updated: {len(existing_tours)} tours ({n_hops} new hops, {n_empty} empty)", flush=True)
    return mid_durs, mid_hops_map


def _build_loop_plan_for_slot(
    tour: dict[str, Any],
    out_itins: list[list[dict[str, Any]]],
    ret_itins: list[list[dict[str, Any]]],
    target_mid_stay: int,
    mid_hops: list[Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Plan builder for non-walking transit Rundkurse that avoids middle-stage schedule conflicts."""
    safe_returns = [r for r in ret_itins if planner.arrival_before_0230(r)]
    if not out_itins or not safe_returns:
        return {"fallback": True, "fwd": None, "rev": None, "alts_fwd": [], "alts_rev": [], "lastSafeReturn": None}, (
            "Keine durchgehende Deutschlandticket-Verbindung (Hin- und Rückfahrt am selben Tag) im Fahrplan gefunden."
        )

    def find_pairs(require_mid: bool, min_stay: int) -> list[tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]]:
        out: list[tuple[list[dict[str, Any]], list[tuple[int, list[dict[str, Any]]]]]] = []
        for f in out_itins:
            dep_f = parse_hhmm(f[0].get("dep")) or 0
            if dep_f < 300 or dep_f > 990:
                continue
            mid_end = _mid_finish_min(f[-1].get("arr"), mid_hops or []) if require_mid else None
            if require_mid and mid_hops and mid_end is None:
                continue
            compat: list[tuple[int, list[dict[str, Any]]]] = []
            for r in safe_returns:
                st = compute_hike_stay(f, r)
                if st is None or not (min_stay <= st <= 840):
                    continue
                f_arr_iso, r_dep_iso = f[-1].get("arrIso"), r[0].get("depIso")
                if f_arr_iso and r_dep_iso:
                    if dt.datetime.fromisoformat(r_dep_iso) < dt.datetime.fromisoformat(f_arr_iso):
                        continue
                if require_mid and mid_end is not None:
                    r_dep_m = parse_hhmm(r[0].get("dep"))
                    if r_dep_m is None or r_dep_m < 240:
                        r_dep_adj = (r_dep_m or 0) + 1440
                    else:
                        r_dep_adj = r_dep_m
                    if r_dep_adj < mid_end + 4:
                        continue
                compat.append((st, r))
            if compat:
                out.append((f, compat))
        return out

    used_min_stay = target_mid_stay
    valid_outs = find_pairs(True, target_mid_stay)
    if not valid_outs:
        valid_outs = find_pairs(True, 45)
        used_min_stay = 45
    if not valid_outs:
        for ms in (target_mid_stay, max(45, target_mid_stay - 20), 45):
            valid_outs = find_pairs(False, ms)
            if valid_outs:
                used_min_stay = ms
                break
    if not valid_outs:
        return {"fallback": True, "fwd": None, "rev": None, "alts_fwd": [], "alts_rev": [], "lastSafeReturn": None}, (
            f"Aufenthaltszeit vor Ort reicht am Stichtag nicht für den ÖPNV-Rundkurs ({target_mid_stay} Min.)."
        )

    def score_out(item):
        f = item[0]
        dep_m = parse_hhmm(f[0]["dep"]) or 0
        in_morning = 0 if 430 <= dep_m <= 585 else (1 if 390 <= dep_m <= 660 else 2)
        dur_f = minutes_diff(f[0]["dep"], f[-1]["arr"], f[0].get("depIso"), f[-1].get("arrIso"))
        return (in_morning, dur_f)

    best_out_item = min(valid_outs, key=score_out)
    main_fwd = copy.deepcopy(best_out_item[0])
    compat_for_main = best_out_item[1]
    main_mid_end = _mid_finish_min(main_fwd[-1].get("arr"), mid_hops or [])

    def score_ret(st_r):
        st, r = st_r
        r_dep_m = parse_hhmm(r[0].get("dep")) or 0
        if main_mid_end is not None:
            r_adj = r_dep_m + (1440 if r_dep_m < 240 else 0)
            extra = max(0, r_adj - main_mid_end)
            ideal_diff = abs(extra - 25) // 15
        else:
            ideal_diff = abs((st - used_min_stay) - 45) // 20
        dur_r = minutes_diff(r[0]["dep"], r[-1]["arr"], r[0].get("depIso"), r[-1].get("arrIso"))
        return (ideal_diff, dur_r)

    best_ret_st, best_ret = min(compat_for_main, key=score_ret)
    main_rev = copy.deepcopy(best_ret)

    main_rev_dep = parse_hhmm(main_rev[0]["dep"]) or 0

    def adj_dep(r):
        dm = parse_hhmm(r[0]["dep"]) or 0
        return dm + (1440 if dm < 240 and main_rev_dep > 600 else 0)

    later_or_eq = [item for item in compat_for_main if adj_dep(item[1]) >= main_rev_dep]
    lsr_st, lsr_segs_raw = max(later_or_eq, key=lambda item: adj_dep(item[1])) if later_or_eq else (best_ret_st, main_rev)
    lsr_segs = copy.deepcopy(lsr_segs_raw)

    all_valid_f = [copy.deepcopy(item[0]) for item in valid_outs]
    alts_fwd = dedupe_and_sort_alts(all_valid_f, main=main_fwd, max_alts=8)

    needed_rets = [copy.deepcopy(main_rev), copy.deepcopy(lsr_segs)]
    for af in alts_fwd:
        af_mid_end = _mid_finish_min(af[-1].get("arr"), mid_hops or [])
        best_r = None
        for r in safe_returns:
            st = compute_hike_stay(af, r)
            if st is None or not (used_min_stay <= st <= 840):
                continue
            if af_mid_end is not None:
                rd = parse_hhmm(r[0].get("dep")) or 0
                if (rd + (1440 if rd < 240 else 0)) < af_mid_end + 4:
                    continue
            best_r = r
            break
        if best_r is not None:
            needed_rets.append(copy.deepcopy(best_r))
    for _, r in compat_for_main:
        needed_rets.append(copy.deepcopy(r))
    alts_rev = dedupe_and_sort_alts(needed_rets, main=main_rev, max_alts=8)

    plan = {
        "fallback": False,
        "fwd": main_fwd,
        "rev": main_rev,
        "alts_fwd": alts_fwd,
        "alts_rev": alts_rev,
        "lastSafeReturn": {
            "dep": lsr_segs[0]["dep"],
            "arr": lsr_segs[-1]["arr"],
            "minStayAchieved": lsr_st,
            "isLastSafe": True,
            "segs": lsr_segs,
        },
    }
    note_reason = None
    distinct_deps = {a[0]["dep"] for a in alts_fwd if a}
    if len(distinct_deps) < 3:
        note_reason = (
            f"Eingeschränktes Fahrplanangebot: Nur {len(distinct_deps)} gültige D-Ticket-Abfahrtsfenster "
            f"mit >= {used_min_stay} Min. Zeit für den Rundkurs verfügbar."
        )
    return plan, note_reason


def route_and_merge_data(
    router: planner.DualEngineRouter,
    mid_durs: dict[str, dict[str, int]],
    mid_hops_map: dict[tuple[str, str, str], list[Any]],
) -> None:
    """Route all 30 new tours from both bases & merge cleanly into existing data.json & routing_notes.json."""
    existing_data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    new_ids = {t["id"] for t in NEW_TOURS}
    kept_tours = [t for t in existing_data["tours"] if t["id"] not in new_ids]
    assert len(kept_tours) == 151, f"Expected 151 existing tours in data.json, got {len(kept_tours)}"

    # Gather OD queries for the 30 new tours
    od_tasks: dict[tuple[str, str, str, bool], tuple[dict[str, Any], dict[str, Any], str, bool]] = {}
    for t in NEW_TOURS:
        for bk, binfo in planner.BASES.items():
            sA, sB = planner.resolve_tour_endpoints(t, bk)
            dA, dB = planner.dhid_base(sA), planner.dhid_base(sB)
            for dk in ("wd", "sa", "so"):
                k_out = (bk, dA, dk, True)
                if k_out not in od_tasks:
                    od_tasks[k_out] = (binfo, sA, dk, True)
                k_ret = (bk, dB, dk, False)
                if k_ret not in od_tasks:
                    od_tasks[k_ret] = (sB, binfo, dk, False)

    print(f"[4/4] Routing {len(NEW_TOURS)} new tours ({len(od_tasks)} unique OD queries)...", flush=True)
    od_results: dict[tuple[str, str, str, bool], list[list[dict[str, Any]]]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for k, res in ex.map(lambda it: (it[0], router.get_od_itineraries(*it[1])), list(od_tasks.items())):
            od_results[k] = res

    new_routed: list[dict[str, Any]] = []
    new_notes: list[dict[str, Any]] = []

    for t in NEW_TOURS:
        t_out = copy.deepcopy(t)
        tr = t_out["transit"]
        wmin = int((t_out.get("hike") or {}).get("walkMin") or 0)
        is_loop = tr.get("kind") == "shortcut_360" and wmin == 0
        plans: dict[str, dict[str, Any]] = {"jugenheim": {}, "karlsruhe": {}}
        unroutable: dict[str, dict[str, str]] = {"jugenheim": {}, "karlsruhe": {}}
        slot_notes: list[tuple[str, str, str]] = []
        durs: list[int] = []

        approach_info = {
            "walkToStartM": tr.get("walkToStartM") or 0,
            "walkFromEndM": tr.get("walkFromEndM") or 0,
            "approachMin": int(round(((tr.get("walkToStartM") or 0) + (tr.get("walkFromEndM") or 0)) / 66.67)),
        }

        for bk in ("jugenheim", "karlsruhe"):
            sA, sB = planner.resolve_tour_endpoints(t_out, bk)
            dA, dB = planner.dhid_base(sA), planner.dhid_base(sB)
            for dk in ("wd", "sa", "so"):
                out_itins = od_results.get((bk, dA, dk, True)) or []
                ret_itins = od_results.get((bk, dB, dk, False)) or []
                if is_loop:
                    mid_need = mid_durs.get(t_out["id"], {}).get(dk, 60) + 25
                    hops = mid_hops_map.get((t_out["id"], bk, dk))
                    plan, note_rsn = _build_loop_plan_for_slot(t_out, out_itins, ret_itins, mid_need, mid_hops=hops)
                else:
                    plan, note_rsn = planner.build_plan_for_slot(t_out, out_itins, ret_itins, approach_info=approach_info)

                if plan.get("fallback"):
                    rsn = note_rsn or "Keine gültige Deutschlandticket-Verbindung an diesem Wochentag verfügbar."
                    plan["reason"] = rsn
                    unroutable[bk][dk] = rsn
                    slot_notes.append((bk, dk, rsn))
                else:
                    if note_rsn:
                        unroutable[bk][dk] = note_rsn
                        slot_notes.append((bk, dk, note_rsn))
                    f0, r0 = plan["fwd"], plan["rev"]
                    durs.append(minutes_diff(f0[0]["dep"], r0[-1]["arr"], f0[0].get("depIso"), r0[-1].get("arrIso")))
                plans[bk][dk] = plan

        if slot_notes:
            new_notes.append({
                "tour_id": t_out["id"],
                "title": t_out["title"]["de"],
                "status": "partial_or_unroutable",
                "bases": sorted({x[0] for x in slot_notes}),
                "days": sorted({x[1] for x in slot_notes}),
                "reason": slot_notes[0][2],
            })

        t_out["durationMin"] = int(round(sum(durs) / len(durs))) if durs else max(180, wmin + 120)
        t_out["plans"] = plans
        t_out["unroutable"] = unroutable
        jg_ok = sum(1 for dk in ("wd", "sa", "so") if not plans["jugenheim"][dk].get("fallback"))
        ka_ok = sum(1 for dk in ("wd", "sa", "so") if not plans["karlsruhe"][dk].get("fallback"))
        print(f"  [route] {t_out['id']:50s} JG={jg_ok}/3 KA={ka_ok}/3 dur={t_out['durationMin']}m", flush=True)
        assert jg_ok + ka_ok > 0, f"Tour {t_out['id']} failed on BOTH bases across all days!"
        new_routed.append(t_out)

    # Compact only the 30 new tours, then append polys & offset indices cleanly into existing_data
    tmp_wrapper = {"tours": new_routed, "polys": list(router.polys)}
    slim_data_dict(tmp_wrapper, max_alts=6, max_alts_rev=8)

    existing_polys: list[str] = list(existing_data.get("polys") or [])
    existing_pidx: dict[str, int] = {p: i for i, p in enumerate(existing_polys)}
    new_polys_src: list[str] = tmp_wrapper["polys"]
    remapped_legs: set[int] = set()

    for t in tmp_wrapper["tours"]:
        for b_plans in (t.get("plans") or {}).values():
            for plan in (b_plans or {}).values():
                if not isinstance(plan, dict) or plan.get("fallback"):
                    continue
                for _, segs in _iter_itins_in_plan(plan):
                    for s in segs:
                        for leg in s.get("legs") or []:
                            if id(leg) in remapped_legs:
                                continue
                            remapped_legs.add(id(leg))
                            old_p = leg.get("p")
                            p_str = new_polys_src[old_p] if isinstance(old_p, int) and 0 <= old_p < len(new_polys_src) else ""
                            if p_str not in existing_pidx:
                                existing_pidx[p_str] = len(existing_polys)
                                existing_polys.append(p_str)
                            leg["p"] = existing_pidx[p_str]

    gen_ts = dt.datetime.now(BERLIN_TZ).isoformat(timespec="minutes")
    existing_data["generated"] = gen_ts
    existing_data["tours"] = kept_tours + tmp_wrapper["tours"]
    existing_data["polys"] = existing_polys
    DATA_PATH.write_text(json.dumps(existing_data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Merge routing_notes.json preserving baseline notes
    notes_raw = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
    kept_notes = [n for n in (notes_raw.get("notes") or []) if n.get("tour_id") not in new_ids]
    all_notes = kept_notes + new_notes
    notes_payload = {
        "generated": gen_ts,
        "days": planner.DAYS,
        "totalTours": len(existing_data["tours"]),
        "notedToursCount": len(all_notes),
        "notes": all_notes,
    }
    NOTES_PATH.write_text(json.dumps(notes_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[4/4] Merged data.json ({len(existing_data['tours'])} tours, {DATA_PATH.stat().st_size / 1024:.1f} KB, "
        f"{len(existing_polys)} polys) & routing_notes.json ({len(all_notes)} notes).",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Integrate 30 Deep Research Deutschlandticket tours.")
    parser.add_argument("--catalog-only", action="store_true", help="Only update data/tours_catalog_full.json")
    parser.add_argument("--skip-media", action="store_true", help="Skip Wikimedia image/credit fetching")
    parser.add_argument("--only-route", action="store_true", help="Skip catalog & media, only run midlegs+routing")
    parser.add_argument("--refresh-media", default="", help="Comma-separated tour IDs whose JPEG/credit should be re-fetched")
    args = parser.parse_args()

    cat = json.loads(FULL_CATALOG_PATH.read_text(encoding="utf-8")) if args.only_route else update_catalog()
    if args.catalog_only:
        return 0

    if args.refresh_media:
        ids = {x.strip() for x in args.refresh_media.split(",") if x.strip()}
        credits = json.loads(CREDITS_PATH.read_text(encoding="utf-8")) if CREDITS_PATH.exists() else {}
        for tid in ids:
            jpg = IMG_DIR / f"{tid}.jpg"
            if jpg.exists():
                jpg.unlink()
            credits.pop(tid, None)
        CREDITS_PATH.write_text(json.dumps(credits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not args.skip_media and not args.only_route:
        update_media(cat)

    # Populate stop metadata dictionary
    stop_meta: dict[str, dict[str, Any]] = {
        "de:06432:24355": dict(planner.BASES["jugenheim"]),
        "de:08212:89": dict(planner.BASES["karlsruhe"]),
        "de:08212:90": dict(planner.BASES["karlsruhe"]),
    }
    for st in STOPS.values():
        d = planner.dhid_base(st)
        if d:
            stop_meta[d] = st

    router = planner.DualEngineRouter(planner.CACHE_DIR, stop_meta)
    mid_durs, mid_hops_map = compute_and_merge_midlegs(router)
    route_and_merge_data(router, mid_durs, mid_hops_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
