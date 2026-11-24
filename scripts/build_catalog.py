#!/usr/bin/env python3
"""M1 Catalog Builder for Südwest-Touren (Jugenheim & Karlsruhe).

Processes:
- data/phone_shortcuts.json + data/android_shortcuts_raw.txt (85 shortcuts)
- data/mapy/*.gpx (68 GPX files -> 19 waypoints, 64 unique tracks, 6 chains)
- Transitous /api/v1/map/stops, /api/v1/geocode & VRN-EFA XML_COORD_REQUEST
  (cached persistently in cache/stops_lookup/ with >= 1.15s delay on misses)

Outputs:
- data/excluded_shortcuts.json
- data/tours_catalog.json
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3.util.connection
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force IPv4 so api.transitous.org never stalls on IPv6 SYN timeouts.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MAPY_DIR = DATA_DIR / "mapy"
CACHE_DIR = BASE_DIR / "cache" / "stops_lookup"

USER_AGENT = "suedwest-touren-planner/1.0 (oliver.simon@stud.h-da.de)"
MIN_REQUEST_DELAY_S = 1.15

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}
REAL_TRANSIT_MODES = {"BUS", "TRAM", "SUBURBAN", "REGIONAL_RAIL", "METRO", "FERRY", "ODM"}
REGULAR_MODES = {"BUS", "TRAM", "SUBURBAN", "REGIONAL_RAIL", "METRO", "FERRY"}
DHID_RE = re.compile(r"(de:\d+:\d+)")

JUGENHEIM_HOME = {
    "name": "Jugenheim Bickenbacher Straße, Seeheim-Jugenheim",
    "type": "STATION",
    "lat": 49.753891,
    "lon": 8.629438,
    "stationId": "124355",
    "dhid": "de:06432:24355",
    "ibnr": None,
    "remoteId": "A=1@O=Jugenheim Bickenbacher Straße, Seeheim-Jugenheim@X=8629438@Y=49753891@U=80@L=124355@",
    "transitousId": "de-DELFI_de:06432:24355:2:2",
}

SHORTCUT_BLOCK_RE = re.compile(
    r"ShortcutInfo \{id=([^,\n]+),\s*flags=([^\n]+)\n"
    r"\s+packageName=(de\.hafas\.android\.\w+)\n"
    r"(.*?)"
    r"(?=\n\s+ShortcutInfo \{id=|\n\s+Total bitmap size:)",
    re.DOTALL,
)
REQUESTDATA_RE = re.compile(
    r"de\.hafas\.android\.requestdata=(.*?)\n\s*\}\]\]",
    re.DOTALL,
)

EXCLUDED_SPECS: dict[int, tuple[str, str]] = {
    7: (
        "commute_university",
        "Campus-Pendelweg zur Hochschule Darmstadt (Campus Dieburg), Ankunft 14:15 / University commute to h_da Dieburg campus",
    ),
    9: (
        "commute_university",
        "Rückfahrt-Pendelweg vom Campus Dieburg (Hochschule Süd) nach Jugenheim / Return commute from h_da Dieburg campus",
    ),
    10: (
        "commute_university",
        "Borderline → EXCLUDE: Ziel ist Campus Hochschule Süd, Dieburg (bdepart=0, 14:15); Ausflugsroute Eberbach–Miltenberg ist über #19 und #67+#59 abgedeckt / Target is h_da Dieburg campus commute",
    ),
    17: (
        "base_to_base_transfer",
        "Borderline → EXCLUDE: Basis-zu-Basis-Transfer ab Karlsruhe Durlacher Tor/KIT über Blankenloch/Bruchsal nach Jugenheim ohne Wander- oder Ausflugsziel / Base-to-base transfer without excursion target",
    ),
    20: (
        "local_utility_route",
        "Borderline → EXCLUDE: Lokale Alltags-/Nachbarschaftsbuslinie 676 (Bensheim-Auerbach → Schwanheim → Jugenheim, bdepart=0 09:53) ohne Ausflugsziel / Local utility bus commute",
    ),
    23: (
        "utility_city_hop",
        "Innerstädtische Kurzverbindung in Stuttgart (Marienplatz → Stuttgart Hbf, explizit laut R1 auszuschließen) / Inner-city Stuttgart hop",
    ),
    25: (
        "personal_errand_waiblingen",
        "Alltags-/Privatfahrt ab Schwanen, Waiblingen (productsField=11111111111111 inkl. Fernverkehr, explizit laut R1 auszuschließen) / Personal errand from Waiblingen",
    ),
    52: (
        "personal_errand_waiblingen",
        "Alltags-/Privatfahrt nach Rathaus, Waiblingen (Gegenrichtung zu #25, explizit laut R1 auszuschließen) / Personal errand to Waiblingen",
    ),
    53: (
        "commute_university",
        "Campus-/Alltagsverbindung ab Stuttgart Universität → Pforzheim Hbf (explizit laut R1 auszuschließen) / University commute from Stuttgart",
    ),
    54: (
        "base_to_base_transfer",
        "Borderline → EXCLUDE: Direkter Basis-zu-Basis-Transfer Jugenheim ↔ Karlsruhe Hbf ohne Zwischenziele / Direct base-to-base transfer",
    ),
    63: (
        "app_utility_shortcut",
        "App-Systemverknüpfung 'Buy tickets' (id=productShortcut) ohne Orte/Parameter (explizit laut R1 auszuschließen) / App ticket shortcut",
    ),
    64: (
        "commute_karlsruhe",
        "Pendel-/Alltagsweg zur Moltkestraße 30 / HAK Karlsruhe (explizit laut R1 auszuschließen) / Commute to Moltkestraße 30 / HAK Karlsruhe",
    ),
    85: (
        "personal_errand_clay_kaserne",
        "Dienst-/Alltagsweg zur Clay Kaserne Wiesbaden via Mainz Bismarckplatz (explizit laut R1 auszuschließen) / Commute to Clay Kaserne",
    ),
}

# GPX Track Metadata Table (Explorer 2 §4.5 + Explorer 3 §4.3)
# Maps canonical GPX filename -> (region, de_title, en_title, category, evening_sun, subtitle_de, subtitle_en)
GPX_META: dict[str, tuple[str, str, str, str, bool, str, str]] = {
    "Route from Abzweig Korsika_Ludwigsdorf to Eiterbach_ Ortsende.gpx": (
        "odenwald", "Ludwigsdorf (Korsika) → Eiterbach", "Ludwigsdorf (Korsika) to Eiterbach",
        "wanderung", False, "Odenwald-Waldweg von Ludwigsdorf nach Eiterbach", "Odenwald forest hike from Ludwigsdorf to Eiterbach",
    ),
    "Route from Ahlmühle to Scenic viewpoint.gpx": (
        "pfaelzerwald", "Ahlmühle → Aussichtspunkt Schuhmacherfelsen", "Ahlmühle to Schuhmacherfelsen Viewpoint",
        "berg", True, "Aussichtsaufstieg im Wasgau zum Schuhmacherfelsen", "Wasgau ridge ascent to Schuhmacherfelsen viewpoint",
    ),
    "Route from Annweiler_ Burg Trifels to Leinsweiler_ Ort.gpx": (
        "pfaelzerwald", "Burg Trifels → Leinsweiler", "Trifels Castle to Leinsweiler",
        "burg", True, "Von der Reichsburg Trifels über Felsenpfade nach Leinsweiler", "From Trifels Castle across sandstone trails to Leinsweiler",
    ),
    "Route from Bad Bergzabern_ Bahnhof to Bad Bergzabern_ Bahnhof.gpx": (
        "pfaelzerwald", "Bad Bergzabern Kurpark-Runde", "Bad Bergzabern Spa & Town Walk",
        "stadt", False, "Entspannte Kurpark- und Altstadtrunde in Bad Bergzabern", "Relaxed spa park and old town walk in Bad Bergzabern",
    ),
    "Route from Bad Dürkheim_ Sonnenwende to Bad Dürkheim_ Vigilienstraße.gpx": (
        "pfaelzerwald", "Bad Dürkheim Sonnenwende → Vigilienstraße", "Bad Dürkheim Sonnenwende to Vigilienstraße",
        "wein", True, "Abstieg vom Haardtrand durch Weinberge nach Bad Dürkheim", "Descent from the Haardt edge through vineyards into Bad Dürkheim",
    ),
    "Route from Bad Herrenalb to Bad Herrenalb Kullenmühle.gpx": (
        "schwarzwald", "Bad Herrenalb → Kullenmühle", "Bad Herrenalb to Kullenmühle",
        "wanderung", False, "Kurzer Talweg entlang der Alb nach Kullenmühle", "Short valley walk along the Alb river to Kullenmühle",
    ),
    "Route from Betzenberg_ Spinozastraße to Mölschbach_ Weiherfelderhof.gpx": (
        "pfaelzerwald", "Kaiserslautern Betzenberg → Mölschbach", "Kaiserslautern Betzenberg to Mölschbach",
        "wanderung", False, "Walddurchquerung vom Betzenberg ins Pfälzerwald-Dorf Mölschbach", "Forest crossing from Betzenberg to Mölschbach",
    ),
    "Route from Boppard Buchholz (Hunsr_) Bf to Vierseenblicklift.gpx": (
        "weitere", "Boppard Buchholz → Vierseenblick (Mittelrhein)", "Boppard Buchholz to Vierseenblick (Middle Rhine)",
        "panoramatour", True, "Traumpfad-Abstieg zum berühmten Vierseenblick über der Rheinschleife", "Panoramic descent to the famous Vierseenblick over the Rhine bend",
    ),
    "Route from Buchklingen to Weinheim_ Schlossbergterrasse.gpx": (
        "odenwald", "Buchklingen → Weinheim Schlossbergterrasse", "Buchklingen to Weinheim Castle Terrace",
        "burg", True, "Über die Bergstraße hinab zur Weinheimer Schlossbergterrasse", "Descent along the Bergstraße to Weinheim Castle Terrace",
    ),
    "Route from Cafe am Markt to Scenic viewpoint.gpx": (
        "weitere", "Idar-Oberstein Markt → Felsenkirche & Schloss", "Idar-Oberstein Market to Felsenkirche Viewpoint",
        "burg", True, "Steiler Aufstieg vorbei an der Felsenkirche zum Schloss Oberstein", "Steep climb past the Felsenkirche to Oberstein Castle",
    ),
    "Route from Dimbach_ Ort to Darstein_ Ortsmitte.gpx": (
        "pfaelzerwald", "Dimbach → Darstein (Wasgau-Felsenweg)", "Dimbach to Darstein (Wasgau Cliffs)",
        "berg", True, "Klassischer Dimbacher Buntsandstein-Höhenweg nach Darstein", "Classic Dimbach red sandstone ridge trail to Darstein",
    ),
    "Route from Dossenheim Bahnhof to Schriesheim_ Rathaus.gpx": (
        "odenwald", "Dossenheim → Strahlenburg → Schriesheim", "Dossenheim via Strahlenburg to Schriesheim",
        "burg", True, "Bergstraßen-Weinbergs- und Burgwanderung zur Strahlenburg", "Bergstraße vineyard and castle hike via Strahlenburg",
    ),
    "Route from Drei Buchen to Burg Meistersel.gpx": (
        "pfaelzerwald", "Drei Buchen → Burg Meistersel", "Drei Buchen to Meistersel Castle",
        "burg", False, "Kurzer Waldaufstieg zur historischen Ruine Burg Meistersel", "Short forest climb to the ruins of Meistersel Castle",
    ),
    "Route from Drei Buchen to Scenic viewpoint.gpx": (
        "pfaelzerwald", "Drei Buchen → Orensfelsen", "Drei Buchen to Orensfelsen Viewpoint",
        "berg", True, "Kammwanderung zum spektakulären Aussichtsplateau Orensfelsen", "Ridge hike to the spectacular Orensfelsen rock plateau",
    ),
    "Route from Drei Eichen to Guttenberg Castle.gpx": (
        "pfaelzerwald", "Drei Eichen → Burg Guttenberg", "Drei Eichen to Guttenberg Castle",
        "burg", False, "Grenzwald-Pfad zur hochgelegenen Ruine Burg Guttenberg", "Borderland forest path to the Guttenberg Castle ruins",
    ),
    "Route from Ebersteinburg Kapelle to Ebersteinburg Kapelle.gpx": (
        "schwarzwald", "Ebersteinburg → Battertfelsen & Altes Schloss (Westrunde)", "Ebersteinburg to Battert Cliffs & Old Castle (West Loop)",
        "burg", True, "Felsensteig am Battertfelsen mit Altem Schloss Hohenbaden", "Cliff trail along Battert Rocks and Hohenbaden Old Castle",
    ),
    "Route from Edenkoben_ Abzw_ Ludwigshöhe to Rietburg Chairlift.gpx": (
        "pfaelzerwald", "Edenkoben Ludwigshöhe → Rietburgbahn", "Edenkoben Ludwigshöhe to Rietburg Chairlift",
        "wein", True, "Weinbergs- und Kastanienweg zur Talstation der Rietburgbahn", "Vineyard and chestnut trail to the Rietburg chairlift valley station",
    ),
    "Route from Erlenbach (Dahn) to Niederschlettenbach.gpx": (
        "pfaelzerwald", "Erlenbach (bei Dahn) → Niederschlettenbach", "Erlenbach (Dahn) to Niederschlettenbach",
        "burg", False, "Dahner Felsenland von Burg Berwartstein ins Lautertal", "Dahner Felsenland past Berwartstein Castle into the Lauter valley",
    ),
    "Route from Handschuhsheim_ Turnerbrunnen to Neuenheim_ Brückenstraße.gpx": (
        "odenwald", "Heidelberg Heiligenberg: Handschuhsheim → Neuenheim", "Heidelberg Heiligenberg: Handschuhsheim to Neuenheim",
        "berg", True, "Über Thingstätte und Philosophenweg über Heidelberg", "Across Thingstätte and Philosopher's Walk above Heidelberg",
    ),
    "Route from Hauenstein_ Ortsmitte to Hauenstein_ Weißenburger Str_.gpx": (
        "pfaelzerwald", "Hauenstein Felsenrunde (Ortsmitte)", "Hauenstein Sandstone Loop",
        "berg", False, "Buntsandstein-Felsentour rund um Hauenstein", "Red sandstone rock tour around Hauenstein",
    ),
    "Route from Hauenstein_ Ortsmitte to Schwanheim_ Ort.gpx": (
        "pfaelzerwald", "Hauenstein → Schwanheim", "Hauenstein to Schwanheim",
        "wanderung", False, "Über die Hauensteiner Felsen und den Dimberg nach Schwanheim", "Across Hauenstein's cliffs and Dimberg to Schwanheim",
    ),
    "Route from Hornisgrinde Turm to Kaltenbrunnen.gpx": (
        "schwarzwald", "Hornisgrinde Turm → Kaltenbrunnen", "Hornisgrinde Tower to Kaltenbrunnen",
        "berg", True, "Vom höchsten Nordschwarzwald-Gipfel hinab nach Kaltenbrunnen", "From the highest Northern Black Forest peak down to Kaltenbrunnen",
    ),
    "Route from Höningen_ Ort to Bad Dürkheim_ Sonnenwende.gpx": (
        "pfaelzerwald", "Höningen → Bismarckturm Peterskopf → Bad Dürkheim", "Höningen via Bismarck Tower to Bad Dürkheim",
        "berg", True, "Durch den Leininger Wald zum Bismarckturm am Peterskopf", "Through the Leiningen forest to the Peterskopf Bismarck Tower",
    ),
    "Route from Jägerwiese to Rothenbuch_ Sportplatz.gpx": (
        "weitere", "Spessart: Jägerwiese → Rothenbuch", "Spessart: Jägerwiese to Rothenbuch",
        "wanderung", False, "Ruhige Spessart-Waldwanderung nach Rothenbuch", "Quiet Spessart forest hike to Rothenbuch",
    ),
    "Route from Kaltenbronn Rankhütte to Gaistal Bernbacher Weg.gpx": (
        "schwarzwald", "Kaltenbronn → Teufelsmühle → Gaistal Bernbach", "Kaltenbronn via Teufelsmühle to Gaistal Bernbach",
        "berg", True, "Hochmoor und Aussichtsturm Teufelsmühle ins Gaistal", "High moorland and Teufelsmühle tower down to Gaistal",
    ),
    "Route from Kaltenbronn Rankhütte to Gaistal Talwiese.gpx": (
        "schwarzwald", "Kaltenbronn → Gaistal Talwiese", "Kaltenbronn to Gaistal Talwiese",
        "wanderung", False, "Vom Hochmoor Kaltenbronn durch das obere Gaistal", "From the Kaltenbronn high moor through upper Gaistal",
    ),
    "Route from Klingenmünster_ Landeckstraße to Klingenmünster_ Pfalzklinikum.gpx": (
        "pfaelzerwald", "Klingenmünster → Burg Landeck & Pfalzklinikum", "Klingenmünster via Landeck Castle",
        "burg", True, "Über Burg Landeck und Waldhöhen bei Klingenmünster", "Via Landeck Castle and forested heights near Klingenmünster",
    ),
    "Route from Ledigsberg to Scenic viewpoint.gpx": (
        "odenwald", "Eberbach Ledigsberg → Neckarblick", "Eberbach Ledigsberg to Neckar Viewpoint",
        "panoramatour", True, "Kurzer Panoramaaufstieg über dem Neckarknie bei Eberbach", "Short panoramic climb above the Neckar bend at Eberbach",
    ),
    "Route from Lindelbrunn to Lindelbrunn Castle.gpx": (
        "pfaelzerwald", "Cramerhaus → Burg Lindelbrunn", "Lindelbrunn Trailhead to Lindelbrunn Castle",
        "burg", True, "Kurzer Anstieg zur Felsburg Lindelbrunn mit 360°-Wasgaublick", "Short climb to Lindelbrunn rock castle with 360° Wasgau views",
    ),
    "Route from Loffenau Marktplatz to Hörden.gpx": (
        "schwarzwald", "Loffenau → Hörden (Murgtal)", "Loffenau to Hörden (Murg Valley)",
        "wanderung", False, "Vom Fachwerkdorf Loffenau hinab ins Murgtal nach Hörden", "From half-timbered Loffenau down into the Murg valley at Hörden",
    ),
    "Route from Ludwigs Tower to Weyher_ Altes Rathaus.gpx": (
        "pfaelzerwald", "Ludwigsturm → Weyher (Altes Rathaus)", "Ludwig's Tower to Weyher Old Town Hall",
        "wein", True, "Vom Blättersberg-Ludwigsturm durch Weinberge nach Weyher", "From Ludwig's Tower through vineyards down to Weyher",
    ),
    "Route from Löhrbach_ Ost to Unter-Abtsteinach_ Schule.gpx": (
        "odenwald", "Löhrbach → Unter-Abtsteinach", "Löhrbach to Unter-Abtsteinach",
        "wanderung", False, "Vorderer Odenwald über Höhenwiesen nach Unter-Abtsteinach", "Front Odenwald ridge meadows to Unter-Abtsteinach",
    ),
    "Route from Nieder-Liebersbach_ Rathaus to Weinheim (Bergstraße).gpx": (
        "odenwald", "Nieder-Liebersbach → Weinheim (Bergstraße)", "Nieder-Liebersbach to Weinheim (Bergstraße)",
        "wanderung", True, "Durch den Gorxheimer Odenwald zum Weinheimer Schloss", "Across Odenwald hills down to Weinheim Castle & station",
    ),
    "Route from Not-Gottes-Kapelle to Melibokus Observation Tower.gpx": (
        "odenwald", "Not-Gottes-Kapelle → Melibokus-Turm", "Not-Gottes Chapel to Melibokus Tower",
        "berg", True, "Auf den höchsten Gipfel der Bergstraße mit Rheinebenenblick", "Climb to the highest peak of the Bergstraße overlooking the Rhine",
    ),
    "Route from Nothweiler to Nothweiler.gpx": (
        "pfaelzerwald", "Nothweiler Rundtour (Weglenburg & Felsland)", "Nothweiler Loop (Weglenburg & Cliffs)",
        "burg", True, "Deutsch-französische Burgenrunde mit der hochgelegenen Weglenburg", "Franco-German castle loop featuring the hilltop Weglenburg",
    ),
    "Route from Oberzent-Kortelshütte Unterdorf to Hirschhorn_ Bahnhof.gpx": (
        "odenwald", "Oberzent-Kortelshütte → Hirschhorn Bf", "Oberzent-Kortelshütte to Hirschhorn Station",
        "wanderung", False, "Waldabstieg durch das Finkenbachtal hinunter ins Neckartal", "Forest descent into the Neckar valley at Hirschhorn",
    ),
    "Route from Oberzent-Rothenberg Sparkasse to Hirschhorn_ Bahnhof.gpx": (
        "odenwald", "Oberzent-Rothenberg → Hirschhorn Bf", "Oberzent-Rothenberg to Hirschhorn Station",
        "wanderung", False, "Vom Höhendorf Rothenberg nach Hirschhorn am Neckar", "From hilltop Rothenberg down to Hirschhorn on the Neckar",
    ),
    "Route from Parking lot to Bismarckwarte.gpx": (
        "odenwald", "Winterkasten → Bismarckwarte Lindenfels", "Winterkasten to Lindenfels Bismarck Tower",
        "berg", True, "Kurzer Kammweg bei Winterkasten zur Bismarckwarte", "Short ridge walk near Winterkasten to the Bismarck Tower",
    ),
    "Route from Parking lot to Parking lot (1).gpx": (
        "schwarzwald", "Herrenwies → Badener Höhe (Friedrichsturm) Rundtour", "Herrenwies to Badener Höhe (Friedrichsturm) Loop",
        "berg", True, "Tausender-Gipfelrunde über die Badener Höhe und den Seekopf", "1,000m summit loop across Badener Höhe and Friedrichsturm",
    ),
    "Route from Parking lot to Parking lot.gpx": (
        "schwarzwald", "Mummelsee → Hornisgrinde Rundtour", "Mummelsee to Hornisgrinde Loop",
        "see", True, "Karsee- und Bohlenweg-Runde über das Hornisgrinde-Hochmoor", "Glacial lake and boardwalk loop across the Hornisgrinde plateau",
    ),
    "Route from Parking lot to Schwarzenbachtalsperre.gpx": (
        "schwarzwald", "Sand → Friedrichsturm → Schwarzenbachtalsperre", "Sand via Friedrichsturm to Schwarzenbach Dam",
        "see", False, "Gipfelüberschreitung über die Badener Höhe zum Stausee", "Summit crossing over Badener Höhe down to Schwarzenbach Reservoir",
    ),
    "Route from Parking lot to Unterstmatt.gpx": (
        "schwarzwald", "Mummelsee → Hornisgrinde → Unterstmatt", "Mummelsee via Hornisgrinde to Unterstmatt",
        "berg", True, "Panorama-Gratweg an der Schwarzwaldhochstraße", "Panoramic ridge trail along the Black Forest High Road",
    ),
    "Route from Parking to Madenburg Castle.gpx": (
        "pfaelzerwald", "Eschbach → Burg Madenburg", "Eschbach to Madenburg Castle",
        "burg", True, "Kurzer Anstieg zur grandiosen Burganlage Madenburg", "Short climb to the grand hilltop Madenburg Castle",
    ),
    "Route from Parking to Orensfelsen.gpx": (
        "pfaelzerwald", "Godramstein/Frankweiler → Orensfelsen", "Godramstein/Frankweiler to Orensfelsen",
        "berg", True, "Langer Weinstraßen-Anstieg zum Orensfelsen-Massiv", "Long climb from the Wine Route up to the Orensfelsen massif",
    ),
    "Route from Parking to Parking.gpx": (
        "odenwald", "Katzenbuckel Gipfelrunde (Waldkatzenbach)", "Katzenbuckel Summit Loop (Waldkatzenbach)",
        "berg", True, "Auf den höchsten Odenwald-Berg (626 m) mit Aussichtsturm", "Loop to the highest peak in the Odenwald (626 m) and tower",
    ),
    "Route from Parking to Scenic viewpoint.gpx": (
        "pfaelzerwald", "Annweiler-Sarnstall → Adelberg-Aussicht", "Annweiler-Sarnstall to Adelberg Viewpoint",
        "panoramatour", True, "Kurze Feierabendrunde auf die Adelberg-Klippe über dem Queichtal", "Short ridge walk to the Adelberg cliff above the Queich valley",
    ),
    "Route from Parking to Schuhmacherfelsen.gpx": (
        "pfaelzerwald", "Ranschbach → Schuhmacherfelsen", "Ranschbach to Schuhmacherfelsen",
        "berg", True, "Steiler Waldanstieg zum aussichtsreichen Schuhmacherfelsen", "Steep forest climb to the panoramic Schuhmacherfelsen crag",
    ),
    "Route from Parking to Wernersberger Geierstein.gpx": (
        "pfaelzerwald", "Lug → Wernersberger Geierstein", "Lug to Wernersberger Geierstein",
        "berg", True, "Felsenkamm-Wanderung zum markanten Geierstein über Lug", "Sandstone crag walk to the dramatic Geierstein above Lug",
    ),
    "Route from Rhodt_ Mitte to Rietburg Chairlift.gpx": (
        "pfaelzerwald", "Rhodt Mitte → Rietburgbahn", "Rhodt Mitte to Rietburg Chairlift",
        "wein", False, "Durch die Theresienstraße und Weinberge zur Rietburgbahn", "Through historic Rhodt and vineyards to the Rietburg chairlift",
    ),
    "Route from Scenic viewpoint to Aussichtsturm Baumwipfelpfad.gpx": (
        "schwarzwald", "Bad Wildbad Sommerberg → Baumwipfelpfad", "Bad Wildbad Sommerberg to Treetop Walk",
        "panoramatour", False, "Höhenweg auf dem Sommerberg zum Baumwipfelpfad Schwarzwald", "High ridge walk on Sommerberg to the Black Forest Treetop Walk",
    ),
    "Route from Scenic viewpoint to Ludwigs Tower.gpx": (
        "pfaelzerwald", "Rietburg → Ludwigsturm", "Rietburg to Ludwig's Tower",
        "berg", True, "Kurzer Kammweg zwischen Rietburg-Bergstation und Ludwigsturm", "Short ridge walk between Rietburg top station and Ludwig's Tower",
    ),
    "Route from Scenic viewpoint to Rhodt_ Mitte.gpx": (
        "pfaelzerwald", "Rietburg → Rhodt unter Rietburg", "Rietburg to Rhodt unter Rietburg",
        "wein", True, "Von der Rietburg-Bergstation durch Weinberge hinab nach Rhodt", "Descent from Rietburg summit through vineyards into Rhodt",
    ),
    "Route from Siedelsbrunn_ Buddhas Weg to Eiterbach_ Ortsende.gpx": (
        "odenwald", "Siedelsbrunn (Buddhas Weg) → Eiterbach", "Siedelsbrunn (Buddhas Weg) to Eiterbach",
        "wanderung", False, "Vom buddhistischen Kloster ins ruhige Eiterbachtal", "From the Buddhist monastery down into the quiet Eiterbach valley",
    ),
    "Route from Stäffelsberg - Altes Bild to Stäffelsberg - Altes Bild.gpx": (
        "pfaelzerwald", "Dörrenbach Stäffelsberg-Runde", "Dörrenbach Stäffelsberg Loop",
        "berg", True, "Runde um den Stäffelsbergturm bei Dörrenbach", "Loop around the Stäffelsberg lookout tower above Dörrenbach",
    ),
    "Route from Varnhalt Kirche to Yburg Castle.gpx": (
        "schwarzwald", "Varnhalt → Yburg", "Varnhalt to Yburg Castle",
        "burg", True, "Durch Rebhänge steil hinauf zur Ruine Yburg über dem Rheintal", "Steep climb through vineyards to the Yburg ruins overlooking the Rhine",
    ),
    "Route from Völkersbach Lamm to Malsch Adler.gpx": (
        "schwarzwald", "Völkersbach → Malsch", "Völkersbach to Malsch",
        "wanderung", False, "Vom Albtal-Vorland über Höhenrücken hinab nach Malsch", "From the Alb foothills across wooded ridges down to Malsch",
    ),
    "Route from Wenschdorf_ Bushaltestelle to Scenic viewpoint.gpx": (
        "odenwald", "Wenschdorf → Haagsaussicht (Miltenberg)", "Wenschdorf to Haagsaussicht (Miltenberg)",
        "panoramatour", True, "Über die Haagsaussicht mit Fernblick ins Maintal", "Via Haagsaussicht with wide views into the Main valley",
    ),
    "Route from Weyher_ Altes Rathaus to Rietburg Chairlift.gpx": (
        "pfaelzerwald", "Weyher → Rietburgbahn", "Weyher to Rietburg Chairlift",
        "wein", False, "Weinbergsweg von Weyher zur Talstation der Rietburgbahn", "Vineyard walk from Weyher to the Rietburg chairlift valley station",
    ),
    "Route from Windeck Castle to Bosch.gpx": (
        "schwarzwald", "Burg Windeck → Bühlertal (Bosch)", "Windeck Castle to Bühlertal (Bosch)",
        "burg", True, "Von der Burg Windeck über die Schwarzwaldhänge ins Bühlertal", "From Windeck Castle across foothills into the Bühlertal valley",
    ),
    "Route from Zwingenberg_Bd_ Bahnhof to Wolfsschlucht unterer Einstieg.gpx": (
        "odenwald", "Zwingenberg (Baden) → Wolfsschlucht", "Zwingenberg (Baden) to Wolfsschlucht Gorge",
        "wanderung", False, "Wildromantischer Anstieg in die Neckartal-Wolfsschlucht", "Wild romantic gorge climb into the Neckar valley Wolfsschlucht",
    ),
    "Route to Rehnergturm.gpx": (
        "pfaelzerwald", "Annweiler → Rehbergturm", "Annweiler to Rehberg Tower",
        "berg", True, "Steiler Aufstieg zum 577 m hohen Rehbergturm bei Annweiler", "Steep climb to the 577 m Rehberg lookout tower near Annweiler",
    ),
    "Route von Sand Kapelle nach Bühlertal Gertelbachstraße.gpx": (
        "schwarzwald", "Sand → Gertelbachschlucht → Bühlertal", "Sand via Gertelbach Gorge to Bühlertal",
        "wanderung", False, "Abstieg entlang der tosenden Gertelbach-Wasserfälle ins Bühlertal", "Descent past the cascading Gertelbach waterfalls into Bühlertal",
    ),
    "Trip from Ebersteinburg Kapelle.gpx": (
        "schwarzwald", "Ebersteinburg Nordost-Rundtour", "Ebersteinburg Northeast Loop",
        "wanderung", False, "Waldreiche Nordostschleife rund um Ebersteinburg", "Forested northeast loop starting at Ebersteinburg",
    ),
    "Trip from Freiolsheim Friedhof.gpx": (
        "schwarzwald", "Freiolsheim Mahlberg-Rundtour", "Freiolsheim Mahlberg Loop",
        "berg", True, "Aussichtstour auf den Mahlberg mit Turm im Nordschwarzwald", "Scenic loop up to the Mahlberg lookout tower in Northern Black Forest",
    ),
}

# Recommended multi-GPX chains (Explorer 2 §4.4)
CHAIN_DEFS = [
    {
        "id": "chain-drei-buchen-orensfelsen-godramstein",
        "region": "pfaelzerwald",
        "segments": [
            ("Route from Drei Buchen to Scenic viewpoint.gpx", False),
            ("Route from Parking to Orensfelsen.gpx", True),
        ],
        "ring": False,
        "category": "berg",
        "eveningSun": True,
        "title": {
            "de": "Drei Buchen → Orensfelsen → Godramstein",
            "en": "Drei Buchen via Orensfelsen to Godramstein",
        },
        "subtitle": {
            "de": "Durchgehende Pfälzerwald-Überschreitung vom Kamm hinab zur Weinstraße",
            "en": "Continuous Palatinate Forest ridge traverse down to the Wine Route",
        },
        "tips": {
            "de": "Verbindet den abgelegenen Orensfelsen-Kamm direkt mit dem Busanschluss in Godramstein.",
            "en": "Connects the remote Orensfelsen ridge directly to transit in Godramstein.",
        },
    },
    {
        "id": "chain-rietburg-weyher-loop",
        "region": "pfaelzerwald",
        "segments": [
            ("Route from Weyher_ Altes Rathaus to Rietburg Chairlift.gpx", False),
            ("Route from Scenic viewpoint to Ludwigs Tower.gpx", False),
            ("Route from Ludwigs Tower to Weyher_ Altes Rathaus.gpx", False),
        ],
        "ring": True,
        "category": "wein",
        "eveningSun": True,
        "title": {
            "de": "Weyher → Rietburg → Ludwigsturm Rundtour",
            "en": "Weyher – Rietburg – Ludwig's Tower Loop",
        },
        "subtitle": {
            "de": "Panoramarunde über die Rietburg (mit Sessellift oder Steig) und Ludwigsturm",
            "en": "Panoramic loop across Rietburg (chairlift or steep footpath) and Ludwig's Tower",
        },
        "tips": {
            "de": "Hinweis: Zwischen Rietburgbahn Talstation und Bergstation ca. 438 m / +200 Hm per Sessellift (nicht im D-Ticket) oder steilem Fußweg.",
            "en": "Note: 438 m / +200 m gap between Rietburg chairlift valley & summit stations (seasonal lift not in D-Ticket, or steep footpath).",
        },
    },
    {
        # Wunsch Oliver (2026-11-24): von Bushalt zu Bushalt – Edenkoben → Villa Ludwigshöhe → Rietburgbahn
        # (Sessellift hinauf) → Rietburg → Ludwigsturm → Weyher. Die Lücke zwischen Tal- und Bergstation ist der Lift.
        "id": "chain-edenkoben-rietburgbahn-ludwigsturm-weyher",
        "region": "pfaelzerwald",
        "lift": True,
        "segments": [
            ("Route from Edenkoben_ Abzw_ Ludwigshöhe to Rietburg Chairlift.gpx", False),
            ("Route from Scenic viewpoint to Ludwigs Tower.gpx", False),
            ("Route from Ludwigs Tower to Weyher_ Altes Rathaus.gpx", False),
        ],
        "ring": False,
        "category": "wein",
        "eveningSun": True,
        "title": {
            "de": "Edenkoben → Villa Ludwigshöhe → Rietburgbahn → Ludwigsturm → Weyher",
            "en": "Edenkoben – Villa Ludwigshöhe – Rietburg chairlift – Ludwig's Tower – Weyher",
        },
        "subtitle": {
            "de": "Von Bushalt zu Bushalt: zur Villa Ludwigshöhe, mit dem Sessellift zur Rietburg, über den Ludwigsturm hinab nach Weyher",
            "en": "Stop to stop: up to Villa Ludwigshöhe, chairlift to Rietburg, across Ludwig's Tower down to Weyher",
        },
        "tips": {
            "de": "Zwischen Villa Ludwigshöhe (Talstation) und Rietburg (Bergstation) fährt die Rietburgbahn (Sessellift, ca. 438 m / +200 Hm, nicht im D-Ticket, nur saisonal – Fahrzeiten vorher prüfen). Alternative: steiler Fußweg.",
            "en": "Between Villa Ludwigshöhe (valley station) and Rietburg (summit) the Rietburg chairlift runs (approx. 438 m / +200 m, not in the D-Ticket, seasonal – check operating times). Alternative: steep footpath.",
        },
    },
    {
        "id": "chain-rhodt-rietburg-loop",
        "region": "pfaelzerwald",
        "segments": [
            ("Route from Rhodt_ Mitte to Rietburg Chairlift.gpx", False),
            ("Route from Scenic viewpoint to Rhodt_ Mitte.gpx", False),
        ],
        "ring": True,
        "category": "wein",
        "eveningSun": True,
        "title": {
            "de": "Rhodt → Rietburg → Rhodt Rundtour",
            "en": "Rhodt – Rietburg – Rhodt Loop",
        },
        "subtitle": {
            "de": "Klassische Weinstraßen-Bergtour von Rhodt über die Rietburg",
            "en": "Classic Wine Route mountain loop from Rhodt via Rietburg",
        },
        "tips": {
            "de": "Hinweis: Aufstieg zur Rietburg-Bergstation per Sessellift (nicht im D-Ticket) oder Fußweg (+200 Hm).",
            "en": "Note: Ascent to Rietburg summit via seasonal chairlift (not in D-Ticket) or footpath (+200 m).",
        },
    },
    {
        "id": "chain-gertelbach-sand-schwarzenbach",
        "region": "schwarzwald",
        "segments": [
            ("Route von Sand Kapelle nach Bühlertal Gertelbachstraße.gpx", True),
            ("Route from Parking lot to Schwarzenbachtalsperre.gpx", False),
        ],
        "ring": False,
        "category": "berg",
        "eveningSun": True,
        "title": {
            "de": "Bühlertal (Gertelbach) → Badener Höhe → Schwarzenbachtalsperre",
            "en": "Bühlertal (Gertelbach) via Badener Höhe to Schwarzenbach Dam",
        },
        "subtitle": {
            "de": "Große Nordschwarzwald-Querung durch die Gertelbachschlucht über den Friedrichsturm",
            "en": "Grand Northern Black Forest traverse via Gertelbach Gorge & Friedrichsturm",
        },
        "tips": {
            "de": "Brückt den Abschnitt Sand/Friedrichsturm zwischen den Buslinien im Bühlertal und an der B 500.",
            "en": "Bridges the Sand/Friedrichsturm ridge between Bühlertal and B 500 bus stops.",
        },
    },
    {
        "id": "chain-korsika-eiterbach-siedelsbrunn",
        "region": "odenwald",
        "segments": [
            ("Route from Abzweig Korsika_Ludwigsdorf to Eiterbach_ Ortsende.gpx", False),
            ("Route from Siedelsbrunn_ Buddhas Weg to Eiterbach_ Ortsende.gpx", True),
        ],
        "ring": False,
        "category": "wanderung",
        "eveningSun": False,
        "title": {
            "de": "Ludwigsdorf (Korsika) → Eiterbach → Siedelsbrunn",
            "en": "Ludwigsdorf (Korsika) via Eiterbach to Siedelsbrunn",
        },
        "subtitle": {
            "de": "Odenwald-Waldquerung zwischen den Bushaltestellen Korsika und Buddhas Weg",
            "en": "Odenwald forest crossing linking Korsika and Siedelsbrunn Buddhas Weg bus stops",
        },
        "tips": {
            "de": "Verbindet beide Haltestellen direkt ohne Ausstieg am schwach angebundenen Ortseingang Eiterbach.",
            "en": "Links both served bus stops seamlessly across the Eiterbach valley.",
        },
    },
]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(x)))


def slugify(text: str, used: set[str]) -> str:
    rep = (
        text.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("→", "-")
        .replace("↔", "-")
    )
    rep = unicodedata.normalize("NFKD", rep).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", rep).strip("-")[:52].strip("-") or "tour"
    cand = slug
    n = 2
    while cand in used:
        suffix = f"-{n}"
        cand = f"{slug[:52 - len(suffix)].strip('-')}{suffix}"
        n += 1
    used.add(cand)
    return cand


def make_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


class RateLimitedAPI:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sess = make_session()
        self.last_net_ts = 0.0
        self.net_calls = 0

    def _wait_rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_net_ts
        if self.last_net_ts > 0 and elapsed < MIN_REQUEST_DELAY_S:
            time.sleep(MIN_REQUEST_DELAY_S - elapsed)
        self.last_net_ts = time.monotonic()
        self.net_calls += 1

    def get_map_stops(self, lat: float, lon: float) -> list[dict[str, Any]]:
        key = f"transitous_{round(lat, 4):.4f}_{round(lon, 4):.4f}.json"
        cpath = self.cache_dir / key
        if cpath.exists():
            return json.loads(cpath.read_text(encoding="utf-8"))
        self._wait_rate_limit()
        try:
            r = self.sess.get(
                "https://api.transitous.org/api/v1/map/stops",
                params={
                    "min": f"{lat - 0.012:.6f},{lon - 0.017:.6f}",
                    "max": f"{lat + 0.012:.6f},{lon + 0.017:.6f}",
                },
                timeout=25,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except requests.RequestException:
            return []

    def get_vrn_coord(self, lat: float, lon: float) -> list[dict[str, Any]]:
        key = f"vrn_{round(lat, 4):.4f}_{round(lon, 4):.4f}.json"
        cpath = self.cache_dir / key
        if cpath.exists():
            return json.loads(cpath.read_text(encoding="utf-8"))
        self._wait_rate_limit()
        try:
            r = self.sess.get(
                "https://www.vrn.de/mngvrn/XML_COORD_REQUEST",
                params={
                    "outputFormat": "rapidJSON",
                    "coordOutputFormat": "WGS84[dd.ddddd]",
                    "coord": f"{lon:.5f}:{lat:.5f}:WGS84[dd.ddddd]",
                    "max": 8,
                    "inclFilter": 1,
                    "radius_1": 1300,
                    "type_1": "STOP",
                },
                timeout=20,
            )
            r.raise_for_status()
            locs = r.json().get("locations", [])
        except Exception:
            locs = []
        cpath.write_text(json.dumps(locs, ensure_ascii=False), encoding="utf-8")
        return locs

    def get_geocode(self, text: str, lat: float, lon: float) -> list[dict[str, Any]]:
        h = hashlib.sha256(f"{text}|{lat:.4f}|{lon:.4f}".encode("utf-8")).hexdigest()[:16]
        cpath = self.cache_dir / f"geocode_{h}.json"
        if cpath.exists():
            return json.loads(cpath.read_text(encoding="utf-8"))
        self._wait_rate_limit()
        try:
            r = self.sess.get(
                "https://api.transitous.org/api/v1/geocode",
                params={"text": text, "place": f"{lat:.5f},{lon:.5f}", "type": "STOP", "language": "de"},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            data = []
        cpath.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data


def _vrn_modes_from_classes(classes: list[int]) -> list[str]:
    modes: set[str] = set()
    for c in classes:
        if c in (0, 1, 2):
            modes.add("REGIONAL_RAIL")
        elif c == 3:
            modes.add("SUBURBAN")
        elif c == 4:
            modes.add("TRAM")
        elif c in (5, 6):
            modes.add("BUS")
        elif c == 9:
            modes.add("FERRY")
        elif c == 10:
            modes.add("ODM")
    return sorted(modes) if modes else ["BUS"]


def resolve_nearest_stop(
    lat: float,
    lon: float,
    api: RateLimitedAPI,
    max_m: float = 800.0,
    prefer_different_from_dhid: str | None = None,
) -> dict[str, Any]:
    """Resolve nearest real public transit stop within <= max_m meters.

    - Excludes de-amarillo-* carpool stops and non-transit modes (RIDE_SHARING, []).
    - Cross-checks VRN-EFA XML_COORD_REQUEST (radius_1=1300, post-filtered by Haversine <= max_m).
    """
    raw_stops = api.get_map_stops(lat, lon)
    groups: dict[str, dict[str, Any]] = {}

    for st in raw_stops:
        sid = st.get("stopId") or ""
        if sid.startswith("de-amarillo-") or sid.startswith("eu-flixbus_") or not sid.startswith("de-"):
            continue
        modes = set(st.get("modes") or []) & REAL_TRANSIT_MODES
        if not modes:
            continue
        m_dhid = DHID_RE.search(sid)
        dhid = m_dhid.group(1) if m_dhid else sid
        d_m = haversine_m(lat, lon, float(st["lat"]), float(st["lon"]))
        is_parent = ("_Pde:" in sid) or (sid.count(":") == 2)
        grp = groups.setdefault(
            dhid,
            {
                "dhid": dhid,
                "modes": set(),
                "best_d": d_m,
                "best_quay": st,
                "parent_id": sid if is_parent else None,
            },
        )
        grp["modes"] |= modes
        if is_parent and grp["parent_id"] is None:
            grp["parent_id"] = sid
        if d_m < grp["best_d"]:
            grp["best_d"] = d_m
            grp["best_quay"] = st

    vrn_locs = api.get_vrn_coord(lat, lon)
    vrn_by_dhid: dict[str, dict[str, Any]] = {}
    vrn_within: list[tuple[float, dict[str, Any]]] = []
    for loc in vrn_locs:
        coords = loc.get("coord") or []
        if len(coords) < 2:
            continue
        v_lat, v_lon = float(coords[0]), float(coords[1])
        d_m = haversine_m(lat, lon, v_lat, v_lon)
        props = loc.get("properties") or {}
        g_id = props.get("STOP_GLOBAL_ID") or loc.get("id") or ""
        m_dhid = DHID_RE.search(g_id)
        dhid = m_dhid.group(1) if m_dhid else g_id
        classes = [int(c) for c in (loc.get("productClasses") or [])]
        entry = {
            "dhid": dhid,
            "name": props.get("STOP_NAME_WITH_PLACE") or loc.get("name") or "",
            "lat": round(v_lat, 6),
            "lon": round(v_lon, 6),
            "classes": classes,
            "d_m": d_m,
        }
        if d_m <= max_m:
            vrn_within.append((d_m, entry))
            if dhid:
                vrn_by_dhid[dhid] = entry
        elif dhid and dhid not in vrn_by_dhid:
            vrn_by_dhid[dhid] = entry

    within_groups: list[dict[str, Any]] = []
    nearest_any: tuple[float, str] | None = None
    for dhid, grp in groups.items():
        d_m = grp["best_d"]
        q = grp["best_quay"]
        if nearest_any is None or d_m < nearest_any[0]:
            nearest_any = (d_m, q.get("name") or dhid)
        if d_m <= max_m:
            grp["has_regular"] = bool(grp["modes"] & REGULAR_MODES)
            within_groups.append(grp)

    def pick_group(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not cands:
            return None
        regs = sorted([g for g in cands if g["has_regular"]], key=lambda g: g["best_d"])
        odms = sorted([g for g in cands if not g["has_regular"]], key=lambda g: g["best_d"])
        best_reg = regs[0] if regs else None
        best_odm = odms[0] if odms else None
        if best_reg and (best_odm is None or (best_reg["best_d"] - best_odm["best_d"] <= 150.0)):
            return best_reg
        return best_odm or best_reg

    filtered_groups = (
        [g for g in within_groups if g["dhid"] != prefer_different_from_dhid]
        if prefer_different_from_dhid
        else within_groups
    )
    chosen = pick_group(filtered_groups)
    if chosen is None and prefer_different_from_dhid:
        chosen = pick_group(within_groups)

    if chosen is not None:
        dhid = chosen["dhid"]
        q = chosen["best_quay"]
        vrn_info = vrn_by_dhid.get(dhid)
        classes = vrn_info["classes"] if vrn_info else []
        has_class_10 = 10 in classes
        modes_sorted = sorted(chosen["modes"])
        has_reg = chosen["has_regular"] and not (classes == [10])
        return {
            "found": True,
            "walkM": round(chosen["best_d"]),
            "stop": {
                "name": q.get("name") or dhid,
                "lat": round(float(q["lat"]), 6),
                "lon": round(float(q["lon"]), 6),
                "dhid": dhid if dhid.startswith("de:") else None,
                "transitousId": q.get("stopId"),
                "ibnr": None,
                "remoteId": None,
                "modes": modes_sorted,
                "odmOnly": not has_reg,
                "ruftaxiClass10": has_class_10,
            },
        }

    # VRN fallback if Transitous returned 0 hits <= max_m
    if vrn_within:
        vrn_within.sort(key=lambda x: x[0])
        if prefer_different_from_dhid:
            alt = [x for x in vrn_within if x[1]["dhid"] != prefer_different_from_dhid]
            if alt:
                vrn_within = alt
        d_m, v = vrn_within[0]
        classes = v["classes"]
        has_class_10 = 10 in classes
        modes_sorted = _vrn_modes_from_classes(classes)
        has_reg = bool(set(modes_sorted) & REGULAR_MODES) and not (classes == [10])
        return {
            "found": True,
            "walkM": round(d_m),
            "stop": {
                "name": v["name"],
                "lat": v["lat"],
                "lon": v["lon"],
                "dhid": v["dhid"] if v["dhid"].startswith("de:") else None,
                "transitousId": None,
                "ibnr": None,
                "remoteId": None,
                "modes": modes_sorted,
                "odmOnly": not has_reg,
                "ruftaxiClass10": has_class_10,
            },
        }

    # Check VRN unfiltered nearest for honest unlinkedReason
    for loc in vrn_locs:
        coords = loc.get("coord") or []
        if len(coords) >= 2:
            d_m = haversine_m(lat, lon, float(coords[0]), float(coords[1]))
            nm = (loc.get("properties") or {}).get("STOP_NAME_WITH_PLACE") or loc.get("name") or ""
            if nearest_any is None or d_m < nearest_any[0]:
                nearest_any = (d_m, nm)

    return {
        "found": False,
        "nearestDistM": round(nearest_any[0]) if nearest_any else None,
        "nearestName": nearest_any[1] if nearest_any else None,
        "stop": None,
    }


def enrich_station_stop(stop: dict[str, Any], api: RateLimitedAPI) -> dict[str, Any]:
    """Ensure every shortcut STATION stop has dhid & transitousId alongside stationId/remoteId/ibnr."""
    if stop.get("dhid") and stop.get("transitousId"):
        return stop
    if stop.get("stationId") in ("124355", "3024355") or (
        stop.get("lat") is not None
        and stop.get("lon") is not None
        and haversine_m(stop["lat"], stop["lon"], 49.753891, 8.629438) <= 600
    ):
        merged = dict(JUGENHEIM_HOME)
        return merged

    lat, lon = stop.get("lat"), stop.get("lon")
    if lat is None or lon is None:
        return stop

    res = resolve_nearest_stop(lat, lon, api, max_m=800.0)
    if res["found"]:
        rs = res["stop"]
        stop = dict(stop)
        if not stop.get("dhid") and rs.get("dhid"):
            stop["dhid"] = rs["dhid"]
        if not stop.get("transitousId") and rs.get("transitousId"):
            stop["transitousId"] = rs["transitousId"]
        if rs.get("modes") and not stop.get("modes"):
            stop["modes"] = rs["modes"]
        if rs.get("ruftaxiClass10"):
            stop["ruftaxiClass10"] = True
        return stop

    # Fallback: Transitous /api/v1/geocode by name
    hits = api.get_geocode(stop["name"], lat, lon)
    for h in hits:
        sid = h.get("id") or ""
        if sid.startswith("de-amarillo-"):
            continue
        m_dhid = DHID_RE.search(sid)
        stop = dict(stop)
        if m_dhid:
            stop["dhid"] = m_dhid.group(1)
        stop["transitousId"] = sid
        break
    return stop


def parse_shortcuts(api: RateLimitedAPI) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recover all 85 shortcuts from phone_shortcuts.json + android_shortcuts_raw.txt."""
    sc_path = DATA_DIR / "phone_shortcuts.json"
    raw_path = DATA_DIR / "android_shortcuts_raw.txt"
    phone_shortcuts = json.loads(sc_path.read_text(encoding="utf-8"))
    raw_txt = raw_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")

    raw_map: dict[tuple[str, str], str] = {}
    for m in SHORTCUT_BLOCK_RE.finditer(raw_txt):
        raw_map[(m.group(3).strip(), m.group(1).strip())] = m.group(4)

    assert len(raw_map) == 85, f"Expected 85 raw ShortcutInfo blocks, got {len(raw_map)}"

    recovered: list[dict[str, Any]] = []
    recovered_null_stations = 0
    raw_address_count = 0

    for idx, entry in enumerate(phone_shortcuts, 1):
        key = (entry["app"], entry["id"])
        assert key in raw_map, f"Missing raw block for #{idx} {key}"
        body = raw_map[key]
        rm = REQUESTDATA_RE.search(body)
        if not rm:
            recovered.append(
                {
                    "rawIndex": idx,
                    "app": entry["app"],
                    "id": entry["id"],
                    "label": entry["label"],
                    "hasRequestData": False,
                    "locations": {},
                    "params": {},
                }
            )
            continue

        kv: dict[str, str] = {}
        for line in rm.group(1).split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()

        locs: dict[str, dict[str, Any]] = {}
        for lkey in ("startLocation", "targetLocation", "viaLocation.0", "viaLocation.1"):
            if lkey not in kv:
                continue
            rl = json.loads(kv[lkey])
            loc_type = "ADDRESS" if rl.get("type") in ("ADDRESS", 2) else "STATION"
            lat = round(rl["geoPoint"]["lat"] / 1e6, 6)
            lon = round(rl["geoPoint"]["lon"] / 1e6, 6)
            sid = (
                str(rl["stationNumber"])
                if rl.get("stationNumber") is not None
                else (str(rl["extId"]) if rl.get("extId") is not None else None)
            )
            remote_id = rl.get("remoteId")
            ibnr = None
            if remote_id:
                lm = re.search(r"@L=(\d+)@", remote_id)
                if lm and len(lm.group(1)) == 7 and lm.group(1).startswith("80"):
                    ibnr = lm.group(1)
            elif sid and len(sid) == 7 and sid.startswith("80"):
                ibnr = sid

            orig_loc = (entry.get("locations") or {}).get(lkey)
            if orig_loc and orig_loc.get("id") is None:
                if loc_type == "STATION":
                    assert sid and remote_id, f"Failed to recover STATION id for #{idx} {lkey}"
                    recovered_null_stations += 1
                else:
                    raw_address_count += 1

            locs[lkey] = {
                "name": rl["name"],
                "type": loc_type,
                "lat": lat,
                "lon": lon,
                "stationId": sid,
                "remoteId": remote_id,
                "ibnr": ibnr,
                "dhid": None,
                "transitousId": None,
            }

        req_ms = int(kv["requestTime"]) if "requestTime" in kv else None
        req_iso = (
            datetime.fromtimestamp(req_ms / 1000.0, ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds")
            if req_ms is not None
            else None
        )
        pfield = kv.get("productsField") or None
        mode_hint = None
        if pfield:
            if pfield.startswith("000001"):
                mode_hint = "bus_only"
            elif pfield.startswith("000011"):
                mode_hint = "sbahn_bus"
            elif pfield.startswith("000111"):
                mode_hint = "dticket_all"
            elif pfield == "11111111111111":
                mode_hint = "all_incl_fv"

        params = {
            "bdepart": int(kv.get("bdepart", "1")),
            "requestTimeMs": req_ms,
            "requestTimeIso": req_iso,
            "productsField": pfield,
            "modeHint": mode_hint,
            "minChangeTime": int(kv["I>minChangeTime"]) if "I>minChangeTime" in kv else None,
        }
        recovered.append(
            {
                "rawIndex": idx,
                "app": entry["app"],
                "id": entry["id"],
                "label": entry["label"],
                "hasRequestData": True,
                "locations": locs,
                "params": params,
            }
        )

    assert recovered_null_stations == 123, f"Expected 123 recovered STATIONs, got {recovered_null_stations}"
    assert raw_address_count == 12, f"Expected 12 ADDRESS entries, got {raw_address_count}"

    excluded: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    borderline_set = {10, 17, 20, 54}

    for sc in recovered:
        idx = sc["rawIndex"]
        if idx in EXCLUDED_SPECS:
            cat, reason = EXCLUDED_SPECS[idx]
            excluded.append(
                {
                    "rawIndex": idx,
                    "app": sc["app"],
                    "id": sc["id"],
                    "label": sc["label"],
                    "category": cat,
                    "reason": reason,
                    "borderlineReviewed": idx in borderline_set,
                }
            )
            continue

        # Normalize home & resolve non-home ADDRESS + enrich STATION stops
        new_locs: dict[str, dict[str, Any]] = {}
        for lkey, loc in sc["locations"].items():
            if loc["type"] == "ADDRESS":
                if haversine_m(loc["lat"], loc["lon"], 49.753891, 8.629438) <= 600.0:
                    new_locs[lkey] = dict(JUGENHEIM_HOME)
                else:
                    res = resolve_nearest_stop(loc["lat"], loc["lon"], api, max_m=800.0)
                    assert res["found"], f"Failed to resolve ADDRESS #{idx} {lkey}: {loc}"
                    st = dict(res["stop"])
                    st["resolvedFromAddress"] = loc["name"]
                    new_locs[lkey] = st
            else:
                new_locs[lkey] = enrich_station_stop(loc, api)
        sc["locations"] = new_locs
        retained.append(sc)

    assert len(excluded) == 13, f"Expected 13 excluded, got {len(excluded)}"
    assert len(retained) == 72, f"Expected 72 retained, got {len(retained)}"
    return excluded, retained


def is_home_jugenheim(loc: dict[str, Any]) -> bool:
    if loc.get("stationId") in ("124355", "3024355", "8000948", "8000956"):
        return True
    if "Bickenbach(Bergstr)" in (loc.get("name") or ""):
        return True
    return haversine_m(loc["lat"], loc["lon"], 49.753891, 8.629438) <= 600.0


def smooth_elevation_hysteresis(eles: list[float], thresh_m: float = 3.0) -> tuple[int, int]:
    """Peak-aware directional hysteresis filter (3.0 m) to suppress DEM micro-jitter."""
    if not eles:
        return 0, 0
    gain = loss = 0.0
    state = 0
    low = high = eles[0]
    for e in eles[1:]:
        if state == 0:
            if e - low >= thresh_m:
                state = 1
                high = e
                gain += e - low
            elif high - e >= thresh_m:
                state = -1
                low = e
                loss += high - e
            if e < low:
                low = e
            if e > high:
                high = e
        elif state == 1:
            if e > high:
                gain += e - high
                high = e
            elif high - e >= thresh_m:
                state = -1
                low = e
                loss += high - e
        elif state == -1:
            if e < low:
                loss += low - e
                low = e
            elif e - low >= thresh_m:
                state = 1
                high = e
                gain += e - low
    return round(gain), round(loss)


def calc_walk_min(dist_km: float, elev_gain_m: int, elev_loss_m: int) -> int:
    """Swiss SAC hiking time (4.0 km/h, +400 m/h, -800 m/h, max + 0.5*min)."""
    t_horiz_h = dist_km / 4.0
    t_vert_h = (elev_gain_m / 400.0) + (elev_loss_m / 800.0)
    t_hours = max(t_horiz_h, t_vert_h) + 0.5 * min(t_horiz_h, t_vert_h)
    return math.ceil(t_hours * 60)


def _perp_dist_2d(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0.0 and dy == 0.0:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(pt[0] - (a[0] + t * dx), pt[1] - (a[1] + t * dy))


def _rdp_indices(pts_2d: list[tuple[float, float]], eps: float) -> list[int]:
    if len(pts_2d) <= 2:
        return list(range(len(pts_2d)))
    stack = [(0, len(pts_2d) - 1)]
    keep = {0, len(pts_2d) - 1}
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 <= 1:
            continue
        max_d = -1.0
        max_idx = -1
        a, b = pts_2d[i0], pts_2d[i1]
        for idx in range(i0 + 1, i1):
            d = _perp_dist_2d(pts_2d[idx], a, b)
            if d > max_d:
                max_d = d
                max_idx = idx
        if max_d > eps and max_idx != -1:
            keep.add(max_idx)
            stack.append((i0, max_idx))
            stack.append((max_idx, i1))
    return sorted(keep)


def encode_poly5(coords: list[tuple[float, float]]) -> str:
    """Google Encoded Polyline Algorithm Format at precision 1e5."""
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


def compute_hike_metrics(pts: list[tuple[float, float, float]], gpx_files: list[str]) -> dict[str, Any]:
    """Compute h3, Swiss walkMin, DP 5m poly5 (1e5), and <=30pt 1D-RDP elevProfile."""
    cum_km = [0.0]
    for i in range(1, len(pts)):
        d_m = haversine_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        cum_km.append(cum_km[-1] + d_m / 1000.0)
    dist_km = round(cum_km[-1], 2)
    eles = [p[2] for p in pts]
    gain_m, loss_m = smooth_elevation_hysteresis(eles, thresh_m=3.0)
    walk_min = calc_walk_min(dist_km, gain_m, loss_m)

    # 2D RDP (epsilon = 5.0 m) using local equirectangular projection
    lat0 = pts[0][0]
    cos_lat = math.cos(math.radians(lat0))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * cos_lat
    pts_m = [((p[1] - pts[0][1]) * m_per_deg_lon, (p[0] - lat0) * m_per_deg_lat) for p in pts]
    keep_2d = _rdp_indices(pts_m, eps=5.0)
    poly_coords = [(pts[i][0], pts[i][1]) for i in keep_2d]
    poly5 = encode_poly5(poly_coords)

    # 1D-RDP elevProfile (<= 30 pts)
    pts_elev = [(cum_km[i] * 50.0, eles[i]) for i in range(len(pts))]
    eps = 3.0
    keep_1d = _rdp_indices(pts_elev, eps=eps)
    while len(keep_1d) > 30:
        eps *= 1.25
        keep_1d = _rdp_indices(pts_elev, eps=eps)
    elev_profile = [[round(cum_km[i], 2), round(eles[i])] for i in keep_1d]

    return {
        "hasGpx": True,
        "gpxFiles": gpx_files,
        "distKm": dist_km,
        "elevGainM": gain_m,
        "elevLossM": loss_m,
        "walkMin": walk_min,
        "startCoord": [round(pts[0][0], 6), round(pts[0][1], 6)],
        "endCoord": [round(pts[-1][0], 6), round(pts[-1][1], 6)],
        "poly5": poly5,
        "elevProfile": elev_profile,
    }


def classify_waypoint_region(lat: float, lon: float) -> str:
    if lat >= 49.75 or (49.60 <= lat <= 49.70 and lon <= 8.20) or (49.0 <= lat <= 49.15 and 8.5 <= lon <= 8.7):
        return "weitere"
    if lon >= 8.58 and lat >= 49.35:
        return "odenwald"
    if lat <= 48.98:
        return "schwarzwald"
    return "pfaelzerwald"


def parse_waypoints_and_gpx() -> tuple[
    list[dict[str, Any]],
    dict[str, list[tuple[float, float, float]]],
    list[dict[str, str]],
]:
    """Parse 19 waypoints, verify My Tours.gpx parity, and deduplicate 66 -> 64 tracks."""
    wpt_root = ET.parse(MAPY_DIR / "points_26_11_23_143109.gpx").getroot()
    waypoints: list[dict[str, Any]] = []
    for w in wpt_root.findall("g:wpt", GPX_NS):
        lat = round(float(w.attrib["lat"]), 6)
        lon = round(float(w.attrib["lon"]), 6)
        ele = round(float(w.findtext("g:ele", "0", GPX_NS)), 1)
        name = w.findtext("g:name", "", GPX_NS).strip()
        waypoints.append(
            {
                "name": name,
                "lat": lat,
                "lon": lon,
                "ele": ele,
                "region": classify_waypoint_region(lat, lon),
            }
        )
    assert len(waypoints) == 19, f"Expected 19 waypoints, got {len(waypoints)}"

    # Verify parity with My Tours.gpx
    my_tours_root = ET.parse(MAPY_DIR / "My Tours.gpx").getroot()
    mt_wpts = my_tours_root.findall("g:wpt", GPX_NS)
    mt_trks = my_tours_root.findall("g:trk", GPX_NS)
    assert len(mt_wpts) == 19, f"My Tours.gpx wpt count {len(mt_wpts)} != 19"
    assert len(mt_trks) == 66, f"My Tours.gpx trk count {len(mt_trks)} != 66"

    cand_files = sorted(
        [
            f
            for f in os.listdir(MAPY_DIR)
            if f.endswith(".gpx") and f not in ("My Tours.gpx", "points_26_11_23_143109.gpx")
        ],
        key=lambda fn: (1 if re.search(r"\(\d+\)\.gpx$", fn) else 0, len(fn), fn),
    )
    assert len(cand_files) == 66, f"Expected 66 individual GPX files, got {len(cand_files)}"

    seen_sha: set[str] = set()
    sha_unique: list[tuple[str, list[tuple[float, float, float]]]] = []
    for fn in cand_files:
        raw = (MAPY_DIR / fn).read_bytes()
        h = hashlib.sha256(raw).hexdigest()
        if h in seen_sha:
            continue
        seen_sha.add(h)
        root = ET.fromstring(raw)
        pts = [
            (
                float(tp.attrib["lat"]),
                float(tp.attrib["lon"]),
                float(tp.findtext("g:ele", "0", GPX_NS)),
            )
            for tp in root.findall(".//g:trkpt", GPX_NS)
        ]
        sha_unique.append((fn, pts))

    assert len(sha_unique) == 65, f"Expected 65 SHA-unique GPX tracks, got {len(sha_unique)}"

    # Stage 2: geometric near-duplicate merge (>=95% within 50m and endpoints <=100m)
    dropped: set[str] = set()
    excluded_gpx: list[dict[str, str]] = []
    for i in range(len(sha_unique)):
        f1, p1 = sha_unique[i]
        if f1 in dropped:
            continue
        for j in range(i + 1, len(sha_unique)):
            f2, p2 = sha_unique[j]
            if f2 in dropped:
                continue
            near_fwd = abs(p1[0][0] - p2[0][0]) <= 0.005 and abs(p1[0][1] - p2[0][1]) <= 0.005
            near_rev = abs(p1[0][0] - p2[-1][0]) <= 0.005 and abs(p1[0][1] - p2[-1][1]) <= 0.005
            if not (near_fwd or near_rev):
                continue
            same_dir = (
                haversine_m(p1[0][0], p1[0][1], p2[0][0], p2[0][1]) <= 100.0
                and haversine_m(p1[-1][0], p1[-1][1], p2[-1][0], p2[-1][1]) <= 100.0
            )
            rev_dir = (
                haversine_m(p1[0][0], p1[0][1], p2[-1][0], p2[-1][1]) <= 100.0
                and haversine_m(p1[-1][0], p1[-1][1], p2[0][0], p2[0][1]) <= 100.0
            )
            if not (same_dir or rev_dir):
                continue
            c1 = sum(1 for a in p1 if min(haversine_m(a[0], a[1], b[0], b[1]) for b in p2) <= 50.0) / len(p1)
            c2 = sum(1 for b in p2 if min(haversine_m(b[0], b[1], a[0], a[1]) for a in p1) <= 50.0) / len(p2)
            if min(c1, c2) >= 0.95:
                # Keep the one with more points (and no (1) suffix on tie)
                if len(p1) >= len(p2):
                    kept_fn, drop_fn, kept_pts, drop_pts = f1, f2, len(p1), len(p2)
                    dropped.add(f2)
                else:
                    kept_fn, drop_fn, kept_pts, drop_pts = f2, f1, len(p2), len(p1)
                    dropped.add(f1)
                excluded_gpx.append(
                    {
                        "file": drop_fn,
                        "duplicateOf": kept_fn,
                        "stage": "geometric_near_duplicate",
                        "reason": (
                            f"Geometric near-duplicate of '{kept_fn}' "
                            f"(overlap {min(c1, c2) * 100:.1f}% <= 50m; "
                            f"retained {kept_pts}-pt track over {drop_pts}-pt track)."
                        ),
                    }
                )
                if drop_fn == f1:
                    break

    unique_tracks = {fn: pts for fn, pts in sha_unique if fn not in dropped}
    assert len(unique_tracks) == 64, f"Expected 64 unique GPX tracks, got {len(unique_tracks)}"
    return waypoints, unique_tracks, excluded_gpx


def merge_segments(
    segments: list[tuple[str, bool]],
    unique_tracks: dict[str, list[tuple[float, float, float]]],
) -> tuple[list[tuple[float, float, float]], list[str]]:
    merged: list[tuple[float, float, float]] = []
    files: list[str] = []
    for fn, rev in segments:
        pts = list(reversed(unique_tracks[fn])) if rev else list(unique_tracks[fn])
        files.append(fn)
        if merged:
            gap = haversine_m(merged[-1][0], merged[-1][1], pts[0][0], pts[0][1])
            if gap < 20.0:
                pts = pts[1:]
        merged.extend(pts)
    return merged, files


def region_from_coord(lat: float, lon: float) -> str:
    if lat >= 49.72 and lon < 8.60:
        return "weitere"
    if lat >= 49.76 and lon > 9.15:
        return "weitere"
    if lon <= 8.40 and lat >= 49.00 and lat <= 49.68:
        return "pfaelzerwald" if lon >= 7.60 else "weitere"
    if lat <= 48.98 and lon >= 7.80:
        return "schwarzwald"
    if lon >= 8.55 and 49.30 <= lat <= 49.82:
        return "odenwald"
    return "weitere"


def difficulty_from_hike(hike: dict[str, Any]) -> str:
    if not hike.get("hasGpx"):
        return "leicht"
    wm = hike["walkMin"]
    gain = hike["elevGainM"]
    if wm < 90 and gain < 250:
        return "leicht"
    if wm > 180 or gain > 500:
        return "schwer"
    return "mittel"


def empty_hike() -> dict[str, Any]:
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


def resolve_gpx_endpoints(
    hike: dict[str, Any],
    ring: bool,
    retained_by_idx: dict[int, dict[str, Any]],
    api: RateLimitedAPI,
    forced_start: dict[str, Any] | None = None,
    forced_end: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int | None, int | None, str | None, bool]:
    """Apply Rule A (<= 250 m non-home shortcut snap with non-ring guard) + Rule B (<= 800 m lookup)."""
    s_lat, s_lon = hike["startCoord"]
    e_lat, e_lon = hike["endCoord"]

    # Gather non-home shortcut STATION candidates
    sc_stations: list[dict[str, Any]] = []
    for sc in retained_by_idx.values():
        for lkey, loc in sc["locations"].items():
            if loc.get("type") == "STATION" and not is_home_jugenheim(loc):
                sc_stations.append(loc)

    def nearest_shortcut_250(lat: float, lon: float) -> tuple[float, dict[str, Any]] | None:
        best: tuple[float, dict[str, Any]] | None = None
        for st in sc_stations:
            d = haversine_m(lat, lon, st["lat"], st["lon"])
            if d <= 250.0 and (best is None or d < best[0]):
                best = (d, st)
        return best

    s_stop = forced_start
    s_dist = round(haversine_m(s_lat, s_lon, s_stop["lat"], s_stop["lon"])) if s_stop else None
    e_stop = forced_end
    e_dist = round(haversine_m(e_lat, e_lon, e_stop["lat"], e_stop["lon"])) if e_stop else None

    cand_s = None if s_stop else nearest_shortcut_250(s_lat, s_lon)
    cand_e = None if e_stop else nearest_shortcut_250(e_lat, e_lon)

    # Non-ring collapse guard between Rule A candidates
    if cand_s and cand_e and not ring:
        id_s = cand_s[1].get("dhid") or cand_s[1].get("stationId") or cand_s[1]["name"]
        id_e = cand_e[1].get("dhid") or cand_e[1].get("stationId") or cand_e[1]["name"]
        if id_s == id_e:
            if cand_s[0] <= cand_e[0]:
                cand_e = None
            else:
                cand_s = None

    if s_stop is None and cand_s:
        s_stop = dict(cand_s[1])
        s_dist = round(cand_s[0])
    if e_stop is None and cand_e:
        e_stop = dict(cand_e[1])
        e_dist = round(cand_e[0])

    s_reason = None
    e_reason = None

    if s_stop is None:
        res_s = resolve_nearest_stop(s_lat, s_lon, api, max_m=800.0)
        if res_s["found"]:
            s_stop = res_s["stop"]
            s_dist = res_s["walkM"]
        else:
            nd, nn = res_s["nearestDistM"], res_s["nearestName"]
            s_reason = (
                f"Start > 800 m vom nächsten ÖPNV-Halt ('{nn}', {nd} m)"
                if nd
                else "Start > 800 m vom nächsten ÖPNV-Halt (kein Halt im 1,3 km Umkreis)"
            )

    if e_stop is None:
        s_dhid = s_stop.get("dhid") if (s_stop and not ring) else None
        res_e = resolve_nearest_stop(e_lat, e_lon, api, max_m=800.0, prefer_different_from_dhid=s_dhid)
        if res_e["found"]:
            e_stop = res_e["stop"]
            e_dist = res_e["walkM"]
        else:
            nd, nn = res_e["nearestDistM"], res_e["nearestName"]
            e_reason = (
                f"Ende > 800 m vom nächsten ÖPNV-Halt ('{nn}', {nd} m)"
                if nd
                else "Ende > 800 m vom nächsten ÖPNV-Halt (kein Halt im 1,3 km Umkreis)"
            )

    # If a short non-ring spur genuinely has only ONE single transit stop within 800 m for both ends
    # (e.g. Idar-Oberstein Felsenkirche / Burg Lindelbrunn / Drei Buchen -> Meistersel), treat transit as ring=True.
    effective_ring = ring
    if not effective_ring and s_stop and e_stop:
        id_s = s_stop.get("dhid") or s_stop.get("stationId") or s_stop["name"]
        id_e = e_stop.get("dhid") or e_stop.get("stationId") or e_stop["name"]
        if id_s == id_e:
            effective_ring = True

    reasons = [r for r in (s_reason, e_reason) if r]
    unlinked_reason = "; ".join(reasons) if reasons else None
    if unlinked_reason:
        if s_reason:
            s_stop, s_dist = None, None
        if e_reason:
            e_stop, e_dist = None, None

    return s_stop, e_stop, s_dist, e_dist, unlinked_reason, effective_ring


def make_shortcut_tour(
    tour_id: str,
    title_de: str,
    title_en: str,
    sub_de: str,
    sub_en: str,
    region: str,
    category: str,
    ring: bool,
    evening_sun: bool,
    kind: str,
    start_stop: dict[str, Any] | None,
    end_stop: dict[str, Any] | None,
    vias: list[dict[str, Any]],
    hike: dict[str, Any],
    raw_indices: list[int],
    walk_to_start_m: int | None = None,
    walk_from_end_m: int | None = None,
    unlinked_reason: str | None = None,
    min_change_time: int | None = None,
    mode_hint: str | None = None,
    bdepart: int = 1,
    tips_de: str | None = None,
    tips_en: str | None = None,
    places: list[str] | None = None,
) -> dict[str, Any]:
    if places is None:
        places = []
        for p in ([start_stop["name"]] if start_stop else []) + [v["name"] for v in vias] + (
            [end_stop["name"]] if end_stop else []
        ):
            if p and (not places or places[-1] != p):
                places.append(p)

    sun_de = (
        "Schöne Nachmittags- und Abendsonne auf offenen Höhenzügen / Aussichtspunkten."
        if evening_sun
        else "Ganztags gut kombinierbar; schattige Wald- oder Talabschnitte vorhanden."
    )
    sun_en = (
        "Great afternoon and evening sun along open ridges and viewpoints."
        if evening_sun
        else "Suitable all day; includes shaded forest or valley sections."
    )
    return {
        "id": tour_id,
        "source": "shortcut",
        "region": region,
        "category": category,
        "difficulty": difficulty_from_hike(hike),
        "ring": ring,
        "title": {"de": title_de, "en": title_en},
        "subtitle": {"de": sub_de, "en": sub_en},
        "sun": {"de": sun_de, "en": sun_en},
        "tips": {
            "de": tips_de or "Deutschlandticket-tauglich; letzte Rückfahrt am Abend beachten.",
            "en": tips_en or "Valid with Deutschlandticket; check last safe evening return.",
        },
        "eveningSun": evening_sun,
        "places": places,
        "rawShortcutIndices": raw_indices,
        "minChangeTime": min_change_time,
        "modeHint": mode_hint,
        "bdepart": bdepart,
        "transit": {
            "kind": kind,
            "startStop": start_stop,
            "endStop": end_stop,
            "vias": vias,
            "walkToStartM": walk_to_start_m,
            "walkFromEndM": walk_from_end_m,
            "unlinkedReason": unlinked_reason,
        },
        "hike": hike,
    }


def build_all_tours(
    retained: list[dict[str, Any]],
    unique_tracks: dict[str, list[tuple[float, float, float]]],
    api: RateLimitedAPI,
) -> list[dict[str, Any]]:
    by_idx = {sc["rawIndex"]: sc for sc in retained}
    used_slugs: set[str] = set()
    tours: list[dict[str, Any]] = []
    absorbed_gpx: set[str] = set()

    def via_entry(loc: dict[str, Any], duration_min: int = 60) -> dict[str, Any]:
        v = dict(loc)
        v["durationMin"] = duration_min
        return v

    # 1. 19 x shortcut_360
    ring_360_meta: dict[int, tuple[str, str, str, str, bool]] = {
        1: ("pfaelzerwald", "Weinstraße-Runde: Diedesfeld – Germersheim", "Wine Route Loop: Diedesfeld – Germersheim", "wein", True),
        2: ("pfaelzerwald", "Pfälzerwald-Runde: Kaiserslautern – Trippstadt – Waldfischbach", "Palatinate Loop: Kaiserslautern – Trippstadt – Waldfischbach", "panoramatour", False),
        8: ("weitere", "Kraichgau & Neckar-Runde: Karlsruhe – Heilbronn", "Kraichgau & Neckar Loop: Karlsruhe – Heilbronn", "stadt", False),
        11: ("pfaelzerwald", "Westpfalz-Runde: Kaiserslautern – Ramstein – Landstuhl", "West Palatinate Loop: Kaiserslautern – Ramstein – Landstuhl", "panoramatour", False),
        14: ("pfaelzerwald", "Neustadter Tal-Runde: Lambrecht – Lindenberg", "Neustadt Valley Loop: Lambrecht – Lindenberg", "wanderung", False),
        18: ("pfaelzerwald", "Wasgau-Runde: Bad Bergzabern – Darstein – Annweiler", "Wasgau Loop: Bad Bergzabern – Darstein – Annweiler", "panoramatour", True),
        19: ("odenwald", "Odenwald-Main-Runde: Eberbach – Miltenberg", "Odenwald-Main Loop: Eberbach – Miltenberg", "panoramatour", False),
        21: ("pfaelzerwald", "Haardt-Rheinhessen-Runde: Bad Dürkheim – Grünstadt – Worms", "Haardt-Rheinhessen Loop: Bad Dürkheim – Grünstadt – Worms", "wein", True),
        24: ("weitere", "Saar-Pfalz-Runde: Homburg (Saar) – Kusel", "Saar-Palatinate Loop: Homburg (Saar) – Kusel", "panoramatour", False),
        27: ("schwarzwald", "Murgtal-Schwarzwald-Runde: Forbach – Schwarzenbachtalsperre – Bühl", "Murg Valley Black Forest Loop: Forbach – Schwarzenbach Dam – Bühl", "see", True),
        44: ("schwarzwald", "Albtal-Nordschwarzwald-Runde: Bad Herrenalb – Bühl", "Albtal Black Forest Loop: Bad Herrenalb – Bühl", "panoramatour", False),
        47: ("odenwald", "Ried-Bergstraße-Runde: Groß-Rohrheim – Hüttenfeld", "Ried-Bergstraße Loop: Groß-Rohrheim – Hüttenfeld", "panoramatour", False),
        49: ("pfaelzerwald", "Dahner Felsenland-Runde: Böllenborn – Nothweiler", "Dahn Rock Country Loop: Böllenborn – Nothweiler", "burg", True),
        50: ("schwarzwald", "Schwarzwald-Rhein-Runde: Bad Herrenalb – Bühl – Rastatt", "Black Forest–Rhine Loop: Bad Herrenalb – Bühl – Rastatt", "panoramatour", False),
        56: ("weitere", "Nahe-Glan-Runde: Kusel – Baumholder – Idar-Oberstein", "Nahe-Glan Loop: Kusel – Baumholder – Idar-Oberstein", "panoramatour", False),
        58: ("odenwald", "Ried-Odenwald-Runde: Gernsheim – Darmstadt Hbf", "Ried–Darmstadt Loop: Gernsheim – Darmstadt Hbf", "stadt", False),
        60: ("schwarzwald", "Schwarzwaldhochstraße-Runde: Achern – Kniebis – Freudenstadt", "Black Forest High Road Loop: Achern – Kniebis – Freudenstadt", "berg", True),
        83: ("weitere", "Mittelrhein-Lahn-Runde: Koblenz – Limburg", "Middle Rhine–Lahn Loop: Koblenz – Limburg", "stadt", True),
        84: ("weitere", "Taunus-Gipfeltour: Großer Feldberg", "Taunus Summit Tour: Großer Feldberg", "berg", True),
    }
    for idx, (reg, de_t, en_t, cat, esun) in ring_360_meta.items():
        sc = by_idx[idx]
        locs = sc["locations"]
        vias = [via_entry(locs[k]) for k in ("viaLocation.0", "viaLocation.1") if k in locs]
        tid = slugify(f"r360-{sc['label']}", used_slugs)
        tours.append(
            make_shortcut_tour(
                tour_id=tid,
                title_de=de_t,
                title_en=en_t,
                sub_de=f"360°-ÖPNV-Ringtour über {' → '.join(v['name'] for v in vias)}",
                sub_en=f"360° transit ring tour via {' → '.join(v['name'] for v in vias)}",
                region=reg,
                category=cat,
                ring=True,
                evening_sun=esun,
                kind="shortcut_360",
                start_stop=locs["startLocation"],
                end_stop=locs["targetLocation"],
                vias=vias,
                hike=empty_hike(),
                raw_indices=[idx],
                min_change_time=sc["params"]["minChangeTime"],
                mode_hint=sc["params"]["modeHint"],
                bdepart=sc["params"]["bdepart"],
            )
        )

    # 2. 9 x shortcut_pair (P1..P9)
    pair_defs = [
        (5, 4, "weitere", "Rastatt-Rhein-Runde (via Blankenloch & Illingen)", "Rastatt–Rhine Loop (via Blankenloch & Illingen)", "stadt", False, None),
        (16, 12, "odenwald", "Neckar-Odenwald-Runde: Eberbach – Mosbach – Aglasterhausen", "Neckar–Odenwald Loop: Eberbach – Mosbach – Aglasterhausen", "panoramatour", False, None),
        (32, 30, "odenwald", "Odenwald-Dreiländer-Runde: Eulbach – Amorbach – Mudau – Mosbach", "Odenwald Loop: Eulbach – Amorbach – Mudau – Mosbach", "panoramatour", False, None),
        (46, 34, "schwarzwald", "Gertelbachschlucht: Sand Kapelle → Bühlertal", "Gertelbach Gorge: Sand Kapelle to Bühlertal", "wanderung", False, "Route von Sand Kapelle nach Bühlertal Gertelbachstraße.gpx"),
        (75, 74, "weitere", "Loreley-Plateau & Taunus-Runde (St. Goarshausen – Nastätten)", "Loreley Plateau & Taunus Loop (St. Goarshausen – Nastätten)", "panoramatour", True, None),
        (35, 42, "weitere", "Spessart-Main-Runde: Rothenbuch – Lohr am Main – Würzburg", "Spessart–Main Loop: Rothenbuch – Lohr am Main – Würzburg", "panoramatour", False, None),
        (67, 59, "odenwald", "Odenwald-Main-Querung: Mudau – Miltenberg – Mömlingen", "Odenwald–Main Crossing: Mudau – Miltenberg – Mömlingen", "panoramatour", False, None),
        (33, 82, "schwarzwald", "Renchtal-Ortenau-Runde: Sasbachried – Oppenau – Offenburg", "Renchtal–Ortenau Loop: Sasbachried – Oppenau – Offenburg", "panoramatour", False, None),
        (13, 3, "pfaelzerwald", "Pfälzerwald-Busrunde: Ramstein – Fischbach – Frankenstein – Enkenbach", "Palatinate Bus Loop: Ramstein – Fischbach – Frankenstein – Enkenbach", "panoramatour", False, None),
    ]
    for out_i, in_i, reg, de_t, en_t, cat, esun, gpx_fn in pair_defs:
        sc_out, sc_in = by_idx[out_i], by_idx[in_i]
        if gpx_fn:
            absorbed_gpx.add(gpx_fn)
            hike = compute_hike_metrics(unique_tracks[gpx_fn], [gpx_fn])
            s_stop, e_stop, s_d, e_d, u_rsn, eff_ring = resolve_gpx_endpoints(hike, False, by_idx, api)
            tid = slugify(f"pair-{de_t}", used_slugs)
            tours.append(
                make_shortcut_tour(
                    tour_id=tid,
                    title_de=de_t,
                    title_en=en_t,
                    sub_de="Kombinierte Hin-/Rückfahrt mit Gertelbachschlucht-Wanderung (5,4 km)",
                    sub_en="Outbound/return bus pair with Gertelbach Gorge hike (5.4 km)",
                    region=reg,
                    category=cat,
                    ring=eff_ring,
                    evening_sun=esun,
                    kind="shortcut_pair",
                    start_stop=s_stop,
                    end_stop=e_stop,
                    vias=[],
                    hike=hike,
                    raw_indices=[out_i, in_i],
                    walk_to_start_m=s_d,
                    walk_from_end_m=e_d,
                    unlinked_reason=u_rsn,
                    mode_hint=sc_out["params"]["modeHint"] or sc_in["params"]["modeHint"],
                    tips_de="Anfahrt nach Sand Kapelle, Abstieg durch die Gertelbach-Wasserfälle nach Bühlertal.",
                    tips_en="Bus to Sand Kapelle, descend along Gertelbach waterfalls to Bühlertal.",
                )
            )
        else:
            vias: list[dict[str, Any]] = []
            for k in ("viaLocation.0", "viaLocation.1"):
                if k in sc_out["locations"]:
                    vias.append(via_entry(sc_out["locations"][k]))
            if not is_home_jugenheim(sc_out["locations"]["targetLocation"]):
                vias.append(via_entry(sc_out["locations"]["targetLocation"]))
            if not is_home_jugenheim(sc_in["locations"]["startLocation"]):
                s_in = sc_in["locations"]["startLocation"]
                if not vias or (s_in.get("dhid") or s_in["name"]) != (vias[-1].get("dhid") or vias[-1]["name"]):
                    vias.append(via_entry(s_in))
            for k in ("viaLocation.0", "viaLocation.1"):
                if k in sc_in["locations"]:
                    vias.append(via_entry(sc_in["locations"][k]))

            tid = slugify(f"pair-{de_t}", used_slugs)
            tours.append(
                make_shortcut_tour(
                    tour_id=tid,
                    title_de=de_t,
                    title_en=en_t,
                    sub_de=f"Kombinierte Rundfahrt über {' → '.join(v['name'] for v in vias[:3])}",
                    sub_en=f"Combined round-trip via {' → '.join(v['name'] for v in vias[:3])}",
                    region=reg,
                    category=cat,
                    ring=True,
                    evening_sun=esun,
                    kind="shortcut_pair",
                    start_stop=sc_out["locations"]["startLocation"],
                    end_stop=sc_in["locations"]["targetLocation"],
                    vias=vias,
                    hike=empty_hike(),
                    raw_indices=[out_i, in_i],
                    mode_hint=sc_out["params"]["modeHint"] or sc_in["params"]["modeHint"],
                )
            )

    # 3. Chained GPX-linked outbound pair #78 + #79 (Höningen -> Sonnenwende -> Vigilienstraße)
    sc78, sc79 = by_idx[78], by_idx[79]
    seg78_79 = [
        ("Route from Höningen_ Ort to Bad Dürkheim_ Sonnenwende.gpx", False),
        ("Route from Bad Dürkheim_ Sonnenwende to Bad Dürkheim_ Vigilienstraße.gpx", False),
    ]
    absorbed_gpx.update(s[0] for s in seg78_79)
    pts78_79, files78_79 = merge_segments(seg78_79, unique_tracks)
    hike78_79 = compute_hike_metrics(pts78_79, files78_79)
    s_stop, e_stop, s_d, e_d, u_rsn, eff_ring = resolve_gpx_endpoints(
        hike78_79, False, by_idx, api, forced_start=sc78["locations"]["targetLocation"]
    )
    places78_79: list[str] = []
    for p in ([s_stop["name"]] if s_stop else []) + [sc79["locations"]["targetLocation"]["name"]] + (
        [e_stop["name"]] if e_stop else []
    ):
        if p and (not places78_79 or places78_79[-1] != p):
            places78_79.append(p)
    tours.append(
        make_shortcut_tour(
            tour_id=slugify("hoeningen-sonnenwende-bad-duerkheim", used_slugs),
            title_de="Höningen → Bismarckturm Peterskopf → Bad Dürkheim",
            title_en="Höningen via Peterskopf Bismarck Tower to Bad Dürkheim",
            sub_de="Kombinierte 11,3 km Pfälzerwald-Wanderung über den Peterskopf nach Bad Dürkheim",
            sub_en="Combined 11.3 km Palatinate Forest hike via Peterskopf to Bad Dürkheim",
            region="pfaelzerwald",
            category="berg",
            ring=eff_ring,
            evening_sun=True,
            kind="gpx_linked",
            start_stop=s_stop,
            end_stop=e_stop,
            vias=[],
            hike=hike78_79,
            raw_indices=[78, 79],
            walk_to_start_m=s_d,
            walk_from_end_m=e_d,
            unlinked_reason=u_rsn,
            tips_de="Start in Höningen (Bus), über Bismarckturm & Sonnenwende hinab nach Bad Dürkheim Vigilienstraße / Bahnhof.",
            tips_en="Start at Höningen (bus), cross Peterskopf Bismarck Tower & Sonnenwende down to Bad Dürkheim.",
            places=places78_79,
        )
    )

    # 4. Standalone Inbound #68 (Buchen(Odenw) -> Jugenheim Bus)
    sc68 = by_idx[68]
    home68 = sc68["locations"]["targetLocation"]
    vias68 = [
        via_entry(sc68["locations"][k])
        for k in ("startLocation", "viaLocation.0", "viaLocation.1")
        if k in sc68["locations"]
    ]
    tours.append(
        make_shortcut_tour(
            tour_id=slugify("buchen-odenwald-bus-rueckfahrt", used_slugs),
            title_de="Odenwald-Busquerung: Buchen → Eberbach → Reichelsheim",
            title_en="Odenwald Bus Crossing: Buchen – Eberbach – Reichelsheim",
            sub_de="Landschaftliche Bus-Entdeckertour quer durch den Odenwald",
            sub_en="Scenic regional bus crossing right through the Odenwald",
            region="odenwald",
            category="panoramatour",
            ring=True,
            evening_sun=False,
            kind="shortcut_standalone",
            start_stop=dict(home68),
            end_stop=dict(home68),
            vias=vias68,
            hike=empty_hike(),
            raw_indices=[68],
            min_change_time=sc68["params"]["minChangeTime"],
            mode_hint=sc68["params"]["modeHint"],
            bdepart=sc68["params"]["bdepart"],
        )
    )

    # 5. 11 GPX-linked Standalone Outbound Shortcuts
    gpx_out_defs = [
        (29, "Route from Oberzent-Kortelshütte Unterdorf to Hirschhorn_ Bahnhof.gpx", "start"),
        (36, "Route from Hornisgrinde Turm to Kaltenbrunnen.gpx", "start"),
        (43, "Route from Weyher_ Altes Rathaus to Rietburg Chairlift.gpx", "start"),
        (45, "Route from Buchklingen to Weinheim_ Schlossbergterrasse.gpx", "start"),
        (48, "Route from Windeck Castle to Bosch.gpx", "start"),
        (51, "Route from Nieder-Liebersbach_ Rathaus to Weinheim (Bergstraße).gpx", "end"),
        (57, "Route from Kaltenbronn Rankhütte to Gaistal Talwiese.gpx", "start"),
        (61, "Route from Zwingenberg_Bd_ Bahnhof to Wolfsschlucht unterer Einstieg.gpx", "start"),
        (71, "Route from Ebersteinburg Kapelle to Ebersteinburg Kapelle.gpx", "start"),
        (72, "Route from Annweiler_ Burg Trifels to Leinsweiler_ Ort.gpx", "start"),
        (80, "Route from Dimbach_ Ort to Darstein_ Ortsmitte.gpx", "start"),
    ]
    for idx, gpx_fn, anchor_side in gpx_out_defs:
        sc = by_idx[idx]
        absorbed_gpx.add(gpx_fn)
        reg, de_t, en_t, cat, esun, sub_de, sub_en = GPX_META[gpx_fn]
        pts = unique_tracks[gpx_fn]
        hike = compute_hike_metrics(pts, [gpx_fn])
        raw_ring = haversine_m(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) <= 150.0
        target_stop = sc["locations"]["targetLocation"]
        vias: list[dict[str, Any]] = []
        forced_s = target_stop if anchor_side == "start" else None
        forced_e = target_stop if anchor_side == "end" else None
        s_stop, e_stop, s_d, e_d, u_rsn, eff_ring = resolve_gpx_endpoints(
            hike, raw_ring, by_idx, api, forced_start=forced_s, forced_end=forced_e
        )
        tours.append(
            make_shortcut_tour(
                tour_id=slugify(de_t, used_slugs),
                title_de=de_t,
                title_en=en_t,
                sub_de=sub_de,
                sub_en=sub_en,
                region=reg,
                category=cat,
                ring=eff_ring,
                evening_sun=esun,
                kind="gpx_linked",
                start_stop=s_stop,
                end_stop=e_stop,
                vias=vias,
                hike=hike,
                raw_indices=[idx],
                walk_to_start_m=s_d,
                walk_from_end_m=e_d,
                unlinked_reason=u_rsn,
                min_change_time=sc["params"]["minChangeTime"],
                mode_hint=sc["params"]["modeHint"],
                bdepart=sc["params"]["bdepart"],
            )
        )

    # 6. 8 Transit-only Standalone Outbound Shortcuts (incl. #62 Bad Wildbad Bf)
    transit_out_meta = {
        15: ("pfaelzerwald", "Weinstraße Bus 500: St. Martin – Burrweiler – Landau", "Wine Route Bus 500: St. Martin – Burrweiler – Landau", "wein", True),
        22: ("odenwald", "Kurpfalz-Bus S6: Großsachsen – Schwetzingen – Wiesloch", "Kurpfalz Bus S6: Großsachsen – Schwetzingen – Wiesloch", "stadt", False),
        40: ("weitere", "Neckartal-Stauferstadt: Bad Wimpfen (via Heinsheim)", "Neckar Historic Town: Bad Wimpfen (via Heinsheim)", "stadt", True),
        41: ("weitere", "Odenwald-Franken-Tour: Mosbach → Würzburg Hbf", "Odenwald–Franconia Tour: Mosbach to Würzburg Hbf", "panoramatour", False),
        55: ("weitere", "Kraichgau-Panoramatour nach Karlsruhe über Eppingen", "Kraichgau Scenic Tour to Karlsruhe via Eppingen", "panoramatour", False),
        62: ("schwarzwald", "Enztal-Schwarzwaldtour: Bad Wildbad (via Calw)", "Enz Valley Black Forest Tour: Bad Wildbad (via Calw)", "panoramatour", False),
        65: ("weitere", "Main-Tauber-Tour: Wertheim → Würzburg Hbf", "Main–Tauber Tour: Wertheim to Würzburg Hbf", "stadt", False),
        70: ("weitere", "Bauland-Franken-Bus: Buchen (Odenw) → Würzburg Hbf", "Bauland–Franconia Bus: Buchen to Würzburg Hbf", "panoramatour", False),
    }
    for idx, (reg, de_t, en_t, cat, esun) in transit_out_meta.items():
        sc = by_idx[idx]
        locs = sc["locations"]
        vias = [via_entry(locs[k]) for k in ("viaLocation.0", "viaLocation.1") if k in locs]
        tours.append(
            make_shortcut_tour(
                tour_id=slugify(de_t, used_slugs),
                title_de=de_t,
                title_en=en_t,
                sub_de=f"ÖPNV-Tagestour nach {locs['targetLocation']['name']}",
                sub_en=f"Transit day excursion to {locs['targetLocation']['name']}",
                region=reg,
                category=cat,
                ring=True,
                evening_sun=esun,
                kind="shortcut_standalone",
                start_stop=locs["startLocation"],
                end_stop=locs["targetLocation"],
                vias=vias,
                hike=empty_hike(),
                raw_indices=[idx],
                min_change_time=sc["params"]["minChangeTime"],
                mode_hint=sc["params"]["modeHint"],
                bdepart=sc["params"]["bdepart"],
            )
        )

    # 7. 13 Standalone P2P Shortcuts
    p2p_meta = {
        6: ("schwarzwald", "Schwarzwald-Querspange: Karlsruhe → Grünwettersbach → Pforzheim (Bus)", "Black Forest Bus Link: Karlsruhe via Grünwettersbach to Pforzheim", "panoramatour", False),
        26: ("schwarzwald", "Alb-Murg-Bus: Baden-Baden → Gaggenau → Ettlingen", "Alb–Murg Bus: Baden-Baden via Gaggenau to Ettlingen", "panoramatour", False),
        28: ("schwarzwald", "Enz-Albtal-Verbindung: Pforzheim → Bad Herrenalb", "Enz–Alb Link: Pforzheim to Bad Herrenalb", "panoramatour", False),
        31: ("weitere", "Odenwald-Main-Buslinie: Eberbach → Würzburg Hbf", "Odenwald–Main Bus Line: Eberbach to Würzburg Hbf", "panoramatour", False),
        37: ("schwarzwald", "Schwarzwaldhochstraße: Achern → Oppenau → Ruhestein", "Black Forest High Road: Achern via Oppenau to Ruhestein", "berg", True),
        38: ("odenwald", "Ried-Bergstraße Bus 610: Worms → Hüttenfeld → Bensheim", "Ried–Bergstraße Bus 610: Worms via Hüttenfeld to Bensheim", "panoramatour", False),
        39: ("pfaelzerwald", "Südpfalz-Buslinie: Wiesloch-Walldorf → Klingenmünster → Kandel", "South Palatinate Bus: Wiesloch-Walldorf via Klingenmünster to Kandel", "wein", True),
        66: ("pfaelzerwald", "Nordpfalz-Bahn/Bus: Bad Dürkheim → Kirchheimbolanden", "North Palatinate Link: Bad Dürkheim to Kirchheimbolanden", "wein", True),
        69: ("weitere", "Mainviereck-Bus: Miltenberg → Würzburg Hbf", "Mainviereck Bus: Miltenberg to Würzburg Hbf", "panoramatour", False),
        73: ("schwarzwald", "Nationalpark-Querung: Ottenhöfen → Hornisgrinde → Oppenau", "National Park Crossing: Ottenhöfen via Hornisgrinde to Oppenau", "berg", True),
        76: ("odenwald", "Spessart-Odenwald-Korridor: Miltenberg → Wildensee → Heidelberg", "Spessart–Odenwald Corridor: Miltenberg via Wildensee to Heidelberg", "panoramatour", False),
        77: ("odenwald", "Heidelberger Höhenbus: Neuenheim → Ziegelhausen Langer Kirschbaum", "Heidelberg Ridge Bus: Neuenheim to Ziegelhausen Langer Kirschbaum", "wanderung", False),
        81: ("schwarzwald", "Ortenau-Korridor: Kehl Rathaus → Offenburg → Bühl", "Ortenau Corridor: Kehl via Offenburg to Bühl", "stadt", False),
    }
    for idx, (reg, de_t, en_t, cat, esun) in p2p_meta.items():
        sc = by_idx[idx]
        locs = sc["locations"]
        vias = [via_entry(locs[k]) for k in ("viaLocation.0", "viaLocation.1") if k in locs]
        tours.append(
            make_shortcut_tour(
                tour_id=slugify(de_t, used_slugs),
                title_de=de_t,
                title_en=en_t,
                sub_de=f"Strecken-Korridor {locs['startLocation']['name']} → {locs['targetLocation']['name']}",
                sub_en=f"Point-to-point corridor {locs['startLocation']['name']} to {locs['targetLocation']['name']}",
                region=reg,
                category=cat,
                ring=False,
                evening_sun=esun,
                kind="shortcut_standalone",
                start_stop=locs["startLocation"],
                end_stop=locs["targetLocation"],
                vias=vias,
                hike=empty_hike(),
                raw_indices=[idx],
                min_change_time=sc["params"]["minChangeTime"],
                mode_hint=sc["params"]["modeHint"],
                bdepart=sc["params"]["bdepart"],
            )
        )

    # 8. Remaining 50 Unabsorbed Atomic GPX Tracks (source: "mapy")
    for fn in sorted(unique_tracks.keys()):
        if fn in absorbed_gpx:
            continue
        reg, de_t, en_t, cat, esun, sub_de, sub_en = GPX_META[fn]
        pts = unique_tracks[fn]
        hike = compute_hike_metrics(pts, [fn])
        raw_ring = haversine_m(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) <= 150.0
        s_stop, e_stop, s_d, e_d, u_rsn, eff_ring = resolve_gpx_endpoints(hike, raw_ring, by_idx, api)
        places = []
        for nm in ([s_stop["name"]] if s_stop else []) + ([e_stop["name"]] if e_stop else []):
            if nm and (not places or places[-1] != nm):
                places.append(nm)
        tours.append(
            {
                "id": slugify(de_t, used_slugs),
                "source": "mapy",
                "region": reg,
                "category": cat,
                "difficulty": difficulty_from_hike(hike),
                "ring": eff_ring,
                "title": {"de": de_t, "en": en_t},
                "subtitle": {"de": sub_de, "en": sub_en},
                "sun": {
                    "de": "Schöne Abendsonne an offenen Aussichtspunkten." if esun else "Ganztags gut begehbare Route.",
                    "en": "Great evening sun at open viewpoints." if esun else "Good all-day walking route.",
                },
                "tips": {
                    "de": "GPX-Track aus Olivers Mapy-Sammlung; Rückfahrtzeiten je nach Basis prüfen.",
                    "en": "GPX track from Oliver's Mapy collection; check return times for your base.",
                },
                "eveningSun": esun,
                "places": places or [de_t],
                "transit": {
                    "kind": "gpx_linked",
                    "startStop": s_stop,
                    "endStop": e_stop,
                    "vias": [],
                    "walkToStartM": s_d,
                    "walkFromEndM": e_d,
                    "unlinkedReason": u_rsn,
                },
                "hike": hike,
            }
        )

    # 9. 5 Additional Multi-Segment Chains (source: "mapy", id: "chain-*")
    for ch in CHAIN_DEFS:
        pts, files = merge_segments(ch["segments"], unique_tracks)
        hike = compute_hike_metrics(pts, files)
        if ch.get("lift"):
            # Only the segments are walked; the gap between them is ridden (chairlift) → not in km/Hm/walkMin.
            seg_d = seg_g = seg_l = 0.0
            for fn, rev in ch["segments"]:
                sp = list(reversed(unique_tracks[fn])) if rev else list(unique_tracks[fn])
                m = compute_hike_metrics(sp, [fn])
                seg_d += m["distKm"]
                seg_g += m["elevGainM"]
                seg_l += m["elevLossM"]
            hike["liftKm"] = round(hike["distKm"] - seg_d, 2)
            hike["distKm"] = round(seg_d, 2)
            hike["elevGainM"] = type(hike["elevGainM"])(round(seg_g))
            hike["elevLossM"] = type(hike["elevLossM"])(round(seg_l))
            hike["walkMin"] = calc_walk_min(hike["distKm"], hike["elevGainM"], hike["elevLossM"])
            # Höhenprofil nur über die Gehstrecke: die Liftfahrt zählt 0 km (senkrechter Sprung im Profil)
            prof, off = [], 0.0
            for fn, rev in ch["segments"]:
                sp = list(reversed(unique_tracks[fn])) if rev else list(unique_tracks[fn])
                m = compute_hike_metrics(sp, [fn])
                prof.extend([round(off + km, 2), ele] for km, ele in m["elevProfile"])
                off += m["distKm"]
            hike["elevProfile"] = prof
        s_stop, e_stop, s_d, e_d, u_rsn, eff_ring = resolve_gpx_endpoints(hike, ch["ring"], by_idx, api)
        places = []
        for nm in ([s_stop["name"]] if s_stop else []) + ([e_stop["name"]] if e_stop else []):
            if nm and (not places or places[-1] != nm):
                places.append(nm)
        cid = ch["id"]
        used_slugs.add(cid)
        tours.append(
            {
                "id": cid,
                "source": "mapy",
                "region": ch["region"],
                "category": ch["category"],
                "difficulty": difficulty_from_hike(hike),
                "ring": eff_ring,
                "title": ch["title"],
                "subtitle": ch["subtitle"],
                "sun": {
                    "de": "Abendsonne entlang der offenen Höhen." if ch["eveningSun"] else "Ganztags gut begehbare Kombi-Route.",
                    "en": "Evening sun along open ridges." if ch["eveningSun"] else "Good all-day combined route.",
                },
                "tips": ch["tips"],
                "eveningSun": ch["eveningSun"],
                "places": places or [ch["title"]["de"]],
                "transit": {
                    "kind": "gpx_linked",
                    "startStop": s_stop,
                    "endStop": e_stop,
                    "vias": [],
                    "walkToStartM": s_d,
                    "walkFromEndM": e_d,
                    "unlinkedReason": u_rsn,
                },
                "hike": hike,
            }
        )

    return tours


def main() -> None:
    api = RateLimitedAPI(CACHE_DIR)
    excluded, retained = parse_shortcuts(api)
    waypoints, unique_tracks, excluded_gpx = parse_waypoints_and_gpx()
    tours = build_all_tours(retained, unique_tracks, api)

    now_iso = datetime.now(ZoneInfo("Europe/Berlin")).isoformat(timespec="seconds")
    catalog = {
        "generated": now_iso,
        "waypoints": waypoints,
        "excludedGpx": excluded_gpx,
        "tours": tours,
    }

    (DATA_DIR / "excluded_shortcuts.json").write_text(
        json.dumps(excluded, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (DATA_DIR / "tours_catalog.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    unlinked = [t["id"] for t in tours if t["transit"]["unlinkedReason"]]
    print(
        f"Built catalog: {len(tours)} tours, {len(waypoints)} waypoints, "
        f"{len(excluded)} excluded shortcuts, {len(unlinked)} unlinked (>800m), "
        f"{api.net_calls} network requests."
    )


if __name__ == "__main__":
    main()
