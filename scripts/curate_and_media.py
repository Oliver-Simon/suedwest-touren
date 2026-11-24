#!/usr/bin/env python3
"""M2 Curated Highlight Tours & Wikimedia Commons Media Pipeline for Südwest-Touren.

Merges:
- data/tours_catalog.json (117 M1 tours, read-only)
- 27 curated highlight tours (9 Pfälzerwald, 9 Schwarzwald, 9 Odenwald)
into data/tours_catalog_full.json (144 tours, 19 waypoints, 1 excludedGpx).

Fetches freely licensed Wikimedia Commons photos via a 4-stage batched waterfall
(IPv4-locked, >=2.2s delay, 429/503 backoff, persistent cache in cache/wikimedia/),
center-crops to 16:9 800x450 JPEG (quality=85) in img/<id>.jpg, and writes
normalized attribution metadata to img_src/credits.json.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import socket
import time
import urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3.util.connection
from PIL import Image

# Force IPv4 so Wikimedia endpoints never stall on IPv6 SYN timeouts.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
M1_CATALOG_PATH = DATA_DIR / "tours_catalog.json"
FULL_CATALOG_PATH = DATA_DIR / "tours_catalog_full.json"
IMG_DIR = BASE_DIR / "img"
IMG_SRC_DIR = BASE_DIR / "img_src"
CREDITS_PATH = IMG_SRC_DIR / "credits.json"
CACHE_DIR = BASE_DIR / "cache" / "wikimedia"
ORIGINALS_DIR = CACHE_DIR / "originals"

USER_AGENT = "suedwest-touren-planner/1.0 (oliver.simon@stud.h-da.de)"
MIN_DELAY_S = 2.2
DE_WP_API = "https://de.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

FREE_LICENSES_RE = re.compile(
    r"^(CC[\s-]?BY(?:[\s-]?SA)?(?:\s+\d\.\d)?|CC0|PD|Public\s+domain|GFDL|CC\s+Public\s+Domain)",
    re.IGNORECASE,
)
BAD_NAME_RE = re.compile(
    r"(wappen|coa|coat_of_arms|flagge|flag|locator|karte|map|posmap|lageplan|"
    r"logo|icon|symbol|sign|diagram|grundriss|plan_|commons-logo|wikimedia|"
    r"ambox|edit-clear|question_book|bewohner|einwohner|demograf|stammbaum|siegel|seal)",
    re.IGNORECASE,
)
VALID_EXT_RE = re.compile(r"\.(?:jpe?g)$", re.IGNORECASE)

CURATED_TOURS: list[dict[str, Any]] = [ { 'id': 'hambacher-schloss-neustadt',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'burg',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Hambacher Schloss & Klausental: Hambach → Neustadt',
               'en': 'Hambach Castle & Klausental: Hambach to Neustadt'},
    'subtitle': { 'de': 'Von der Wiege der deutschen Demokratie über die Haardt-Hänge und das Klausental '
                        'nach Neustadt',
                  'en': 'From the Cradle of German Democracy across Haardt slopes and Klausental to '
                        'Neustadt'},
    'sun': { 'de': 'Sonnige Ost- und Südostterrasse am Hambacher Schloss; schattige Waldpassagen im '
                   'Klausental.',
             'en': 'Sunny east and southeast terrace at Hambach Castle; shaded forest sections through '
                   'Klausental.'},
    'tips': { 'de': 'Bus 502 ab Neustadt Hbf direkt hinauf zum Schloss; Rückfahrt stündlich/halbstündlich ab '
                    'Neustadt (Weinstr) Hbf per RE/S-Bahn.',
              'en': 'Bus 502 runs from Neustadt Hbf directly up to the castle; frequent RE/S-Bahn returns '
                    'from Neustadt (Weinstr) Hbf.'},
    'eveningSun': True,
    'places': [ 'Hambach, Schloss',
                'Hambacher Schloss (325 m)',
                'Zigeunerfelsen',
                'Klausental',
                'Neustadt (Weinstr) Hbf'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Hambach, Schloss',
                                'lat': 49.32588,
                                'lon': 8.116305,
                                'dhid': 'de:07316:3219',
                                'transitousId': 'de-DELFI_de:07316:3219:0:1',
                                'ibnr': None},
                 'endStop': { 'name': 'Neustadt (Weinstr) Hbf',
                              'lat': 49.349552,
                              'lon': 8.140757,
                              'dhid': 'de:07316:3144',
                              'transitousId': 'de-DELFI_de:07316:3144_G',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 120,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 8.8,
              'elevGainM': 240,
              'elevLossM': 400,
              'walkMin': 155,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Hambacher Schloss'},
  { 'id': 'kalmit-gipfel-maikammer',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'berg',
    'difficulty': 'schwer',
    'ring': False,
    'title': { 'de': 'Kalmit-Gipfel & Felsenmeer: Maikammer → St. Martin',
               'en': 'Kalmit Summit & Felsenmeer: Maikammer to St. Martin'},
    'subtitle': { 'de': 'Aufstieg über Weinberge (Radweg Deutsche Weinstraße) zum höchsten Gipfel des '
                        'Pfälzerwalds (673 m)',
                  'en': 'Ascent past vineyards (German Wine Route) to the highest summit of the Palatinate '
                        'Forest (673 m)'},
    'sun': { 'de': 'Sonnige Weinbergsflanken in Maikammer & St. Martin; freie Gipfelaussicht am Kalmithaus '
                   'über die Rheinebene.',
             'en': 'Sunlit vineyard slopes in Maikammer and St. Martin; open Rhine plain panorama from '
                   'Kalmithaus.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Radweg Deutsche Weinstraße ein; Abstieg über Felsenmeer '
                    'Hüttenberg & Kropsburg nach St. Martin (Bus 500/501).',
              'en': "Incorporates Oliver's Mapy waypoint Radweg Deutsche Weinstraße; descent via Felsenmeer "
                    'Hüttenberg & Kropsburg to St. Martin (Bus 500/501).'},
    'eveningSun': False,
    'places': [ 'Maikammer-Kirrweiler',
                'Radweg Deutsche Weinstraße',
                'Alsterweiler',
                'Kalmit (673 m)',
                'Felsenmeer Hüttenberg',
                'Kropsburg',
                'St. Martin, Ort'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Maikammer-Kirrweiler',
                                'lat': 49.30558,
                                'lon': 8.147177,
                                'dhid': 'de:07337:8300',
                                'transitousId': 'de-DELFI_de:07337:8300_G',
                                'ibnr': None},
                 'endStop': { 'name': 'St. Martin, Ort',
                              'lat': 49.299145,
                              'lon': 8.106721,
                              'dhid': 'de:07337:3822',
                              'transitousId': 'de-DELFI_de:07337:3822:0:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 50,
                 'walkFromEndM': 50,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 12.6,
              'elevGainM': 540,
              'elevLossM': 460,
              'walkMin': 240,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Kalmit'},
  { 'id': 'weinbiet-gimmeldingen-haardt',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'berg',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Weinbietturm & Haardtrand: Mußbach → Neustadt',
               'en': 'Weinbiet Tower & Haardt Edge: Mußbach to Neustadt'},
    'subtitle': { 'de': 'Durch die Gimmeldinger Weinberge hinauf zum Weinbiet (554 m) und über Haardt nach '
                        'Neustadt',
                  'en': 'Through Gimmeldingen vineyards up to Weinbiet summit (554 m) and across Haardt to '
                        'Neustadt'},
    'sun': { 'de': 'Sonnenverwöhnter Osthang in Gimmeldingen und offene Südost-Terrassen oberhalb von Haardt '
                   'bis zum späten Nachmittag.',
             'en': 'Sun-drenched east slopes in Gimmeldingen and open southeast terraces above Haardt into '
                   'late afternoon.'},
    'tips': { 'de': 'Passiert das Haardter Turnerdenkmal (Mapy-Waypoint); Weinbiet-Aussichtsturm (554 m) '
                    'bietet Rundblick bis zum Odenwald und Schwarzwald.',
              'en': 'Passes the Haardt gymnastics memorial (Mapy waypoint); Weinbiet Tower (554 m) overlooks '
                    'the Rhine plain to Odenwald and Black Forest.'},
    'eveningSun': True,
    'places': [ 'Mußbach, Bahnhof',
                'Gimmeldingen',
                'Weinbiet (554 m)',
                'Turnerdenkmal Haardt',
                'Neustadt (Weinstr) Hbf'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Mußbach, Bahnhof',
                                'lat': 49.37196,
                                'lon': 8.168529,
                                'dhid': 'de:07316:3135',
                                'transitousId': 'de-DELFI_de:07316:3135',
                                'ibnr': None},
                 'endStop': { 'name': 'Neustadt (Weinstr) Hbf',
                              'lat': 49.349552,
                              'lon': 8.140757,
                              'dhid': 'de:07316:3144',
                              'transitousId': 'de-DELFI_de:07316:3144_G',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 11.2,
              'elevGainM': 430,
              'elevLossM': 440,
              'walkMin': 210,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Weinbiet'},
  { 'id': 'deidesheim-wachtenburg-wachenheim',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'wein',
    'difficulty': 'leicht',
    'ring': False,
    'title': { 'de': 'Forster Weinlagen & Wachtenburg: Deidesheim → Wachenheim',
               'en': 'Forst Vineyards & Wachtenburg: Deidesheim to Wachenheim'},
    'subtitle': { 'de': 'Genusswanderung durch berühmte Riesling-Lagen, vorbei am Turnerdenkmal-Waldrand zur '
                        'Wachtenburg',
                  'en': 'Scenic vineyard walk through famous Riesling slopes past the forest edge to '
                        'Wachtenburg Castle'},
    'sun': { 'de': "Nahezu durchgehend offene Weinbergswege mit viel Sonne und geschütztem Blick vom 'Balkon "
                   "der Pfalz'.",
             'en': 'Almost continuous open vineyard trails with abundant sun and wide views from the '
                   "'Balcony of the Palatinate'."},
    'tips': { 'de': 'Ideal für sonnige Herbst-/Wintertage; direkte RB45-Bahnanbindung an Start und Ziel.',
              'en': 'Ideal on sunny autumn/winter days; direct RB45 regional rail service at both start and '
                    'finish.'},
    'eveningSun': True,
    'places': [ 'Deidesheim, Bahnhof',
                'Forst an der Weinstraße',
                'Pechsteinkopf',
                'Wachtenburg',
                'Wachenheim/Wstr, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Deidesheim, Bahnhof',
                                'lat': 49.408474,
                                'lon': 8.192779,
                                'dhid': 'de:07332:470',
                                'transitousId': 'de-DELFI_de:07332:470:1:2',
                                'ibnr': None},
                 'endStop': { 'name': 'Wachenheim/Wstr, Bahnhof',
                              'lat': 49.439278,
                              'lon': 8.191162,
                              'dhid': 'de:07332:3895',
                              'transitousId': 'de-DELFI_de:07332:3895:1:DB',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.5,
              'elevGainM': 240,
              'elevLossM': 240,
              'walkMin': 165,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Wachtenburg'},
  { 'id': 'limburg-hardenburg-bad-duerkheim',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'burg',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Klosterruine Limburg & Hardenburg (Bad Dürkheim)',
               'en': 'Limburg Abbey Ruins & Hardenburg Castle (Bad Dürkheim)'},
    'subtitle': { 'de': 'Kultur- und Burg-Rundtour vom Bahnhof Bad Dürkheim zu zwei Monumenten der '
                        'Salierzeit',
                  'en': 'Cultural and castle loop from Bad Dürkheim station to two monumental Salian '
                        'heritage sites'},
    'sun': { 'de': 'Sonnige Ausblicke vom Limburghügel und von der Hardenburg-Südterrasse über das '
                   'Isenachtal.',
             'en': "Sunny vistas from Limburg hill and Hardenburg's south terrace across the Isenach "
                   'valley.'},
    'tips': { 'de': 'Ergänzt Olivers Peterskopf-Route um die beiden großen Baudenkmäler südlich/westlich von '
                    'Bad Dürkheim; RHB/RB45 fahren im Halbstundentakt.',
              'en': "Complements Oliver's Peterskopf route with the two major heritage monuments south/west "
                    'of Bad Dürkheim; frequent RHB/RB45 rail returns.'},
    'eveningSun': True,
    'places': [ 'Bad Dürkheim, Bahnhof',
                'Klosterruine Limburg',
                'Burg Hardenburg',
                'Kriemhildenstuhl',
                'Bad Dürkheim, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Bad Dürkheim, Bahnhof',
                                'lat': 49.460632,
                                'lon': 8.170701,
                                'dhid': 'de:07332:116',
                                'transitousId': 'de-DELFI_de:07332:116',
                                'ibnr': '8000698'},
                 'endStop': { 'name': 'Bad Dürkheim, Bahnhof',
                              'lat': 49.460632,
                              'lon': 8.170701,
                              'dhid': 'de:07332:116',
                              'transitousId': 'de-DELFI_de:07332:116',
                              'ibnr': '8000698'},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 11.4,
              'elevGainM': 340,
              'elevLossM': 340,
              'walkMin': 205,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Kloster Limburg'},
  { 'id': 'eschkopfturm-elmstein-pfalz',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'berg',
    'difficulty': 'schwer',
    'ring': True,
    'title': { 'de': 'Eschkopfturm & Elmsteiner Tal (Elmstein-Runde)',
               'en': 'Eschkopf Tower & Elmstein Valley (Elmstein Loop)'},
    'subtitle': { 'de': 'Anspruchsvolle Kernwald-Tour ab Elmstein zum 609 m hohen Eschkopfturm '
                        '(Mapy-Waypoint)',
                  'en': 'Challenging core-forest loop from Elmstein to the 609 m Eschkopf observation tower '
                        '(Mapy waypoint)'},
    'sun': { 'de': 'Tiefe, stille Mischwälder; rundum freie Fernsicht auf der Plattform des Eschkopfturms.',
             'en': 'Deep, quiet mixed forests; 360-degree panoramic views from the Eschkopf tower platform.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Eschkopf tower (611 m) über die verlässliche Buslinie 517 '
                    'ab Lambrecht (Elmstein, Schule) an; frühe Hinfahrt nutzen.',
              'en': "Connects Oliver's Mapy waypoint Eschkopf tower (611 m) via reliable Bus 517 from "
                    'Lambrecht (Elmstein, Schule); take an early outbound bus.'},
    'eveningSun': False,
    'places': ['Elmstein, Schule', 'Mückenwies', 'Eschkopfturm (609 m)', 'Iggelbach', 'Elmstein, Schule'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Elmstein, Schule',
                                'lat': 49.351143,
                                'lon': 7.935978,
                                'dhid': 'de:07332:669',
                                'transitousId': 'de-DELFI_de:07332:669:0:1',
                                'ibnr': None},
                 'endStop': { 'name': 'Elmstein, Schule',
                              'lat': 49.351143,
                              'lon': 7.935978,
                              'dhid': 'de:07332:669',
                              'transitousId': 'de-DELFI_de:07332:669:0:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 50,
                 'walkFromEndM': 50,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 15.2,
              'elevGainM': 460,
              'elevLossM': 460,
              'walkMin': 270,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Eschkopf'},
  { 'id': 'karlstalschlucht-trippstadt-pavillon',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'wanderung',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Karlstalschlucht & Pavillon-Blick: Trippstadt → Schopp',
               'en': 'Karlstal Gorge & Pavilion View: Trippstadt to Schopp'},
    'subtitle': { 'de': 'Durch die wilde Moosalbe-Schlucht und vorbei am Mapy-Waypoint Pavillon zum Bahnhof '
                        'Schopp',
                  'en': 'Through the rocky Moosalbe gorge past the Mapy Pavilion waypoint to Schopp rail '
                        'station'},
    'sun': { 'de': 'Kühl-schattige Felsenschlucht mit Holzbrücken; lichte Waldterrassen rund um den '
                   'Pavillon.',
             'en': 'Cool shaded sandstone gorge with wooden bridges; airy forest clearings near the '
                   'Pavilion.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Pavillon (349 m) direkt ein; Anfahrt per Bus 170 ab '
                    'Kaiserslautern nach Trippstadt, Rückfahrt stündlich per RB64 ab Schopp.',
              'en': "Directly incorporates Oliver's Mapy waypoint Pavillon (349 m); reach Trippstadt via Bus "
                    '170 from Kaiserslautern, hourly RB64 returns from Schopp.'},
    'eveningSun': False,
    'places': ['Trippstadt, Denkmal', 'Karlstalschlucht', 'Pavillon (Moosalbe)', 'Schopp, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Trippstadt, Denkmal',
                                'lat': 49.36065,
                                'lon': 7.778252,
                                'dhid': 'de:07335:94544',
                                'transitousId': 'de-DELFI_de:07335:94544:0:1',
                                'ibnr': None},
                 'endStop': { 'name': 'Schopp, Bahnhof',
                              'lat': 49.358868,
                              'lon': 7.687317,
                              'dhid': 'de:07335:8144',
                              'transitousId': 'de-DELFI_de:07335:8144',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 80,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 10.4,
              'elevGainM': 210,
              'elevLossM': 320,
              'walkMin': 180,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Moosalbe (Schwarzbach)'},
  { 'id': 'almersberg-eusserthal-albersweiler',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'berg',
    'difficulty': 'schwer',
    'ring': False,
    'title': { 'de': 'Almersberg & Kloster Eußerthal: Albersweiler → Eußerthal',
               'en': 'Almersberg & Eußerthal Abbey: Albersweiler to Eußerthal'},
    'subtitle': { 'de': 'Über den aussichtsreichen Almersberg (564 m, Mapy-Waypoint) zur Zisterzienserkirche '
                        'Eußerthal',
                  'en': 'Over panoramic Almersberg summit (564 m, Mapy waypoint) to Eußerthal Cistercian '
                        'church'},
    'sun': { 'de': 'Freie Kuppe am Almersberg mit Wasgau-Rundblick; geschütztes Waldtal beim Abstieg nach '
                   'Eußerthal.',
             'en': 'Open Almersberg summit clearing with Wasgau views; sheltered forest valley descending to '
                   'Eußerthal.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Almersberg (559 m) zwischen Queichtalbahn (Albersweiler) '
                    'und Bus 522 (Eußerthal, Schule) ein.',
              'en': "Connects Oliver's Mapy waypoint Almersberg (559 m) between Queichtal rail "
                    '(Albersweiler) and Bus 522 (Eußerthal, Schule).'},
    'eveningSun': False,
    'places': [ 'Albersweiler, Bahnhof',
                'St. Johann',
                'Almersberg (564 m)',
                'Kloster Eußerthal',
                'Eußerthal, Schule'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Albersweiler, Bahnhof',
                                'lat': 49.218616,
                                'lon': 8.02386,
                                'dhid': 'de:07337:8053',
                                'transitousId': 'de-DELFI_de:07337:8053:1:DB',
                                'ibnr': None},
                 'endStop': { 'name': 'Eußerthal, Schule',
                              'lat': 49.240635,
                              'lon': 7.972629,
                              'dhid': 'de:07337:5757',
                              'transitousId': 'de-DELFI_de:07337:5757:0:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 50,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 11.8,
              'elevGainM': 450,
              'elevLossM': 380,
              'walkMin': 220,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Kloster Eußerthal'},
  { 'id': 'teufelstisch-hinterweidenthal',
    'source': 'curated',
    'region': 'pfaelzerwald',
    'category': 'berg',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Teufelstisch & Felsenweg (Hinterweidenthal)',
               'en': "Devil's Table & Rock Trail (Hinterweidenthal)"},
    'subtitle': { 'de': 'Bahntour zum berühmten 14 m hohen Buntsandstein-Pilzfelsen und durch die '
                        'Kaltenbacher Felsen',
                  'en': 'Rail-accessible loop to the iconic 14 m sandstone mushroom rock and Kaltenbach '
                        'crags'},
    'sun': { 'de': 'Sonnige Felsterrasse am Teufelstisch und weite Wiesentäler rund um Hinterweidenthal.',
             'en': "Sunny rock terrace at the Devil's Table and open meadow valleys around "
                   'Hinterweidenthal.'},
    'tips': { 'de': 'Stündliche Direktverbindung per RB55 (Queichtalbahn) bis Hinterweidenthal, Ort; ideal '
                    'als entspannte Halbtagestour.',
              'en': 'Hourly RB55 rail service directly to Hinterweidenthal, Ort; great relaxed half-day rock '
                    'hike.'},
    'eveningSun': True,
    'places': ['Hinterweidenthal, Ort', 'Teufelstisch', 'Teufelsschmiede', 'Hinterweidenthal, Ort'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Hinterweidenthal, Ort',
                                'lat': 49.1975,
                                'lon': 7.750568,
                                'dhid': 'de:07340:6269',
                                'transitousId': 'de-DELFI_de:07340:6269',
                                'ibnr': None},
                 'endStop': { 'name': 'Hinterweidenthal, Ort',
                              'lat': 49.1975,
                              'lon': 7.750568,
                              'dhid': 'de:07340:6269',
                              'transitousId': 'de-DELFI_de:07340:6269',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.2,
              'elevGainM': 250,
              'elevLossM': 250,
              'walkMin': 165,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Teufelstisch (Hinterweidenthal)'},
  { 'id': 'geroldsauer-wasserfall-baden-baden',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'wanderung',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Geroldsauer Wasserfall & Bütthof (Baden-Baden)',
               'en': 'Geroldsau Waterfall & Bütthof (Baden-Baden)'},
    'subtitle': { 'de': 'Kurze Schwarzwald-Idylle ab Geroldsau entlang des Grobbachs zu Olivers '
                        'Mapy-Waypoint Wasserfall',
                  'en': "Scenic Black Forest valley loop from Geroldsau along Grobbach stream to Oliver's "
                        'Mapy waterfall waypoint'},
    'sun': { 'de': 'Kühles, moosiges Bachtal am Wasserfall; sonnige Waldwiesen an der Waldgaststätte '
                   'Bütthof.',
             'en': 'Cool mossy stream valley at the waterfall; sunny forest meadows around Bütthof inn.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Geroldsauer Wasserfall (320 m) über Bus X45/204 ab '
                    'Baden-Baden an.',
              'en': "Connects Oliver's Mapy waypoint Geroldsauer Wasserfall (320 m) via Bus X45/204 from "
                    'Baden-Baden.'},
    'eveningSun': False,
    'places': [ 'Geroldsau Malschbacher Str.',
                'Geroldsauer Wasserfall',
                'Bütthof',
                'Geroldsau Malschbacher Str.'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Geroldsau Malschbacher Str.',
                                'lat': 48.724846,
                                'lon': 8.247469,
                                'dhid': 'de:08211:32376',
                                'transitousId': 'de-KVV_Pde:08211:32376',
                                'ibnr': None},
                 'endStop': { 'name': 'Geroldsau Malschbacher Str.',
                              'lat': 48.724846,
                              'lon': 8.247469,
                              'dhid': 'de:08211:32376',
                              'transitousId': 'de-KVV_Pde:08211:32376',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 50,
                 'walkFromEndM': 50,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 7.6,
              'elevGainM': 240,
              'elevLossM': 240,
              'walkMin': 140,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Geroldsauer Wasserfall'},
  { 'id': 'wattkopf-bismarckturm-ettlingen',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'berg',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Ettlinger Wattkopf & Bismarckturm (Ettlingen)',
               'en': 'Ettlingen Wattkopf & Bismarck Tower (Ettlingen)'},
    'subtitle': { 'de': 'Hausberg-Runde ab S-Bahn Ettlingen Stadt über Hedwigsquelle zu Olivers '
                        'Mapy-Waypoint Bismarck Tower',
                  'en': "Local peak loop from Ettlingen Stadt S-Bahn via Hedwigsquelle to Oliver's Mapy "
                        'Bismarck Tower waypoint'},
    'sun': { 'de': 'Traumhafter West- und Südwestblick vom Bismarckturm (269 m) über Karlsruhe und die '
                   'Rheinebene bis in die Pfalz.',
             'en': 'Superb west/southwest sunset view from Bismarck Tower (269 m) across Karlsruhe and the '
                   'Rhine plain to the Palatinate.'},
    'tips': { 'de': 'Extrem schnelle Anbindung via S1 ab Karlsruhe (20 Min.) und gute Verbindung ab '
                    'Jugenheim; perfekte Feierabend-/Abendsonne-Tour.',
              'en': 'Ultra-fast S1 connection from Karlsruhe (20 min) and smooth link from Jugenheim; ideal '
                    'sunset lookout.'},
    'eveningSun': True,
    'places': ['Ettlingen Stadt', 'Bismarckturm Wattkopf', 'Kaisereiche', 'Hedwigsquelle', 'Ettlingen Stadt'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Ettlingen Stadt',
                                'lat': 48.93869,
                                'lon': 8.40895,
                                'dhid': 'de:08215:1231',
                                'transitousId': 'de-KVV_Pde:08215:1231',
                                'ibnr': '8007007'},
                 'endStop': { 'name': 'Ettlingen Stadt',
                              'lat': 48.93869,
                              'lon': 8.40895,
                              'dhid': 'de:08215:1231',
                              'transitousId': 'de-KVV_Pde:08215:1231',
                              'ibnr': '8007007'},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.2,
              'elevGainM': 260,
              'elevLossM': 260,
              'walkMin': 165,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Bismarckturm (Ettlingen)'},
  { 'id': 'albtal-falkenstein-bad-herrenalb',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'wanderung',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Albtal-Falkenstein & Kloster Frauenalb: Bad Herrenalb → Frauenalb',
               'en': 'Alb Valley Falkenstein & Frauenalb Abbey: Bad Herrenalb to Frauenalb'},
    'subtitle': { 'de': 'Über den Aussichtsfelsen Falkenstein und den Graf-Rhena-Weg zur barocken '
                        'Klosterruine Frauenalb',
                  'en': 'Via Falkenstein crag and Graf-Rhena-Weg to the baroque ruins of Frauenalb Abbey'},
    'sun': { 'de': 'Sonnige Felsenkanzel am Falkenstein und offene Klosterwiesen in Frauenalb.',
             'en': 'Sunny rock overlook at Falkenstein and open monastery meadows at Frauenalb.'},
    'tips': { 'de': 'Beide Endpunkte liegen direkt an der Albtalbahn S1 (20–30-Min.-Takt nach Karlsruhe '
                    'Hbf).',
              'en': 'Both endpoints sit directly on the Albtalbahn S1 (every 20–30 min to Karlsruhe Hbf).'},
    'eveningSun': False,
    'places': [ 'Bad Herrenalb',
                'Falkenstein',
                'Graf-Rhena-Weg',
                'Klosterruine Frauenalb',
                'Frauenalb - Schielberg'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Bad Herrenalb',
                                'lat': 48.80245,
                                'lon': 8.43907,
                                'dhid': 'de:08235:9558',
                                'transitousId': 'de-KVV_Pde:08235:9558',
                                'ibnr': '8007011'},
                 'endStop': { 'name': 'Frauenalb - Schielberg',
                              'lat': 48.83744,
                              'lon': 8.44049,
                              'dhid': 'de:08215:1304',
                              'transitousId': 'de-KVV_de:08215:1304:1:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.8,
              'elevGainM': 280,
              'elevLossM': 350,
              'walkMin': 175,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Kloster Frauenalb'},
  { 'id': 'allerheiligen-wasserfaelle-oppenau',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'wanderung',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Allerheiligen Wasserfälle & Klosterruine → Oppenau',
               'en': 'Allerheiligen Waterfalls & Abbey Ruins to Oppenau'},
    'subtitle': { 'de': 'Von den 83 m hohen Wasserfall-Kaskaden und der gotischen Klosterruine talwärts nach '
                        'Oppenau',
                  'en': 'From the 83 m waterfall cascades and Gothic abbey ruins downhill through the '
                        'Lierbach valley to Oppenau'},
    'sun': { 'de': 'Schattige Kaskadenschlucht am Morgen; sonnige Bergwiesen auf dem Höhenrücken Richtung '
                   'Oppenau.',
             'en': 'Shaded cascade gorge in the morning; sunny mountain meadows along the ridge toward '
                   'Oppenau.'},
    'tips': { 'de': 'Anfahrt mit Bus 425 bis Allerheiligen Wasserfälle; Abstieg nach Oppenau Bahnhof '
                    'garantiert stündliche Rückfahrten per RS2 bis abends.',
              'en': 'Arrive via Bus 425 at Allerheiligen Wasserfälle; finishing at Oppenau Bahnhof '
                    'guarantees hourly RS2 rail returns into the evening.'},
    'eveningSun': False,
    'places': ['Allerheiligen Wasserfälle', 'Klosterruine Allerheiligen', 'Eselsbrunnen', 'Oppenau Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Allerheiligen Wasserfälle',
                                'lat': 48.52814,
                                'lon': 8.188171,
                                'dhid': 'de:08317:14396',
                                'transitousId': 'de-DELFI_de:08317:14396:1:1',
                                'ibnr': None},
                 'endStop': { 'name': 'Oppenau Bahnhof',
                              'lat': 48.472,
                              'lon': 8.159761,
                              'dhid': 'de:08317:14360',
                              'transitousId': 'de-DELFI_de:08317:14360',
                              'ibnr': '8004679'},
                 'vias': [],
                 'walkToStartM': 50,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 11.2,
              'elevGainM': 280,
              'elevLossM': 590,
              'walkMin': 195,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Allerheiligen-Wasserfälle'},
  { 'id': 'gengenbach-altstadt-kastelberg',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'stadt',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Gengenbach Altstadt & Kastelberg-Weinweg',
               'en': 'Gengenbach Old Town & Kastelberg Vineyard Loop'},
    'subtitle': { 'de': 'Durch die historische Reichsstadt-Altstadt über Berglekapelle und Kastelberg-Reben '
                        'zurück ins Kinzigtal',
                  'en': 'Through historic Imperial-Town alleys up to Bergle Chapel and Kastelberg vineyards '
                        'above the Kinzig valley'},
    'sun': { 'de': 'Milde West- und Südhänge am Kastelberg und sonnige Fachwerkgassen rund um den '
                   'Marktplatz.',
             'en': 'Mild west and south-facing vineyard slopes on Kastelberg and sunny half-timbered '
                   'squares.'},
    'tips': { 'de': 'Direkt an der Schwarzwaldbahn RE2 gelegen (56 Min. ab Karlsruhe Hbf); wunderbare Kombi '
                    'aus Altstadt und Aussicht.',
              'en': 'Directly on Schwarzwaldbahn RE2 (56 min from Karlsruhe Hbf); great mix of historic town '
                    'and ridge views.'},
    'eveningSun': True,
    'places': [ 'Gengenbach Bahnhof',
                'Obertor & Marktplatz',
                'Berglekapelle',
                'Jakobskapelle Kastelberg',
                'Gengenbach Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Gengenbach Bahnhof',
                                'lat': 48.40466,
                                'lon': 8.010192,
                                'dhid': 'de:08317:15810',
                                'transitousId': 'de-DELFI_de:08317:15810',
                                'ibnr': None},
                 'endStop': { 'name': 'Gengenbach Bahnhof',
                              'lat': 48.40466,
                              'lon': 8.010192,
                              'dhid': 'de:08317:15810',
                              'transitousId': 'de-DELFI_de:08317:15810',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 8.4,
              'elevGainM': 250,
              'elevLossM': 250,
              'walkMin': 150,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Gengenbach'},
  { 'id': 'triberger-wasserfaelle-schwarzwaldbahn',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'wanderung',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Triberger Wasserfälle & Bergsee (Schwarzwaldbahn)',
               'en': 'Triberg Waterfalls & Bergsee (Black Forest Railway)'},
    'subtitle': { 'de': 'Panoramafahrt über die Kehrtunnel der Schwarzwaldbahn zu Deutschlands höchsten '
                        'Wasserfällen (163 m)',
                  'en': "Scenic rail ride through the Black Forest Railway loop tunnels to Germany's highest "
                        'waterfalls (163 m)'},
    'sun': { 'de': 'Schattige Tannen-Kaskaden entlang der Gutach; sonnige Lichtung am Bergsee und bei Maria '
                   'in der Tanne.',
             'en': 'Shaded fir cascades along the Gutach; sunny clearing at Bergsee and Maria in der Tanne.'},
    'tips': { 'de': 'Die Fahrt mit dem RE2 über die Schwarzwaldbahn-Steige ist bereits ein Erlebnis; '
                    'stündliche Züge ab Triberg.',
              'en': 'The RE2 ride up the Black Forest Railway grades is an attraction in itself; hourly '
                    'trains from Triberg.'},
    'eveningSun': False,
    'places': ['Triberg', 'Triberger Wasserfälle', 'Bergsee', 'Maria in der Tanne', 'Triberg'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Triberg',
                                'lat': 48.140034,
                                'lon': 8.236886,
                                'dhid': 'de:08326:6591',
                                'transitousId': 'de-DELFI_de:08326:6591_G',
                                'ibnr': None},
                 'endStop': { 'name': 'Triberg',
                              'lat': 48.140034,
                              'lon': 8.236886,
                              'dhid': 'de:08326:6591',
                              'transitousId': 'de-DELFI_de:08326:6591_G',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 7.8,
              'elevGainM': 310,
              'elevLossM': 310,
              'walkMin': 150,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Triberger Wasserfälle'},
  { 'id': 'calw-kloster-hirsau-nagoldtal',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'stadt',
    'difficulty': 'leicht',
    'ring': False,
    'title': { 'de': 'Hermann-Hesse-Stadt Calw & Kloster Hirsau (Nagoldtal)',
               'en': 'Hermann Hesse Town Calw & Hirsau Abbey (Nagold Valley)'},
    'subtitle': { 'de': 'Durch die Fachwerk-Altstadt von Calw und entlang der Nagold zur imposanten '
                        'Klosterruine Hirsau',
                  'en': 'Through half-timbered Calw and along the Nagold river to the monumental ruins of '
                        'Hirsau Abbey'},
    'sun': { 'de': 'Sonniger Marktplatz in Calw und offener Kreuzgang-Innenhof der Klosteranlage Hirsau.',
             'en': 'Sunny market square in Calw and open cloister courtyard inside Hirsau Abbey.'},
    'tips': { 'de': 'Kurze, kulturreiche Talwanderung zwischen den Bahnhöfen Calw und Hirsau (RB74/X63 '
                    'Richtung Pforzheim/Karlsruhe).',
              'en': 'Short, culture-rich valley walk between Calw and Hirsau stations (RB74/X63 toward '
                    'Pforzheim/Karlsruhe).'},
    'eveningSun': False,
    'places': [ 'Calw, Bahnhof/ZOB',
                'Nikolausbrücke & Marktplatz',
                'Nagold-Uferweg',
                'Kloster Hirsau',
                'Hirsau, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Calw, Bahnhof/ZOB',
                                'lat': 48.71448,
                                'lon': 8.741811,
                                'dhid': 'de:08235:9030',
                                'transitousId': 'de-DELFI_de:08235:9030',
                                'ibnr': '8000063'},
                 'endStop': { 'name': 'Hirsau, Bahnhof',
                              'lat': 48.73401,
                              'lon': 8.73677,
                              'dhid': 'de:08235:9020',
                              'transitousId': 'de-DELFI_de:08235:9020:1:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 7.2,
              'elevGainM': 180,
              'elevLossM': 190,
              'walkMin': 125,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Kloster Hirsau'},
  { 'id': 'sankenbach-wasserfall-baiersbronn',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'see',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Sankenbachsee & Wasserfälle (Baiersbronn)',
               'en': 'Lake Sankenbach & Waterfalls (Baiersbronn)'},
    'subtitle': { 'de': 'Murgtal-Klassiker direkt ab Stadtbahn S8 zum eiszeitlichen Karsee und den '
                        'Sankenbach-Kaskaden',
                  'en': 'Murgtal classic directly from S8 light rail to the glacial cirque lake and '
                        'Sankenbach cascades'},
    'sun': { 'de': 'Morgen- und Mittagssonne am spiegelglatten Sankenbachsee; schattiger Aufstieg neben den '
                   'Wasserfällen.',
             'en': 'Morning and midday sun on calm Lake Sankenbach; shaded climb beside the waterfalls.'},
    'tips': { 'de': 'Direkte S8-Stadtbahn ab Karlsruhe Hbf durchs Murgtal nach Baiersbronn; auf Wunsch '
                    'Verlängerung zur Wasenhütte.',
              'en': 'Direct S8 Stadtbahn from Karlsruhe Hbf through the Murg valley to Baiersbronn; optional '
                    'extension to Wasenhütte.'},
    'eveningSun': False,
    'places': ['Baiersbronn', 'Sankenbachsee', 'Sankenbach-Wasserfälle', 'Wasenhütte', 'Baiersbronn'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Baiersbronn',
                                'lat': 48.503574,
                                'lon': 8.372038,
                                'dhid': 'de:08237:1201',
                                'transitousId': 'de-DELFI_de:08237:1201_G',
                                'ibnr': None},
                 'endStop': { 'name': 'Baiersbronn',
                              'lat': 48.503574,
                              'lon': 8.372038,
                              'dhid': 'de:08237:1201',
                              'transitousId': 'de-DELFI_de:08237:1201_G',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 10.8,
              'elevGainM': 360,
              'elevLossM': 360,
              'walkMin': 200,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Sankenbachsee'},
  { 'id': 'sasbachwalden-alde-gott-gaisholl',
    'source': 'curated',
    'region': 'schwarzwald',
    'category': 'wein',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Gaishöll-Wasserfälle & Alde Gott (Sasbachwalden)',
               'en': 'Gaishöll Waterfalls & Alde Gott Vineyards (Sasbachwalden)'},
    'subtitle': { 'de': 'Über 13 Holzbrücken durch die Gaishöll-Schlucht und aussichtsreich durch die '
                        'Alde-Gott-Weinberge',
                  'en': 'Across 13 wooden bridges through Gaishöll Gorge and back via panoramic Alde Gott '
                        'vineyards'},
    'sun': { 'de': 'Nachmittags- und Abendsonne in den Westhängen der Ortenauer Weinberge mit Blick bis zu '
                   'den Vogesen.',
             'en': 'Afternoon and evening sun along west-facing Ortenau vineyard slopes overlooking the '
                   'Vosges.'},
    'tips': { 'de': 'Bus 400/423 ab Achern direkt bis Sasbachwalden Gaishölle; Schnapsbrunnen entlang des '
                    'Alde-Gott-Panoramarundwegs.',
              'en': 'Bus 400/423 from Achern stops directly at Sasbachwalden Gaishölle; famous '
                    "'Schnapsbrunnen' stops along the Alde Gott trail."},
    'eveningSun': True,
    'places': [ 'Sasbachwalden Gaishölle',
                'Gaishöll-Wasserfälle',
                'Burg Hohenrod',
                'Alde-Gott-Panoramarundweg',
                'Sasbachwalden Gaishölle'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Sasbachwalden Gaishölle',
                                'lat': 48.617207,
                                'lon': 8.128523,
                                'dhid': 'de:08317:30044',
                                'transitousId': 'de-DELFI_de:08317:30044:1:1',
                                'ibnr': None},
                 'endStop': { 'name': 'Sasbachwalden Gaishölle',
                              'lat': 48.617207,
                              'lon': 8.128523,
                              'dhid': 'de:08317:30044',
                              'transitousId': 'de-DELFI_de:08317:30044:1:1',
                              'ibnr': None},
                 'vias': [],
                 'walkToStartM': 40,
                 'walkFromEndM': 40,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 8.8,
              'elevGainM': 310,
              'elevLossM': 310,
              'walkMin': 165,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Burg Hohenrod (Sasbachwalden)'},
  { 'id': 'odenwald-kraehberg-kailbach',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'berg',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Krähberg, Jagdschloss & Himbächel-Viadukt: Hetzbach → Kailbach',
               'en': 'Krähberg, Hunting Lodge & Himbächel Viaduct: Hetzbach to Kailbach'},
    'subtitle': { 'de': 'Höhenwanderung über den Krähberg (554 m) zwischen zwei Stationen der Odenwaldbahn',
                  'en': 'Ridge hike across Krähberg (554 m) linking two Odenwaldbahn rail stations'},
    'sun': { 'de': 'Schattige Buchen- und Fichtenwälder am Krähberg-Kamm; sonnige Lichtungen rund um '
                   'Jagdschloss Krähberg.',
             'en': 'Shaded beech and spruce forests along Krähberg ridge; sunny clearings around Krähberg '
                   'Hunting Lodge.'},
    'tips': { 'de': 'Bindet Olivers Mapy-Waypoint Krähberg (554 m) direkt an die Odenwaldbahn RB82 (Hetzbach '
                    '→ Kailbach) an; auf Stundentakt der RB82 achten.',
              'en': "Links Oliver's Mapy waypoint Krähberg (554 m) directly to Odenwaldbahn RB82 (Hetzbach "
                    'to Kailbach); watch hourly RB82 departures.'},
    'eveningSun': False,
    'places': [ 'Oberzent-Hetzbach Bahnhof',
                'Himbächel-Viadukt',
                'Krähberg (554 m)',
                'Jagdschloss Krähberg',
                'Oberzent, Kailbach Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Oberzent-Hetzbach Bahnhof',
                                'lat': 49.5949,
                                'lon': 8.992856,
                                'dhid': 'de:06437:11224',
                                'transitousId': 'de-DELFI_de:06437:11224',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'BUS']},
                 'endStop': { 'name': 'Oberzent, Kailbach Bahnhof',
                              'lat': 49.539337,
                              'lon': 9.077482,
                              'dhid': 'de:06437:11448',
                              'transitousId': 'de-DELFI_de:06437:11448',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 11.8,
              'elevGainM': 340,
              'elevLossM': 375,
              'walkMin': 210,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Jagdschloss Krähberg filetype:bitmap'},
  { 'id': 'odenwald-ohrsberg-tower-eberbach',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'panoramatour',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Eberbach: Ohrsberg Tower, Neckarsteig & Altstadt-Runde',
               'en': 'Eberbach: Ohrsberg Tower, Neckarsteig & Old Town Loop'},
    'subtitle': { 'de': 'Aussichtsreiche Stadtwald-Runde ab Eberbach Bf zum Ohrsberg-Aussichtsturm',
                  'en': 'Scenic woodland loop from Eberbach station up to Ohrsberg Tower'},
    'sun': { 'de': 'Schöner Süd- und Westblick über die Neckarschleife am Ohrsberg-Turm am Nachmittag.',
             'en': 'Great afternoon south and west views over the Neckar bend from Ohrsberg Tower.'},
    'tips': { 'de': 'Direkt ab Bahnhof Eberbach über Olivers Mapy-Waypoint Ohrsberg Tower (235 m) und zurück '
                    'durch die historische Staufer-Altstadt am Pulverturm.',
              'en': "Starts right at Eberbach station via Oliver's Mapy waypoint Ohrsberg Tower (235 m) and "
                    'loops back through the historic old town.'},
    'eveningSun': True,
    'places': [ 'Eberbach',
                'Ohrsberg Tower',
                'Neckarsteig-Promenade',
                'Pulverturm & Altstadt Eberbach',
                'Eberbach'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Eberbach',
                                'type': 'STATION',
                                'lat': 49.465607,
                                'lon': 8.984476,
                                'stationId': '8000369',
                                'remoteId': 'A=1@O=Eberbach@X=8984152@Y=49465769@U=80@L=8000369@i=U×008014121@',
                                'ibnr': '8000369',
                                'dhid': 'de:08226:534',
                                'transitousId': 'de-DELFI_de:08226:534:4:2',
                                'modes': ['BUS', 'ODM', 'REGIONAL_RAIL', 'SUBURBAN']},
                 'endStop': { 'name': 'Eberbach',
                              'type': 'STATION',
                              'lat': 49.465607,
                              'lon': 8.984476,
                              'stationId': '8000369',
                              'remoteId': 'A=1@O=Eberbach@X=8984152@Y=49465769@U=80@L=8000369@i=U×008014121@',
                              'ibnr': '8000369',
                              'dhid': 'de:08226:534',
                              'transitousId': 'de-DELFI_de:08226:534:4:2',
                              'modes': ['BUS', 'ODM', 'REGIONAL_RAIL', 'SUBURBAN']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 5.8,
              'elevGainM': 185,
              'elevLossM': 185,
              'walkMin': 105,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Ohrsberg Eberbach Aussichtsturm'},
  { 'id': 'bergstrasse-auerbacher-schloss-weinlagen',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'wein',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Auerbacher Schloss, Fürstenlager & Weinlagenwanderung → Bensheim',
               'en': 'Auerbach Castle, Fürstenlager & Vineyard Trail to Bensheim'},
    'subtitle': { 'de': 'Klassische Bergstraßen-Weinbergstour über Schloss Auerbach und Kirchberghäuschen',
                  'en': 'Classic Bergstraße vineyard hike via Auerbach Castle and Kirchberghäuschen'},
    'sun': { 'de': 'Sonnenverwöhnte Westhänge der Bergstraße — ideal für Nachmittags- und Abendsonne über '
                   'der Rheinebene.',
             'en': 'Sun-drenched west-facing Bergstraße slopes — ideal for afternoon and evening sun over '
                   'the Rhine plain.'},
    'tips': { 'de': 'Verbindet Auerbacher Schloss, Staatspark Fürstenlager und Olivers Mapy-Waypoint '
                    'Weinlagenwanderung (2022) mit Einkehr am Kirchberghäuschen.',
              'en': "Connects Auerbach Castle, Fürstenlager park, and Oliver's Mapy waypoint "
                    'Weinlagenwanderung (2022) via Kirchberghäuschen.'},
    'eveningSun': True,
    'places': [ 'Auerbach, Bahnhof',
                'Auerbacher Schloss',
                'Staatspark Fürstenlager',
                'Weinlagenwanderung (2022)',
                'Kirchberghäuschen',
                'Bensheim, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Auerbach, Bahnhof',
                                'lat': 49.70179,
                                'lon': 8.613182,
                                'dhid': 'de:06431:248',
                                'transitousId': 'de-DELFI_de:06431:248:3:2',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'BUS']},
                 'endStop': { 'name': 'Bensheim, Bahnhof',
                              'lat': 49.681328,
                              'lon': 8.616717,
                              'dhid': 'de:06431:236',
                              'transitousId': 'de-DELFI_de:06431:236_G',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'SUBURBAN', 'BUS']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.4,
              'elevGainM': 310,
              'elevLossM': 315,
              'walkMin': 170,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Auerbacher Schloss Bensheim'},
  { 'id': 'odenwald-felsenmeer-lautertal',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'berg',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Felsenmeer Lautertal, Felsberg & Ohlyturm → Auerbach',
               'en': 'Felsenmeer Lautertal, Felsberg & Ohly Tower to Auerbach'},
    'subtitle': { 'de': 'Durch das steinerne Meer der Riesen auf den Felsberg und über den Borstein hinab',
                  'en': 'Climb the granite Sea of Rocks to Felsberg and descend via Borstein'},
    'sun': { 'de': 'Im Blockmeer kühlender Laubwald; freie Aussicht vom Felsberg-Plateau und am Abstieg '
                   'Richtung Rheinebene.',
             'en': 'Cool deciduous forest over the granite boulders; open vistas from Felsberg ridge and the '
                   'descent to the Rhine plain.'},
    'tips': { 'de': 'Trittsichere Schuhe fürs Blockmeer! Anreise mit Bus 665 bis Reichenbach Felsenmeer, '
                    'Rückfahrt direkt per RB68 ab Bensheim-Auerbach.',
              'en': 'Sturdy footwear recommended for the boulders! Arrive via Bus 665 to Reichenbach '
                    'Felsenmeer, return directly by RB68 from Auerbach.'},
    'eveningSun': False,
    'places': [ 'Reichenbach, Felsenmeer',
                'Felsenmeer & Riesensäule',
                'Ohlyturm (Felsberg 514 m)',
                'Borstein & Fürstenlager',
                'Auerbach, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Reichenbach, Felsenmeer',
                                'lat': 49.720154,
                                'lon': 8.694075,
                                'dhid': 'de:06431:14133',
                                'transitousId': 'de-DELFI_de:06431:14133:0:1',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['BUS']},
                 'endStop': { 'name': 'Auerbach, Bahnhof',
                              'lat': 49.70179,
                              'lon': 8.613182,
                              'dhid': 'de:06431:248',
                              'transitousId': 'de-DELFI_de:06431:248:3:2',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'BUS']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 8.6,
              'elevGainM': 320,
              'elevLossM': 430,
              'walkMin': 165,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Felsenmeer Lautertal Odenwald'},
  { 'id': 'bergstrasse-starkenburg-heppenheim',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'burg',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Heppenheim: Fachwerk-Altstadt, Starkenburg & Weinlagensteig',
               'en': 'Heppenheim: Timber-Framed Old Town, Starkenburg Castle & Vineyards'},
    'subtitle': { 'de': 'Feierabend- und Genusstour vom Bahnhof zur Starkenburg über dem Schlossberg',
                  'en': 'Scenic half-day loop from Heppenheim station up to Starkenburg Castle'},
    'sun': { 'de': 'Ausgezeichnete Abendsonne auf dem Schlossberg und entlang des Weinlagensteigs bis ins '
                   'Hessische Ried.',
             'en': 'Superb evening sun atop the Schlossberg and along the vineyard trail facing the Rhine '
                   'plain.'},
    'tips': { 'de': 'Ideal als Nachmittagstour mit Altstadtbummel am Marktplatz, Dom der Bergstraße und '
                    'Burgschänke; schnelle RB68/RE60-Anbindung.',
              'en': 'Ideal afternoon tour combining the historic market square, castle terrace, and frequent '
                    'RB68/RE60 rail connections.'},
    'eveningSun': True,
    'places': [ 'Heppenheim, Bahnhof',
                'Altstadt & Marktplatz',
                'Starkenburg (295 m)',
                'Weinlagensteig Hubenweg',
                'Heppenheim, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Heppenheim, Bahnhof',
                                'lat': 49.641552,
                                'lon': 8.633379,
                                'dhid': 'de:06431:1502',
                                'transitousId': 'de-DELFI_de:06431:1502',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'SUBURBAN']},
                 'endStop': { 'name': 'Heppenheim, Bahnhof',
                              'lat': 49.641552,
                              'lon': 8.633379,
                              'dhid': 'de:06431:1502',
                              'transitousId': 'de-DELFI_de:06431:1502',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'SUBURBAN']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 6.4,
              'elevGainM': 215,
              'elevLossM': 215,
              'walkMin': 115,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Starkenburg Bergfried Heppenheim filetype:bitmap'},
  { 'id': 'neckartal-vierburgensteig-dilsberg',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'burg',
    'difficulty': 'mittel',
    'ring': False,
    'title': { 'de': 'Neckarsteinach Vierburgensteig & Burgfeste Dilsberg → Neckargemünd',
               'en': 'Neckarsteinach Four-Castles Trail & Dilsberg Fortress to Neckargemünd'},
    'subtitle': { 'de': 'Fünf mittelalterliche Burgen hoch über der Neckarschleife bis zur S-Bahn Altstadt',
                  'en': 'Five medieval castles high above the Neckar bend to Neckargemünd Altstadt'},
    'sun': { 'de': 'Sonnige Burgtürme und Südhang-Blicke über das Neckartal; schattige Querung durch den '
                   'Wald.',
             'en': 'Sunny castle towers and south-facing valley vistas above the Neckar; shaded woodland '
                   'crossings.'},
    'tips': { 'de': 'Vorder-, Mittel-, Hinterburg und Schadeck in Neckarsteinach, dann über die '
                    'Neckar-Staustufe hinauf nach Dilsberg und hinab nach Neckargemünd Altstadt (S1/S2).',
              'en': 'Visit all four Neckarsteinach castles, cross the Neckar weir up to walled Dilsberg, and '
                    'descend to Neckargemünd Altstadt (S1/S2).'},
    'eveningSun': True,
    'places': [ 'Neckarsteinach, Bahnhof',
                'Vierburgenstadt Neckarsteinach',
                'Neckar-Staustufe',
                'Burgfeste Dilsberg',
                'Neckargemünd, Altstadt'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Neckarsteinach, Bahnhof',
                                'lat': 49.404724,
                                'lon': 8.841569,
                                'dhid': 'de:06431:3047',
                                'transitousId': 'de-DELFI_de:06431:3047:1:2',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'SUBURBAN']},
                 'endStop': { 'name': 'Neckargemünd, Altstadt',
                              'lat': 49.39239,
                              'lon': 8.802954,
                              'dhid': 'de:08226:2994',
                              'transitousId': 'de-DELFI_de:08226:2994_G',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['SUBURBAN']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 10.2,
              'elevGainM': 350,
              'elevLossM': 350,
              'walkMin': 185,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Neckarsteinach Hinterburg filetype:bitmap'},
  { 'id': 'neckartal-heidelberg-koenigstuhl-schloss',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'berg',
    'difficulty': 'mittel',
    'ring': True,
    'title': { 'de': 'Heidelberg: Himmelsleiter, Königstuhl & Heidelberger Schloss',
               'en': 'Heidelberg: Himmelsleiter Steps, Königstuhl & Heidelberg Castle'},
    'subtitle': { 'de': 'Südseite-Klassiker über 1.200 Buntsandsteinstufen zum Gipfel und durch den '
                        'Schlossgarten',
                  'en': 'South-bank classic climbing the sandstone steps to Königstuhl and down via the '
                        'Castle'},
    'sun': { 'de': 'Morgens und nachmittags weite Blicke über Altstadt, Neckar und Rheinebene von Molkenkur '
                   'und Schlossgarten.',
             'en': 'Wide morning and afternoon views over Old Town, Neckar, and Rhine plain from Molkenkur '
                   'and Castle gardens.'},
    'tips': { 'de': 'Start am S-Bahnhof Heidelberg-Altstadt (Karlstor); die historische Bergbahn bleibt als '
                    'Not-Abstieg (nicht im D-Ticket) optional.',
              'en': 'Starts at Heidelberg-Altstadt S-Bahn station; historic funicular is optional (not '
                    'covered by D-Ticket) if skipping the descent.'},
    'eveningSun': True,
    'places': [ 'Heidelberg, Altstadt',
                'Himmelsleiter',
                'Königstuhl (568 m)',
                'Molkenkur',
                'Heidelberger Schloss',
                'Heidelberg, Altstadt'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Heidelberg, Altstadt',
                                'lat': 49.415237,
                                'lon': 8.720887,
                                'dhid': 'de:08221:1166',
                                'transitousId': 'de-DELFI_de:08221:1166_G',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'SUBURBAN']},
                 'endStop': { 'name': 'Heidelberg, Altstadt',
                              'lat': 49.415237,
                              'lon': 8.720887,
                              'dhid': 'de:08221:1166',
                              'transitousId': 'de-DELFI_de:08221:1166_G',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'SUBURBAN']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 9.8,
              'elevGainM': 470,
              'elevLossM': 470,
              'walkMin': 195,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Heidelberger Schloss filetype:bitmap'},
  { 'id': 'odenwald-michelstadt-erbach-fachwerk',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'stadt',
    'difficulty': 'leicht',
    'ring': False,
    'title': { 'de': 'Michelstadt & Erbach: Fachwerkrathaus, Einhardsbasilika & Schloss',
               'en': 'Michelstadt & Erbach: Timber-Framed Town Hall, Basilica & Palace'},
    'subtitle': { 'de': 'Gemütliche Kulturtour durch das Mümlingtal von Michelstadt bis Schloss Erbach',
                  'en': 'Relaxed cultural walk through the Mümling valley from Michelstadt to Erbach Palace'},
    'sun': { 'de': 'Ganztags sonnige Altstadtplätze und offener Parkweg entlang der Mümling.',
             'en': 'Sunny old-town squares and open riverside paths along the Mümling all day.'},
    'tips': { 'de': 'Perfekt für kühlere oder kürzere Tage: weltberühmtes Fachwerkrathaus (1484), '
                    'karolingische Einhardsbasilika und Deutsches Elfenbeinmuseum in Erbach.',
              'en': 'Great for shorter days: 1484 half-timbered town hall, Carolingian Einhard Basilica, and '
                    'Erbach Palace & Ivory Museum.'},
    'eveningSun': False,
    'places': [ 'Michelstadt Bahnhof',
                'Historisches Rathaus Michelstadt',
                'Einhardsbasilika Steinbach',
                'Schloss Erbach & Lustgarten',
                'Erbach (Odw.) Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Michelstadt Bahnhof',
                                'lat': 49.684856,
                                'lon': 9.001453,
                                'dhid': 'de:06437:11222',
                                'transitousId': 'de-DELFI_de:06437:11222',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'BUS']},
                 'endStop': { 'name': 'Erbach (Odw.) Bahnhof',
                              'lat': 49.65671,
                              'lon': 8.989827,
                              'dhid': 'de:06437:11223',
                              'transitousId': 'de-DELFI_de:06437:11223',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'BUS']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 6.2,
              'elevGainM': 75,
              'elevLossM': 65,
              'walkMin': 100,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Michelstadt Historisches Rathaus filetype:bitmap'},
  { 'id': 'neckartal-ladenburg-altstadt-neckarwiese',
    'source': 'curated',
    'region': 'odenwald',
    'category': 'stadt',
    'difficulty': 'leicht',
    'ring': True,
    'title': { 'de': 'Ladenburg: Römerstadt, Mittelalter-Altstadt & Neckarwiese',
               'en': 'Ladenburg: Roman Lopodunum, Medieval Old Town & Neckar Meadows'},
    'subtitle': { 'de': 'Flache Genuss- und Kulturrunde zwischen Fachwerkgassen, Stadtmauer und Neckarufer',
                  'en': 'Flat cultural stroll linking Roman ruins, half-timbered lanes, and Neckar banks'},
    'sun': { 'de': 'Wunderschöne Nachmittags- und Abendsonne auf der weiten Neckarwiese mit Blick auf die '
                   'Bergstraße.',
             'en': 'Lovely afternoon and evening sun along the wide Neckar meadows facing the Bergstraße '
                   'hills.'},
    'tips': { 'de': 'Direkt auf der Main-Neckar-Bahn (RE60/RB68/S5) erreichbar; Lobdengau-Museum, '
                    'Carl-Benz-Haus und Cafés entlang der Hauptstraße.',
              'en': 'Direct rail access on RE60/RB68/S5; combine Lobdengau Museum, Carl Benz House, and '
                    'riverside cafés.'},
    'eveningSun': True,
    'places': [ 'Ladenburg, Bahnhof',
                'Mittelalterliche Altstadt & Hexenturm',
                'Carl-Benz-Haus',
                'Neckarwiese & Lobdengau',
                'Ladenburg, Bahnhof'],
    'transit': { 'kind': 'curated',
                 'startStop': { 'name': 'Ladenburg, Bahnhof',
                                'lat': 49.4743,
                                'lon': 8.60286,
                                'dhid': 'de:08226:1711',
                                'transitousId': 'de-DELFI_de:08226:1711',
                                'ibnr': None,
                                'remoteId': None,
                                'modes': ['REGIONAL_RAIL', 'SUBURBAN', 'BUS']},
                 'endStop': { 'name': 'Ladenburg, Bahnhof',
                              'lat': 49.4743,
                              'lon': 8.60286,
                              'dhid': 'de:08226:1711',
                              'transitousId': 'de-DELFI_de:08226:1711',
                              'ibnr': None,
                              'remoteId': None,
                              'modes': ['REGIONAL_RAIL', 'SUBURBAN', 'BUS']},
                 'vias': [],
                 'walkToStartM': 0,
                 'walkFromEndM': 0,
                 'unlinkedReason': None},
    'hike': { 'hasGpx': False,
              'gpxFiles': [],
              'distKm': 5.2,
              'elevGainM': 25,
              'elevLossM': 25,
              'walkMin': 85,
              'startCoord': None,
              'endCoord': None,
              'poly5': '',
              'elevProfile': []},
    'wikimediaQuery': 'Ladenburg Altstadt filetype:bitmap'}]

MEDIA_QUERY_MAP: dict[str, tuple[str, str, str, tuple[float, float]]] = { 'r360-landau-germersheim': ( 'Diedesfeld',
                               'Germersheim',
                               'Diedesfeld Weinstraße Weinberge',
                               (49.3222, 8.1357)),
  'r360-k-town-trippstadt-waldfischbach': ( 'Trippstadt',
                                            'Schloss Trippstadt',
                                            'Trippstadt Karlstal Pfälzerwald',
                                            (49.3601, 7.7779)),
  'r360-karlsruhe-heilbronn': ( 'Heilbronn',
                                'Schloss Karlsruhe',
                                'Heilbronn Neckar Altstadt',
                                (49.1427, 9.2109)),
  'r360-rammstein-landstuhl': ('Burg Nanstein', 'Landstuhl', 'Burg Nanstein Landstuhl', (49.4379, 7.7254)),
  'r360-lambrecht': ( 'Lambrecht (Pfalz)',
                      'Lindenberg (Pfalz)',
                      'Lambrecht Pfalz Neustadter Tal',
                      (49.3733, 8.0744)),
  'r360-bad-bergzabern-annweiler': ( 'Bad Bergzabern',
                                     'Wasgau',
                                     'Bad Bergzabern Kurpark Schloss',
                                     (49.1032, 8.0028)),
  'r360-eberbach-miltenberg': ('Eberbach', 'Miltenberg', 'Eberbach Neckar Altstadt', (49.4656, 8.9845)),
  'r360-bd-gruenstadt-wo': ( 'Bad Dürkheim',
                             'Grünstadt',
                             'Bad Dürkheim Gradierbau Kurpark',
                             (49.4607, 8.1707)),
  'r360-homburg-kusel': ('Burg Lichtenberg (Pfalz)', 'Kusel', 'Burg Lichtenberg Kusel', (49.3281, 7.337)),
  'r360-forbach-buehl': ( 'Schwarzenbachtalsperre',
                          'Forbach (Baden)',
                          'Schwarzenbachtalsperre Schwarzwald',
                          (48.6533, 8.328)),
  'r360-bad-herrenalb-buehl': ( 'Kloster Herrenalb',
                                'Bad Herrenalb',
                                'Bad Herrenalb Albtal',
                                (48.8031, 8.4397)),
  'r360-neue-buslinie-gross-rohrheim-huettenfeld': ( 'Groß-Rohrheim',
                                                     'Lampertheim',
                                                     'Groß-Rohrheim Hessisches Ried',
                                                     (49.7204, 8.4841)),
  'r360-nothweiler-oder-dahn-stunde-pause-tour': ( 'Dahner Felsenland',
                                                   'Nothweiler',
                                                   'Dahner Felsenland Wasgau',
                                                   (49.098, 7.9365)),
  'r360-bad-herrenalb-buehl-rastatt': ( 'Schloss Favorite (Rastatt)',
                                        'Rastatt',
                                        'Bühl Schwarzwald Reben',
                                        (48.7928, 8.427)),
  'r360-baumholder-idar-oberstein': ( 'Baumholder',
                                      'Idar-Oberstein',
                                      'Baumholder Stadtweiher',
                                      (49.5383, 7.4111)),
  'r360-neue-runde-gernsheim': ('Gernsheim', 'Darmstadt', 'Gernsheim Rhein Fähranleger', (49.7546, 8.4968)),
  'r360-achern-freudenstadt': ( 'Kniebis',
                                'Schwarzwaldhochstraße',
                                'Kniebis Schwarzwaldhochstraße',
                                (48.634, 8.0653)),
  'r360-koblenz-limburg': ( 'Deutsches Eck',
                            'Limburg an der Lahn',
                            'Koblenz Deutsches Eck Rhein Mosel',
                            (50.3516, 7.5885)),
  'r360-grosser-feldberg': ('Großer Feldberg', 'Taunus', 'Großer Feldberg Taunus Turm', (50.232, 8.4571)),
  'pair-rastatt-rhein-runde-via-blankenloch-illingen': ( 'Residenzschloss Rastatt',
                                                         'Stutensee',
                                                         'Residenzschloss Rastatt',
                                                         (48.8572, 8.2031)),
  'pair-neckar-odenwald-runde-eberbach-mosbach-aglaster': ( 'Mosbach',
                                                            'Aglasterhausen',
                                                            'Mosbach Fachwerk Marktplatz',
                                                            (49.3526, 9.1469)),
  'pair-odenwald-dreilaender-runde-eulbach-amorbach-mud': ( 'Eulbacher Park',
                                                            'Amorbach',
                                                            'Eulbacher Park Odenwald',
                                                            (49.6796, 9.0755)),
  'pair-gertelbachschlucht-sand-kapelle-buehlertal': ( 'Gertelbach-Wasserfälle',
                                                       'Bühlertal',
                                                       'Gertelbach Wasserfälle Bühlertal',
                                                       (48.657, 8.2375)),
  'pair-loreley-plateau-taunus-runde-st-goarshausen-nas': ( 'Loreley',
                                                            'Sankt Goarshausen',
                                                            'Loreley Felsen Rhein',
                                                            (50.1401, 7.7328)),
  'pair-spessart-main-runde-rothenbuch-lohr-am-main-wue': ( 'Rothenbuch',
                                                            'Lohr am Main',
                                                            'Rothenbuch Schloss Spessart',
                                                            (49.9636, 9.389)),
  'pair-odenwald-main-querung-mudau-miltenberg-moemling': ( 'Mudau',
                                                            'Mömlingen',
                                                            'Mudau Odenwald',
                                                            (49.5339, 9.2024)),
  'pair-renchtal-ortenau-runde-sasbachried-oppenau-offe': ( 'Oppenau',
                                                            'Allerheiligen-Wasserfälle',
                                                            'Oppenau Renchtal Schwarzwald',
                                                            (48.472, 8.1601)),
  'pair-pfaelzerwald-busrunde-ramstein-fischbach-franke': ( 'Frankenstein (Pfalz)',
                                                            'Burg Frankenstein (Pfalz)',
                                                            'Burg Frankenstein Pfalz',
                                                            (49.4393, 7.9776)),
  'hoeningen-sonnenwende-bad-duerkheim': ( 'Bismarckturm (Bad Dürkheim)',
                                           'Altleiningen',
                                           'Bismarckturm Bad Dürkheim Peterskopf',
                                           (49.4918, 8.076)),
  'buchen-odenwald-bus-rueckfahrt': ( 'Buchen (Odenwald)',
                                      'Reichelsheim (Odenwald)',
                                      'Buchen Odenwald Stadtturm',
                                      (49.5236, 9.327)),
  'oberzent-kortelshuette-hirschhorn-bf': ( 'Hirschhorn (Neckar)',
                                            'Burg Hirschhorn',
                                            'Hirschhorn Neckar Burg',
                                            (49.4797, 8.9214)),
  'hornisgrinde-turm-kaltenbrunnen': ( 'Hornisgrinde',
                                       'Hornisgrindeturm',
                                       'Hornisgrinde Turm Schwarzwald',
                                       (48.6014, 8.2009)),
  'weyher-rietburgbahn': ( 'Rietburgbahn',
                           'Weyher in der Pfalz',
                           'Rietburgbahn Sessellift',
                           (49.2663, 8.0836)),
  'buchklingen-weinheim-schlossbergterrasse': ( 'Windeck (Weinheim)',
                                                'Weinheim',
                                                'Burg Windeck Weinheim',
                                                (49.5447, 8.7323)),
  'burg-windeck-buehlertal-bosch': ( 'Burg Alt-Windeck',
                                     'Bühl (Baden)',
                                     'Burg Alt-Windeck Bühl',
                                     (48.6722, 8.1578)),
  'nieder-liebersbach-weinheim-bergstrasse': ( 'Weinheimer Schloss',
                                               'Birkenau (Odenwald)',
                                               'Weinheim Schlosspark',
                                               (49.5849, 8.6998)),
  'kaltenbronn-gaistal-talwiese': ('Kaltenbronn', 'Hohloh', 'Kaltenbronn Wildseemoor', (48.7389, 8.4139)),
  'zwingenberg-baden-wolfsschlucht': ( 'Schloss Zwingenberg (Baden)',
                                       'Wolfsschlucht (Zwingenberg)',
                                       'Schloss Zwingenberg Neckar',
                                       (49.4161, 9.0427)),
  'ebersteinburg-battertfelsen-altes-schloss-westrunde': ( 'Battertfelsen',
                                                           'Schloss Hohenbaden',
                                                           'Battertfelsen Baden-Baden',
                                                           (48.778, 8.2713)),
  'burg-trifels-leinsweiler': ( 'Reichsburg Trifels',
                                'Leinsweiler',
                                'Burg Trifels Annweiler',
                                (49.1944, 7.9805)),
  'dimbach-darstein-wasgau-felsenweg': ( 'Dimbach (Pfalz)',
                                         'Darstein',
                                         'Dimbach Pfalz Felsen',
                                         (49.1675, 7.8979)),
  'weinstrasse-bus-500-st-martin-burrweiler-landau': ( 'Sankt Martin (Pfalz)',
                                                       'Burrweiler',
                                                       'Sankt Martin Pfalz Weinstraße',
                                                       (49.2992, 8.107)),
  'kurpfalz-bus-s6-grosssachsen-schwetzingen-wiesloch': ( 'Schloss Schwetzingen',
                                                          'Hirschberg an der Bergstraße',
                                                          'Schloss Schwetzingen Park',
                                                          (49.3836, 8.5712)),
  'neckartal-stauferstadt-bad-wimpfen-via-heinsheim': ( 'Bad Wimpfen',
                                                        'Blauer Turm (Bad Wimpfen)',
                                                        'Bad Wimpfen Altstadt Neckar',
                                                        (49.2305, 9.1636)),
  'odenwald-franken-tour-mosbach-wuerzburg-hbf': ( 'Festung Marienberg',
                                                   'Würzburg',
                                                   'Würzburg Alte Mainbrücke Marienberg',
                                                   (49.7913, 9.9534)),
  'kraichgau-panoramatour-nach-karlsruhe-ueber-eppingen': ( 'Eppingen',
                                                            'Kraichgau',
                                                            'Eppingen Fachwerk Altstadt',
                                                            (49.1372, 8.9094)),
  'enztal-schwarzwaldtour-bad-wildbad-via-calw': ( 'Calw',
                                                   'Bad Wildbad',
                                                   'Calw Fachwerk Nagold',
                                                   (48.7145, 8.7405)),
  'main-tauber-tour-wertheim-wuerzburg-hbf': ( 'Burg Wertheim',
                                               'Wertheim',
                                               'Burg Wertheim Main Tauber',
                                               (49.7597, 9.5186)),
  'bauland-franken-bus-buchen-odenw-wuerzburg-hbf': ( 'Würzburger Residenz',
                                                      'Bauland (Landschaft)',
                                                      'Würzburger Residenz',
                                                      (49.7928, 9.939)),
  'schwarzwald-querspange-karlsruhe-gruenwettersbach-pf': ( 'Pforzheim',
                                                            'Grünwettersbach',
                                                            'Grünwettersbach Fernmeldeturm',
                                                            (48.9583, 8.4668)),
  'alb-murg-bus-baden-baden-gaggenau-ettlingen': ( 'Ettlingen',
                                                   'Gaggenau',
                                                   'Ettlingen Schloss Altstadt',
                                                   (48.9408, 8.4071)),
  'enz-albtal-verbindung-pforzheim-bad-herrenalb': ( 'Bad Herrenalb',
                                                     'Albtal',
                                                     'Bad Herrenalb Kurpark',
                                                     (48.8031, 8.4397)),
  'odenwald-main-buslinie-eberbach-wuerzburg-hbf': ( 'Alte Mainbrücke',
                                                     'Eberbach',
                                                     'Würzburg Alte Mainbrücke',
                                                     (49.8022, 9.9359)),
  'schwarzwaldhochstrasse-achern-oppenau-ruhestein': ( 'Ruhestein',
                                                       'Achern',
                                                       'Ruhestein Nationalparkzentrum',
                                                       (48.5608, 8.2206)),
  'ried-bergstrasse-bus-610-worms-huettenfeld-bensheim': ( 'Wormser Dom',
                                                           'Bensheim',
                                                           'Worms Dom Rhein',
                                                           (49.6349, 8.3566)),
  'suedpfalz-buslinie-wiesloch-walldorf-klingenmuenster': ( 'Klingenmünster',
                                                            'Kandel (Pfalz)',
                                                            'Klingenmünster Kloster Stift',
                                                            (49.1402, 8.0184)),
  'nordpfalz-bahn-bus-bad-duerkheim-kirchheimbolanden': ( 'Kirchheimbolanden',
                                                          'Donnersberg',
                                                          'Kirchheimbolanden Stadtmauer',
                                                          (49.6619, 8.0175)),
  'mainviereck-bus-miltenberg-wuerzburg-hbf': ( 'Mildenburg',
                                                'Mainviereck',
                                                'Miltenberg Main Mildenburg',
                                                (49.7063, 9.2486)),
  'nationalpark-querung-ottenhoefen-hornisgrinde-oppena': ( 'Ottenhöfen im Schwarzwald',
                                                            'Nationalpark Schwarzwald',
                                                            'Ottenhöfen Schwarzwald Mühlenweg',
                                                            (48.5669, 8.151)),
  'spessart-odenwald-korridor-miltenberg-wildensee-heid': ( 'Eschau (Unterfranken)',
                                                            'Hardheim',
                                                            'Spessart Wald Eschau',
                                                            (49.8265, 9.3555)),
  'heidelberger-hoehenbus-neuenheim-ziegelhausen-langer': ( 'Alte Brücke (Heidelberg)',
                                                            'Ziegelhausen',
                                                            'Heidelberg Alte Brücke Neckar',
                                                            (49.4152, 8.7089)),
  'ortenau-korridor-kehl-rathaus-offenburg-buehl': ( 'Offenburg',
                                                     'Kehl',
                                                     'Offenburg Altstadt Rathaus',
                                                     (48.4765, 7.9468)),
  'ludwigsdorf-korsika-eiterbach': ( 'Schönmattenwag',
                                     'Wald-Michelbach',
                                     'Wald-Michelbach Odenwald',
                                     (49.5171, 8.8589)),
  'ahlmuehle-aussichtspunkt-schuhmacherfelsen': ( 'Leinsweiler',
                                                  'Ranschbach',
                                                  'Leinsweiler Slevogthof',
                                                  (49.1895, 7.9913)),
  'bad-bergzabern-kurpark-runde': ( 'Schloss Bad Bergzabern',
                                    'Bad Bergzabern',
                                    'Bad Bergzabern Schloss Kurpark',
                                    (49.1, 7.9892)),
  'bad-herrenalb-kullenmuehle': ('Gaistal', 'Bad Herrenalb', 'Bad Herrenalb Gaistal', (48.8024, 8.4391)),
  'kaiserslautern-betzenberg-moelschbach': ( 'Mölschbach',
                                             'Kaiserslautern',
                                             'Mölschbach Pfälzerwald',
                                             (49.4285, 7.7833)),
  'boppard-buchholz-vierseenblick-mittelrhein': ( 'Vierseenblick',
                                                  'Boppard',
                                                  'Boppard Vierseenblick Rhein',
                                                  (50.2108, 7.5546)),
  'idar-oberstein-markt-felsenkirche-schloss': ( 'Felsenkirche (Idar-Oberstein)',
                                                 'Schloss Oberstein',
                                                 'Idar-Oberstein Felsenkirche',
                                                 (49.7046, 7.3278)),
  'dossenheim-strahlenburg-schriesheim': ( 'Strahlenburg',
                                           'Schriesheim',
                                           'Strahlenburg Schriesheim',
                                           (49.4484, 8.6698)),
  'drei-buchen-burg-meistersel': ( 'Burg Meistersel',
                                   'Ramberg (Pfalz)',
                                   'Burg Meistersel Modenbachtal',
                                   (49.2704, 8.0245)),
  'drei-buchen-orensfelsen': ('Orensberg', 'Orensfelsen', 'Orensfelsen Pfälzerwald', (49.2704, 8.0245)),
  'drei-eichen-burg-guttenberg': ( 'Burg Guttenberg (Pfalz)',
                                   'Oberotterbach',
                                   'Burg Guttenberg Pfalz',
                                   (49.0876, 7.926)),
  'edenkoben-ludwigshoehe-rietburgbahn': ( 'Schloss Villa Ludwigshöhe',
                                           'Edenkoben',
                                           'Villa Ludwigshöhe Edenkoben',
                                           (49.2789, 8.1194)),
  'erlenbach-bei-dahn-niederschlettenbach': ( 'Burg Berwartstein',
                                              'Erlenbach bei Dahn',
                                              'Burg Berwartstein Erlenbach',
                                              (49.1127, 7.8626)),
  'heidelberg-heiligenberg-handschuhsheim-neuenheim': ( 'Heiligenberg (Heidelberg)',
                                                        'Thingstätte (Heidelberg)',
                                                        'Heidelberg Heiligenberg Thingstätte',
                                                        (49.4318, 8.7054)),
  'hauenstein-felsenrunde-ortsmitte': ( 'Hauenstein (Pfalz)',
                                        'Hauensteiner Schusterpfad',
                                        'Hauenstein Pfalz Buntsandstein Felsen',
                                        (49.1919, 7.8548)),
  'hauenstein-schwanheim': ( 'Schwanheim (Pfalz)',
                             'Hauenstein (Pfalz)',
                             'Schwanheim Pfalz Wasgau',
                             (49.1919, 7.8548)),
  'spessart-jaegerwiese-rothenbuch': ( 'Schloss Rothenbuch',
                                       'Rothenbuch',
                                       'Rothenbuch Spessart Wald',
                                       (49.9686, 9.3994)),
  'kaltenbronn-teufelsmuehle-gaistal-bernbach': ( 'Teufelsmühle (Schwarzwald)',
                                                  'Loffenau',
                                                  'Teufelsmühle Schwarzwald',
                                                  (48.7389, 8.4139)),
  'klingenmuenster-burg-landeck-pfalzklinikum': ( 'Burg Landeck (Pfalz)',
                                                  'Klingenmünster',
                                                  'Burg Landeck Klingenmünster',
                                                  (49.1392, 8.009)),
  'eberbach-ledigsberg-neckarblick': ('Ohrsberg', 'Eberbach', 'Eberbach Neckar Blick', (49.4504, 8.9791)),
  'cramerhaus-burg-lindelbrunn': ( 'Burg Lindelbrunn',
                                   'Vorderweidenthal',
                                   'Burg Lindelbrunn Pfalz',
                                   (49.143, 7.8988)),
  'loffenau-hoerden-murgtal': ( 'Loffenau',
                                'Hörden (Gaggenau)',
                                'Loffenau Murgtal Schwarzwald',
                                (48.774, 8.3825)),
  'ludwigsturm-weyher-altes-rathaus': ( 'Blättersberg',
                                        'Weyher in der Pfalz',
                                        'Ludwigsturm Blättersberg',
                                        (49.2695, 8.0678)),
  'loehrbach-unter-abtsteinach': ( 'Abtsteinach',
                                   'Birkenau (Odenwald)',
                                   'Buchklingen Birkenau Odenwald',
                                   (49.5448, 8.7755)),
  'not-gottes-kapelle-melibokus-turm': ( 'Melibokus',
                                         'Auerbach (Bensheim)',
                                         'Melibokus Aussichtsturm',
                                         (49.7154, 8.6397)),
  'nothweiler-rundtour-weglenburg-felsland': ( 'Wegelnburg',
                                               'Nothweiler',
                                               'Wegelnburg Nothweiler',
                                               (49.0688, 7.7989)),
  'oberzent-rothenberg-hirschhorn-bf': ( 'Rothenberg (Oberzent)',
                                         'Oberzent',
                                         'Rothenberg Odenwald',
                                         (49.4997, 8.9187)),
  'winterkasten-bismarckwarte-lindenfels': ( 'Burg Lindenfels',
                                             'Lindenfels',
                                             'Burg Lindenfels Odenwald',
                                             (49.704, 8.7847)),
  'herrenwies-badener-hoehe-friedrichsturm-rundtour': ( 'Badener Höhe',
                                                        'Herrenwies',
                                                        'Friedrichsturm Badener Höhe',
                                                        (48.6584, 8.2668)),
  'mummelsee-hornisgrinde-rundtour': ( 'Mummelsee',
                                       'Seebach (Baden)',
                                       'Mummelsee Schwarzwald',
                                       (48.5956, 8.2014)),
  'sand-friedrichsturm-schwarzenbachtalsperre': ( 'Mehliskopf',
                                                  'Sand (Bühl)',
                                                  'Mehliskopf Schwarzwald',
                                                  (48.6586, 8.2404)),
  'mummelsee-hornisgrinde-unterstmatt': ( 'Unterstmatt',
                                          'Mummelsee',
                                          'Hornisgrinde Mummelsee Panorama',
                                          (48.5956, 8.2014)),
  'eschbach-burg-madenburg': ('Madenburg', 'Eschbach (Pfalz)', 'Burg Madenburg Eschbach', (49.1741, 8.002)),
  'godramstein-frankweiler-orensfelsen': ( 'Frankweiler',
                                           'Ringelsberg',
                                           'Frankweiler Pfalz Weinstraße',
                                           (49.2118, 8.0815)),
  'katzenbuckel-gipfelrunde-waldkatzenbach': ( 'Katzenbuckel',
                                               'Waldbrunn (Odenwald)',
                                               'Katzenbuckel Aussichtsturm Odenwald',
                                               (49.4677, 9.0458)),
  'annweiler-sarnstall-adelberg-aussicht': ( 'Annweiler am Trifels',
                                             'Sarnstall',
                                             'Annweiler am Trifels',
                                             (49.2084, 7.9631)),
  'ranschbach-schuhmacherfelsen': ( 'Ranschbach',
                                    'Ilbesheim bei Landau in der Pfalz',
                                    'Ranschbach Weinstraße',
                                    (49.1948, 8.0102)),
  'lug-wernersberger-geierstein': ('Lug (Pfalz)', 'Wernersberg', 'Lug Pfalz Felsen', (49.1767, 7.9141)),
  'rhodt-mitte-rietburgbahn': ( 'Rhodt unter Rietburg',
                                'Rietburg',
                                'Rhodt unter Rietburg Theresienstraße',
                                (49.2712, 8.1077)),
  'bad-wildbad-sommerberg-baumwipfelpfad': ( 'Baumwipfelpfad Schwarzwald',
                                             'Sommerberg (Bad Wildbad)',
                                             'Baumwipfelpfad Schwarzwald Bad Wildbad',
                                             (48.7509, 8.5398)),
  'rietburg-ludwigsturm': ('Rietburg', 'Rhodt unter Rietburg', 'Rietburg Rhodt Pfalz', (49.2781, 8.0801)),
  'rietburg-rhodt-unter-rietburg': ( 'Rhodt unter Rietburg',
                                     'Edenkoben',
                                     'Rhodt unter Rietburg Weinberge',
                                     (49.2782, 8.0797)),
  'siedelsbrunn-buddhas-weg-eiterbach': ( 'Siedelsbrunn',
                                          'Heiligkreuzsteinach',
                                          'Siedelsbrunn Odenwald',
                                          (49.5462, 8.8033)),
  'doerrenbach-staeffelsberg-runde': ( 'Dörrenbach',
                                       'Stäffelsberg',
                                       'Dörrenbach Rathaus Pfalz',
                                       (49.0871, 7.9518)),
  'varnhalt-yburg': ('Yburg (Baden-Baden)', 'Varnhalt', 'Yburg Baden-Baden', (48.7352, 8.1794)),
  'voelkersbach-malsch': ( 'Malsch (Landkreis Karlsruhe)',
                           'Völkersbach (Malsch)',
                           'Völkersbach Malsch Schwarzwald',
                           (48.8714, 8.3896)),
  'wenschdorf-haagsaussicht-miltenberg': ( 'Miltenberg',
                                           'Engelberg (Kloster)',
                                           'Miltenberg Marktplatz Schnatterloch',
                                           (49.6793, 9.2875)),
  'annweiler-rehbergturm': ('Rehberg (Wasgau)', 'Rehbergturm', 'Rehbergturm Annweiler', (49.19, 7.9755)),
  'ebersteinburg-nordost-rundtour': ( 'Burg Alt-Eberstein',
                                      'Ebersteinburg',
                                      'Burg Alt-Eberstein Ebersteinburg',
                                      (48.778, 8.2713)),
  'freiolsheim-mahlberg-rundtour': ( 'Mahlberg (Schwarzwald)',
                                     'Freiolsheim',
                                     'Mahlbergturm Schwarzwald',
                                     (48.8398, 8.3802)),
  'chain-drei-buchen-orensfelsen-godramstein': ( 'Godramstein',
                                                 'Orensberg',
                                                 'Godramstein Queichtal Weinberge',
                                                 (49.2704, 8.0245)),
  'chain-rietburg-weyher-loop': ( 'Weyher in der Pfalz',
                                  'Rietburg',
                                  'Weyher in der Pfalz Weinberge',
                                  (49.2663, 8.0836)),
  'chain-edenkoben-rietburgbahn-ludwigsturm-weyher': ( 'Edenkoben',
                                                       'Villa Ludwigshöhe',
                                                       'Villa Ludwigshöhe Edenkoben',
                                                       (49.2741, 8.1037)),
  'chain-rhodt-rietburg-loop': ( 'Edenkoben',
                                 'Rhodt unter Rietburg',
                                 'Edenkoben Weinstraße Pfälzerwald',
                                 (49.2712, 8.1077)),
  'chain-gertelbach-sand-schwarzenbach': ( 'Bühlertal',
                                           'Badener Höhe',
                                           'Bühlertal Schwarzwald Panorama',
                                           (48.6704, 8.2007)),
  'chain-korsika-eiterbach-siedelsbrunn': ( 'Heiligkreuzsteinach',
                                            'Abtsteinach',
                                            'Heiligkreuzsteinach Odenwald',
                                            (49.5171, 8.8589))}

CURATED_COMMONS_OVERRIDES: dict[str, str] = {
    "gengenbach-altstadt-kastelberg": "Altstadt Gengenbach",
}


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    txt = html.unescape(str(raw))
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def good_name(file_title: str) -> bool:
    if not file_title:
        return False
    name = re.sub(r"^(?:Datei|File):", "", file_title.strip(), flags=re.IGNORECASE)
    if not VALID_EXT_RE.search(name):
        return False
    if BAD_NAME_RE.search(name):
        return False
    return True


def normalize_file_title(title: str) -> str:
    t = title.strip()
    t = re.sub(r"^(?:Datei|File):", "File:", t, flags=re.IGNORECASE)
    if not t.startswith("File:"):
        t = "File:" + t
    return t


def normalize_license(raw_lic: str) -> str | None:
    lic = strip_html(raw_lic)
    if not lic:
        return None
    low = lic.lower()
    if any(bad in low for bad in ("copyrighted", "fair use", "non-free", "all rights reserved")):
        return None
    if low.startswith("gnu free documentation license") or low == "gfdl":
        lic = "GFDL"
    elif low.startswith("pd-") or low.startswith("public domain"):
        lic = "Public domain"
    elif low.startswith("cc-by-sa-"):
        lic = re.sub(r"^cc-by-sa-", "CC BY-SA ", lic, flags=re.IGNORECASE)
    elif low.startswith("cc-by-"):
        lic = re.sub(r"^cc-by-", "CC BY ", lic, flags=re.IGNORECASE)
    elif low.startswith("cc-zero") or low == "cc0":
        lic = "CC0"
    if not FREE_LICENSES_RE.match(lic):
        return None
    return lic


def normalize_license_url(raw_url: str, norm_license: str) -> str:
    u = strip_html(raw_url)
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if u.startswith("https://"):
        return u
    low = norm_license.lower()
    if "cc0" in low:
        return "https://creativecommons.org/publicdomain/zero/1.0/"
    if "public" in low or low == "pd":
        return "https://creativecommons.org/publicdomain/mark/1.0/"
    if "gfdl" in low:
        return "https://www.gnu.org/licenses/fdl-1.3.html"
    if "by-sa" in low:
        m = re.search(r"(\d\.\d)", low)
        ver = m.group(1) if m else "4.0"
        return f"https://creativecommons.org/licenses/by-sa/{ver}/"
    if "by" in low:
        m = re.search(r"(\d\.\d)", low)
        ver = m.group(1) if m else "4.0"
        return f"https://creativecommons.org/licenses/by/{ver}/"
    return "https://commons.wikimedia.org/wiki/Commons:Licensing"


def normalize_page_url(raw_page: str | None, file_title: str) -> str:
    u = (raw_page or "").strip()
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    if u.startswith("https://"):
        return u
    clean_file = normalize_file_title(file_title).replace(" ", "_")
    return "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(clean_file, safe=":/()_-.")


def normalize_artist(raw_artist: str | None) -> str:
    a = strip_html(raw_artist)
    if not a or "<script" in a.lower():
        return "Wikimedia Commons"
    return a[:80].strip() or "Wikimedia Commons"


def is_tour_media_valid(tid: str, credits: dict[str, Any]) -> bool:
    img_path = IMG_DIR / f"{tid}.jpg"
    if not img_path.exists() or img_path.stat().st_size <= 5000:
        return False
    head = img_path.read_bytes()[:64].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<svg")):
        return False
    try:
        with Image.open(img_path) as im:
            if im.format != "JPEG" or im.size != (800, 450):
                return False
    except Exception:
        return False
    c = credits.get(tid)
    if not isinstance(c, dict):
        return False
    for k in ("file", "artist", "license", "licenseUrl", "page"):
        if not str(c.get(k, "")).strip():
            return False
    if not FREE_LICENSES_RE.match(str(c["license"]).strip()):
        return False
    if not str(c["licenseUrl"]).startswith("https://"):
        return False
    if not str(c["page"]).startswith("https://"):
        return False
    if "<script" in str(c["artist"]).lower():
        return False
    return True


class PoliteClient:
    def __init__(self, min_delay_s: float = MIN_DELAY_S) -> None:
        self.min_delay_s = min_delay_s
        self.last_req_ts = 0.0
        self.network_calls = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _wait_slot(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_req_ts
        if self.last_req_ts == 0.0:
            time.sleep(self.min_delay_s)
        elif elapsed < self.min_delay_s:
            time.sleep(self.min_delay_s - elapsed)

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 40) -> requests.Response:
        for attempt in range(6):
            self._wait_slot()
            try:
                self.last_req_ts = time.monotonic()
                self.network_calls += 1
                resp = self.session.get(url, params=params, timeout=timeout)
                if resp.status_code in (429, 503):
                    ra = resp.headers.get("Retry-After")
                    try:
                        ra_s = float(ra) if ra else 0.0
                    except ValueError:
                        ra_s = 0.0
                    wait_s = max(ra_s, 15.0 * (attempt + 1))
                    print(f"  [HTTP {resp.status_code}] backing off {wait_s:.1f}s (attempt {attempt + 1}/6)...")
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                if attempt < 5:
                    wait_s = 15.0 * (attempt + 1)
                    print(f"  [NET ERR] {exc}; sleeping {wait_s:.1f}s (attempt {attempt + 1}/6)...")
                    time.sleep(wait_s)
                    continue
                raise
        raise RuntimeError(f"Failed request after 6 attempts: {url}")

    def get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.get(url, params=params).json()


def build_full_catalog() -> dict[str, Any]:
    assert M1_CATALOG_PATH.exists(), f"Missing M1 catalog: {M1_CATALOG_PATH}"
    m1 = json.loads(M1_CATALOG_PATH.read_text(encoding="utf-8"))
    m1_tours = list(m1["tours"])
    assert len(m1_tours) == 118, f"Expected 118 M1 tours (117 + Edenkoben–Rietburgbahn–Weyher chain), got {len(m1_tours)}"
    assert 20 <= len(CURATED_TOURS) <= 30, f"Expected 20..30 curated tours, got {len(CURATED_TOURS)}"

    m1_ids = {t["id"] for t in m1_tours}
    cur_ids = {t["id"] for t in CURATED_TOURS}
    assert len(cur_ids) == len(CURATED_TOURS), "Duplicate ID inside CURATED_TOURS"
    assert not (m1_ids & cur_ids), f"ID collision with M1: {m1_ids & cur_ids}"
    for ct in CURATED_TOURS:
        assert str(ct.get("wikimediaQuery", "")).strip(), f"Missing wikimediaQuery on {ct['id']}"

    full_catalog = {
        "generated": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds"),
        "waypoints": m1["waypoints"],
        "excludedGpx": m1["excludedGpx"],
        "tours": m1_tours + CURATED_TOURS,
    }
    FULL_CATALOG_PATH.write_text(
        json.dumps(full_catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return full_catalog


def extract_tour_queries(tour: dict[str, Any]) -> tuple[str, str, str, tuple[float, float]]:
    tid = tour["id"]
    if tid in MEDIA_QUERY_MAP:
        return MEDIA_QUERY_MAP[tid]

    wq = str(tour.get("wikimediaQuery", "")).strip()
    wp_clean = re.sub(r"\s*filetype:\S+", "", wq, flags=re.IGNORECASE).strip()
    places = tour.get("places") or []
    tr = tour.get("transit") or {}
    ss = tr.get("startStop") or {}
    es = tr.get("endStop") or {}

    fallback_wp = ""
    for p in places:
        if p and p not in (ss.get("name"), es.get("name")):
            fallback_wp = re.sub(r"\s*\([^)]*\)", "", str(p)).strip()
            break
    if not fallback_wp:
        fallback_wp = str(tour.get("title", {}).get("de", "")).split(":")[0].strip()

    commons_search = CURATED_COMMONS_OVERRIDES.get(tid) or (wq if wq else fallback_wp)
    coord_stop = es if es.get("lat") is not None else ss
    rep_coords = (float(coord_stop["lat"]), float(coord_stop["lon"]))
    return (wp_clean, fallback_wp, commons_search, rep_coords)


def wp_cache_path(title: str) -> Path:
    return CACHE_DIR / f"wp_{sha1_hex(title)}.json"


def load_wp_cache(title: str) -> dict[str, Any] | None:
    p = wp_cache_path(title)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_wp_cache(title: str, data: dict[str, Any]) -> None:
    wp_cache_path(title).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_wp_batch(client: PoliteClient, titles: list[str]) -> None:
    uncached = [t for t in dict.fromkeys(titles) if t and load_wp_cache(t) is None]
    if not uncached:
        return

    need_images: list[str] = []
    cached_drafts: dict[str, dict[str, Any]] = {}

    for i in range(0, len(uncached), 40):
        batch = uncached[i : i + 40]
        print(f"[Stage 1a] de.wikipedia pageimages batch ({len(batch)} titles)...")
        data = client.get_json(
            DE_WP_API,
            {
                "action": "query",
                "prop": "pageimages",
                "piprop": "name",
                "pilimit": "max",
                "redirects": 1,
                "format": "json",
                "titles": "|".join(batch),
            },
        )
        q = data.get("query", {})
        norm_map = {n["from"]: n["to"] for n in q.get("normalized", [])}
        redir_map = {r["from"]: r["to"] for r in q.get("redirects", [])}
        pages_by_title = {
            p["title"]: p for p in q.get("pages", {}).values() if "missing" not in p
        }

        for orig in batch:
            cur = norm_map.get(orig, orig)
            seen: set[str] = set()
            while cur in redir_map and cur not in seen:
                seen.add(cur)
                cur = redir_map[cur]
            page = pages_by_title.get(cur)
            raw_pi = page.get("pageimage") if page else None
            pi_title = normalize_file_title(raw_pi) if raw_pi else None
            good_pi = pi_title if (pi_title and good_name(pi_title)) else None
            draft = {
                "title": orig,
                "resolved": cur if page else None,
                "pageimage": good_pi,
                "raw_pageimage": pi_title,
                "images": [],
            }
            cached_drafts[orig] = draft
            if page and good_pi is None:
                need_images.append(orig)
            elif page is None:
                save_wp_cache(orig, draft)

    for i in range(0, len(need_images), 5):
        batch = need_images[i : i + 5]
        print(f"[Stage 1b] de.wikipedia prop=images batch ({len(batch)} titles)...")
        collected: dict[str, list[str]] = {t: [] for t in batch}
        cont: dict[str, Any] = {}
        for _ in range(2):
            params = {
                "action": "query",
                "prop": "images",
                "imlimit": "max",
                "redirects": 1,
                "format": "json",
                "titles": "|".join(batch),
                **cont,
            }
            data = client.get_json(DE_WP_API, params)
            q = data.get("query", {})
            norm_map = {n["from"]: n["to"] for n in q.get("normalized", [])}
            redir_map = {r["from"]: r["to"] for r in q.get("redirects", [])}
            pages_by_title = {
                p["title"]: p for p in q.get("pages", {}).values() if "missing" not in p
            }
            for orig in batch:
                cur = norm_map.get(orig, orig)
                seen = set()
                while cur in redir_map and cur not in seen:
                    seen.add(cur)
                    cur = redir_map[cur]
                page = pages_by_title.get(cur)
                if page:
                    for im in page.get("images") or []:
                        nt = normalize_file_title(im.get("title", ""))
                        if good_name(nt) and nt not in collected[orig]:
                            collected[orig].append(nt)
            if "continue" in data and "imcontinue" in data["continue"]:
                cont = {"imcontinue": data["continue"]["imcontinue"]}
            else:
                break
        for orig in batch:
            draft = cached_drafts[orig]
            draft["images"] = collected[orig]
            save_wp_cache(orig, draft)

    for orig, draft in cached_drafts.items():
        if not wp_cache_path(orig).exists():
            save_wp_cache(orig, draft)


def commons_search(client: PoliteClient, query: str) -> list[str]:
    q_norm = query.strip()
    if "filetype:" not in q_norm.lower():
        q_norm = f"{q_norm} filetype:bitmap"
    cpath = CACHE_DIR / f"csearch_{sha1_hex(q_norm)}.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))
    print(f"[Stage 2] Commons search: {q_norm!r}...")
    data = client.get_json(
        COMMONS_API,
        {
            "action": "query",
            "list": "search",
            "srnamespace": 6,
            "srsearch": q_norm,
            "srlimit": 10,
            "format": "json",
        },
    )
    out: list[str] = []
    for hit in data.get("query", {}).get("search", []):
        nt = normalize_file_title(hit.get("title", ""))
        if good_name(nt) and nt not in out:
            out.append(nt)
    cpath.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def commons_geosearch(client: PoliteClient, lat: float, lon: float, radius: int = 5000) -> list[str]:
    cpath = CACHE_DIR / f"cgeo_{lat:.4f}_{lon:.4f}_{radius}.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))
    print(f"[Stage 3] Commons geosearch: {lat:.4f},{lon:.4f} r={radius}m...")
    data = client.get_json(
        COMMONS_API,
        {
            "action": "query",
            "list": "geosearch",
            "gsnamespace": 6,
            "gscoord": f"{lat:.5f}|{lon:.5f}",
            "gsradius": radius,
            "gslimit": 12,
            "format": "json",
        },
    )
    out: list[str] = []
    for hit in data.get("query", {}).get("geosearch", []):
        nt = normalize_file_title(hit.get("title", ""))
        if good_name(nt) and nt not in out:
            out.append(nt)
    cpath.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def fileinfo_cache_path(file_title: str) -> Path:
    return CACHE_DIR / f"fileinfo_{sha1_hex(normalize_file_title(file_title))}.json"


def load_fileinfo_cache(file_title: str) -> dict[str, Any] | None:
    p = fileinfo_cache_path(file_title)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def resolve_fileinfo_batch(client: PoliteClient, file_titles: list[str]) -> None:
    uncached = [t for t in dict.fromkeys(normalize_file_title(x) for x in file_titles) if load_fileinfo_cache(t) is None]
    if not uncached:
        return
    for i in range(0, len(uncached), 25):
        batch = uncached[i : i + 25]
        print(f"[Stage 4a] Commons imageinfo batch ({len(batch)} files)...")
        data = client.get_json(
            COMMONS_API,
            {
                "action": "query",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|size|mime",
                "iiurlwidth": 1200,
                "format": "json",
                "titles": "|".join(batch),
            },
        )
        q = data.get("query", {})
        norm_map = {n["from"]: n["to"] for n in q.get("normalized", [])}
        pages_by_title = {
            p["title"]: p for p in q.get("pages", {}).values() if "missing" not in p
        }
        for orig in batch:
            cur = norm_map.get(orig, orig)
            page = pages_by_title.get(cur)
            ii = (page.get("imageinfo") or [None])[0] if page else None
            payload = ii if ii is not None else {"missing": True}
            fileinfo_cache_path(orig).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


def gather_candidates_for_tour(
    client: PoliteClient,
    tour: dict[str, Any],
    force_broad: bool = False,
) -> tuple[list[tuple[str, bool, str]], str]:
    """Return ([(file_title, is_pageimage, article_label), ...], primary_article)."""
    wp1, wp2, csearch, (lat, lon) = extract_tour_queries(tour)
    cands: list[tuple[str, bool, str]] = []
    seen: set[str] = set()

    def add_cand(ft: str, is_pi: bool, src_label: str) -> None:
        nft = normalize_file_title(ft)
        if nft not in seen and good_name(nft):
            seen.add(nft)
            cands.append((nft, is_pi, src_label))

    for wp_title in (wp1, wp2):
        if not wp_title:
            continue
        cached = load_wp_cache(wp_title)
        if cached:
            if cached.get("pageimage"):
                add_cand(cached["pageimage"], True, f"de:{wp_title}")
            for im in cached.get("images") or []:
                add_cand(im, False, f"de:{wp_title}")

    if len(cands) < 2 or force_broad:
        for ft in commons_search(client, csearch):
            add_cand(ft, False, f"commons_search:{csearch}")

    if len(cands) < 2 or force_broad:
        geo = commons_geosearch(client, lat, lon, 5000)
        if len(geo) < 2:
            geo = geo + commons_geosearch(client, lat, lon, 12000)
        for ft in geo:
            add_cand(ft, False, f"commons_geo:{lat:.4f},{lon:.4f}")

    if force_broad and len(cands) < 2:
        # Broad region + place search fallback
        place_kw = str((tour.get("places") or [tour["region"]])[0])
        broad_q = f"{place_kw} {tour['region']}"
        for ft in commons_search(client, broad_q):
            add_cand(ft, False, f"commons_search:{broad_q}")

    return cands, (f"de:{wp1}" if wp1 else f"commons:{csearch}")


def validate_and_score_candidate(
    file_title: str,
    is_pi: bool,
    use_counts: dict[str, int],
) -> tuple[int, dict[str, Any]] | None:
    ii = load_fileinfo_cache(file_title)
    if not ii or ii.get("missing"):
        return None
    mime = str(ii.get("mime", "")).lower()
    if mime != "image/jpeg":
        return None
    w = int(ii.get("width") or 0)
    h = int(ii.get("height") or 0)
    if w < 600 or h < 350:
        return None

    em = ii.get("extmetadata") or {}
    raw_lic = (em.get("LicenseShortName") or {}).get("value", "")
    norm_lic = normalize_license(raw_lic)
    if not norm_lic:
        return None

    lic_url = normalize_license_url((em.get("LicenseUrl") or {}).get("value", ""), norm_lic)
    page_url = normalize_page_url(ii.get("descriptionurl"), file_title)
    artist = normalize_artist((em.get("Artist") or {}).get("value"))
    thumb_url = ii.get("thumburl") or ii.get("url")
    if not thumb_url:
        return None

    score = 0
    if is_pi:
        score += 100
    ratio = w / float(h)
    if ratio >= 1.2:
        score += 40
    elif w >= h:
        score += 15
    if w >= 1200:
        score += 10
    score -= 30 * use_counts.get(file_title, 0)

    return score, {
        "file": file_title,
        "artist": artist,
        "license": norm_lic,
        "licenseUrl": lic_url,
        "page": page_url,
        "thumb_url": thumb_url,
    }


def download_and_crop(client: PoliteClient, tid: str, file_title: str, thumb_url: str) -> None:
    orig_path = ORIGINALS_DIR / f"{sha1_hex(file_title)}.jpg"
    if not orig_path.exists() or orig_path.stat().st_size <= 5000:
        urls_to_try = []
        if "/thumb/" in thumb_url and re.search(r"/\d+px-", thumb_url):
            urls_to_try.append(re.sub(r"/\d+px-", "/960px-", thumb_url))
            urls_to_try.append(re.sub(r"/\d+px-", "/1024px-", thumb_url))
        urls_to_try.append(thumb_url)
        raw = None
        last_exc = None
        for u in dict.fromkeys(urls_to_try):
            try:
                client._wait_slot()
                client.last_req_ts = time.monotonic()
                client.network_calls += 1
                r = client.session.get(u, timeout=20)
                if r.status_code == 200 and len(r.content) > 5000:
                    with Image.open(BytesIO(r.content)) as test_im:
                        test_im.verify()
                    raw = r.content
                    break
                last_exc = RuntimeError(f"HTTP {r.status_code} for {u}")
            except Exception as e:
                last_exc = e
        if raw is None:
            raise last_exc or RuntimeError(f"Failed download for {file_title}")
        orig_path.write_bytes(raw)

    raw_bytes = orig_path.read_bytes()
    (IMG_SRC_DIR / f"{tid}.jpg").write_bytes(raw_bytes)

    with Image.open(BytesIO(raw_bytes)) as src_im:
        im = src_im.convert("RGB")
        w, h = im.size
        target_ratio = 16.0 / 9.0
        current_ratio = w / float(h)
        if current_ratio > target_ratio:
            new_w = int(round(h * target_ratio))
            left = max(0, (w - new_w) // 2)
            im = im.crop((left, 0, left + new_w, h))
        else:
            new_h = int(round(w / target_ratio))
            top = max(0, (h - new_h) // 2)
            im = im.crop((0, top, w, top + new_h))
        im = im.resize((800, 450), Image.Resampling.LANCZOS)
        out_path = IMG_DIR / f"{tid}.jpg"
        im.save(out_path, "JPEG", quality=85)

    assert out_path.stat().st_size > 5000, f"Output JPEG too small for {tid}: {out_path.stat().st_size} B"
    with Image.open(out_path) as chk:
        assert chk.format == "JPEG" and chk.size == (800, 450)


def run_media_pipeline(catalog: dict[str, Any], force: bool = False, limit: int | None = None) -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    IMG_SRC_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)

    credits: dict[str, Any] = {}
    if CREDITS_PATH.exists():
        credits = json.loads(CREDITS_PATH.read_text(encoding="utf-8"))

    tours = catalog["tours"]
    if limit is not None:
        tours = tours[:limit]

    pending: list[dict[str, Any]] = []
    use_counts: dict[str, int] = {}
    for t in catalog["tours"]:
        tid = t["id"]
        if tid in credits and isinstance(credits[tid], dict) and credits[tid].get("file"):
            f = normalize_file_title(credits[tid]["file"])
            use_counts[f] = use_counts.get(f, 0) + 1

    for t in tours:
        tid = t["id"]
        if not force and is_tour_media_valid(tid, credits):
            continue
        pending.append(t)

    if not pending:
        print(f"[Media] All {len(tours)} tours already have valid 800x450 JPEGs & credits (0 network calls).")
        return

    print(f"[Media] Resolving Wikimedia media for {len(pending)}/{len(tours)} pending tours...")
    client = PoliteClient()

    # Stage 1: Batch de.wikipedia pageimages + prop=images
    wp_titles: list[str] = []
    for t in pending:
        wp1, wp2, _, _ = extract_tour_queries(t)
        if wp1:
            wp_titles.append(wp1)
        if wp2:
            wp_titles.append(wp2)
    resolve_wp_batch(client, wp_titles)

    # Stage 2 & 3: Gather candidates (running Commons search/geosearch when <2 WP candidates)
    tour_cands: dict[str, tuple[list[tuple[str, bool, str]], str]] = {}
    all_files: list[str] = []
    for t in pending:
        cands, primary_art = gather_candidates_for_tour(client, t, force_broad=False)
        tour_cands[t["id"]] = (cands, primary_art)
        for ft, _, _ in cands[:10]:
            all_files.append(ft)

    # Stage 4a: Batch Commons imageinfo
    resolve_fileinfo_batch(client, all_files)

    # Second pass for any tour whose Stage 1-3 candidates all failed imageinfo/license/size checks
    still_needed_files: list[str] = []
    for t in pending:
        tid = t["id"]
        cands, _ = tour_cands[tid]
        valid_any = any(validate_and_score_candidate(ft, is_pi, use_counts) is not None for ft, is_pi, _ in cands[:10])
        if not valid_any:
            cands, primary_art = gather_candidates_for_tour(client, t, force_broad=True)
            tour_cands[tid] = (cands, primary_art)
            for ft, _, _ in cands[:12]:
                still_needed_files.append(ft)
    if still_needed_files:
        resolve_fileinfo_batch(client, still_needed_files)

    # Stage 4b: Score, pick best, download, crop, and save credits
    for idx, t in enumerate(pending, 1):
        tid = t["id"]
        cands, primary_art = tour_cands[tid]
        scored: list[tuple[int, str, str, dict[str, Any]]] = []
        for ft, is_pi, src_label in cands:
            res = validate_and_score_candidate(ft, is_pi, use_counts)
            if res is not None:
                sc, meta = res
                scored.append((sc, src_label, ft, meta))
        scored.sort(key=lambda x: x[0], reverse=True)

        chosen = None
        last_err: Exception | None = None
        for sc, src_label, ft, meta in scored:
            try:
                download_and_crop(client, tid, ft, meta["thumb_url"])
                chosen = (sc, src_label, ft, meta)
                break
            except Exception as exc:
                last_err = exc
                print(f"  [WARN] {tid}: candidate {ft} failed ({exc}), trying next...")

        if chosen is None:
            # Fallback to any already-downloaded local original in ORIGINALS_DIR with valid cached fileinfo
            local_scored: list[tuple[int, str, str, dict[str, Any]]] = []
            for p_fi in CACHE_DIR.glob("fileinfo_*.json"):
                try:
                    fi_d = json.loads(p_fi.read_text(encoding="utf-8"))
                except Exception:
                    continue
                desc_u = str(fi_d.get("descriptionurl") or "")
                m_ft = re.search(r"/wiki/(File:[^?#]+)", urllib.parse.unquote(desc_u))
                if not m_ft:
                    continue
                ft_cand = normalize_file_title(m_ft.group(1))
                orig_cand = ORIGINALS_DIR / f"{sha1_hex(ft_cand)}.jpg"
                if orig_cand.exists() and orig_cand.stat().st_size > 5000:
                    res = validate_and_score_candidate(ft_cand, False, use_counts)
                    if res is not None:
                        sc, meta = res
                        local_scored.append((sc, primary_art, ft_cand, meta))
            local_scored.sort(key=lambda x: x[0], reverse=True)
            for sc, src_label, ft, meta in local_scored:
                try:
                    download_and_crop(client, tid, ft, meta["thumb_url"])
                    chosen = (sc, src_label, ft, meta)
                    break
                except Exception:
                    continue
        if chosen is None:
            raise RuntimeError(f"Could not resolve valid Wikimedia JPEG for tour {tid} (last_err={last_err})")

        _, src_label, ft, meta = chosen
        use_counts[ft] = use_counts.get(ft, 0) + 1
        credits[tid] = {
            "file": ft,
            "article": src_label or primary_art,
            "artist": meta["artist"],
            "license": meta["license"],
            "licenseUrl": meta["licenseUrl"],
            "page": meta["page"],
            "w": 800,
            "h": 450,
        }
        print(f"  [{idx}/{len(pending)}] {tid} -> {ft} ({meta['license']}, {meta['artist']})")

        if idx % 10 == 0:
            CREDITS_PATH.write_text(json.dumps(credits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    CREDITS_PATH.write_text(json.dumps(credits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[Media] Completed {len(pending)} tours with {client.network_calls} live HTTP calls. Total credits: {len(credits)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge M2 curated tours and fetch Wikimedia Commons media.")
    parser.add_argument("--force", action="store_true", help="Re-process/re-crop even if img/<id>.jpg & credits are valid")
    parser.add_argument("--catalog-only", action="store_true", help="Only build data/tours_catalog_full.json without media fetch")
    parser.add_argument("--limit", type=int, default=None, help="Optional tour limit for incremental testing")
    args = parser.parse_args()

    catalog = build_full_catalog()
    print(f"[Catalog] Wrote {FULL_CATALOG_PATH} with {len(catalog['tours'])} tours ({len(CURATED_TOURS)} curated).")
    if not args.catalog_only:
        run_media_pipeline(catalog, force=args.force, limit=args.limit)


if __name__ == "__main__":
    main()
