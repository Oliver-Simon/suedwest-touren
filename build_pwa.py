"""Build the installable PWA (index.html + sw.js) and standalone single-file HTML from template.html and data/data.json.

usage: python3 build_pwa.py [template.html] [data/data.json] [/home/oliver/suedwest_touren.html]
"""
import base64
import datetime
import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _encode_poly5(coords):
    out = []
    prev_lat = 0
    prev_lon = 0
    for lat, lon in coords:
        ilat = int(round(lat * 1e5))
        ilon = int(round(lon * 1e5))
        for d in (ilat - prev_lat, ilon - prev_lon):
            v = ~(d << 1) if d < 0 else (d << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def _synth_data_from_catalog(cat_path):
    """Fallback synthetic data.json in memory if M3 data/data.json is not yet written."""
    cat = json.load(open(cat_path, encoding="utf-8"))
    bases = {
        "jugenheim": {
            "name": "Jugenheim, Bickenbacher Straße 24g",
            "stopName": "Jugenheim Bickenbacher Straße, Seeheim-Jugenheim",
            "lat": 49.753891,
            "lon": 8.629438,
            "dhid": "de:06432:24355",
            "vrnId": "124355",
            "rmvId": "3024355",
            "nearestRail": {"name": "Bickenbach (Bergstr)", "ibnr": "8000956", "lat": 49.7581, "lon": 8.6042},
        },
        "karlsruhe": {
            "name": "Karlsruhe Hbf",
            "stopName": "Karlsruhe Hbf",
            "lat": 48.993530,
            "lon": 8.401939,
            "dhid": "de:08212:89",
            "ibnr": "8000191",
        },
    }
    days = {"wd": "2026-11-24", "sa": "2026-11-28", "so": "2026-11-29"}
    polys = []
    poly_map = {}

    def add_poly(enc):
        if not enc:
            enc = _encode_poly5([(49.753891, 8.629438), (49.40, 8.68)])
        if enc not in poly_map:
            poly_map[enc] = len(polys)
            polys.append(enc)
        return poly_map[enc]

    tours = []
    for idx, t in enumerate(cat.get("tours", [])):
        t = dict(t)
        tr = t.get("transit") or {}
        ss = tr.get("startStop") or {"name": "Start", "lat": 49.45, "lon": 8.15, "dhid": "de:07332:100"}
        es = tr.get("endStop") or ss
        h = t.get("hike") or {}
        wmin = int(h.get("walkMin") or 0)
        stay_needed = max(90, wmin + 45)
        plans = {"jugenheim": {}, "karlsruhe": {}}
        for bkey, binfo in bases.items():
            mid_stop = (
                {"name": "Mannheim Hbf", "lat": 49.4793, "lon": 8.4698, "ibnr": "8000244", "platform": "3"}
                if bkey == "jugenheim"
                else {"name": "Landau (Pfalz) Hbf", "lat": 49.1965, "lon": 8.1261, "ibnr": "8000217", "platform": "2"}
            )
            p_leg1 = add_poly(_encode_poly5([(binfo["lat"], binfo["lon"]), (mid_stop["lat"], mid_stop["lon"])]))
            p_leg2 = add_poly(_encode_poly5([(mid_stop["lat"], mid_stop["lon"]), (float(ss.get("lat") or 49.4), float(ss.get("lon") or 8.2))]))
            p_rev1 = add_poly(_encode_poly5([(float(es.get("lat") or 49.4), float(es.get("lon") or 8.2)), (mid_stop["lat"], mid_stop["lon"])]))
            p_rev2 = add_poly(_encode_poly5([(mid_stop["lat"], mid_stop["lon"]), (binfo["lat"], binfo["lon"])]))

            def make_fwd(dep_h, dep_m):
                t0 = dep_h * 60 + dep_m
                t1 = t0 + 38
                t2 = t1 + 8
                t3 = t2 + 42
                fmt = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
                s1 = {
                    "kind": "ride",
                    "mode": "re" if bkey == "jugenheim" else "stadtbahn",
                    "line": "RE 60" if bkey == "jugenheim" else "S 51",
                    "lineName": "RE 60" if bkey == "jugenheim" else "S 51",
                    "op": "DB Regio",
                    "dep": fmt(t0),
                    "arr": fmt(t1),
                    "from": {"name": binfo["stopName"], "lat": binfo["lat"], "lon": binfo["lon"], "dhid": binfo["dhid"], "ibnr": binfo.get("ibnr") or "8000956", "platform": "1"},
                    "to": dict(mid_stop),
                    "dest": mid_stop["name"],
                    "transfer": 8,
                    "intermediateStops": [
                        {"name": "Bensheim" if bkey == "jugenheim" else "Wörth (Rhein)", "arr": fmt(t0 + 15), "dep": fmt(t0 + 16), "platform": "2", "lat": (binfo["lat"] + mid_stop["lat"]) / 2, "lon": (binfo["lon"] + mid_stop["lon"]) / 2}
                    ],
                    "warnings": [],
                    "legs": [{"m": "re" if bkey == "jugenheim" else "stadtbahn", "l": "RE 60" if bkey == "jugenheim" else "S 51", "d": fmt(t0), "a": fmt(t1), "dist": 35000, "p": p_leg1}],
                }
                s2 = {
                    "kind": "ride",
                    "mode": "bus" if idx % 2 == 0 else "rb",
                    "line": "Bus 500" if idx % 2 == 0 else "RB 53",
                    "lineName": "Bus 500" if idx % 2 == 0 else "RB 53",
                    "op": "VRN / DB Regio",
                    "dep": fmt(t2),
                    "arr": fmt(t3),
                    "from": dict(mid_stop),
                    "to": {"name": ss["name"], "lat": float(ss.get("lat") or 49.4), "lon": float(ss.get("lon") or 8.2), "dhid": ss.get("dhid"), "ibnr": ss.get("ibnr"), "remoteId": ss.get("remoteId"), "platform": "2"},
                    "dest": ss["name"],
                    "intermediateStops": [
                        {"name": "Neustadt (Weinstr) Hbf", "arr": fmt(t2 + 20), "dep": fmt(t2 + 21), "platform": "4", "lat": float(ss.get("lat") or 49.4), "lon": float(ss.get("lon") or 8.2)}
                    ],
                    "warnings": [{"type": "seasonal", "text": "Saison-/Ausflugsverkehr: Fahrplan vorab prüfen"}] if idx == 0 else [],
                    "legs": [{"m": "bus" if idx % 2 == 0 else "rb", "l": "Bus 500" if idx % 2 == 0 else "RB 53", "d": fmt(t2), "a": fmt(t3), "dist": 25000, "p": p_leg2}],
                }
                return [s1, s2], t3

            def make_rev(dep_min):
                t0 = dep_min
                t1 = t0 + 42
                t2 = t1 + 8
                t3 = t2 + 38
                fmt = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
                r1 = {
                    "kind": "ride",
                    "mode": "rb",
                    "line": "RB 53",
                    "lineName": "RB 53",
                    "op": "DB Regio",
                    "dep": fmt(t0),
                    "arr": fmt(t1),
                    "from": {"name": es["name"], "lat": float(es.get("lat") or 49.4), "lon": float(es.get("lon") or 8.2), "dhid": es.get("dhid"), "ibnr": es.get("ibnr"), "remoteId": es.get("remoteId"), "platform": "1"},
                    "to": dict(mid_stop),
                    "dest": mid_stop["name"],
                    "transfer": 8,
                    "intermediateStops": [],
                    "warnings": [],
                    "legs": [{"m": "rb", "l": "RB 53", "d": fmt(t0), "a": fmt(t1), "dist": 25000, "p": p_rev1}],
                }
                r2 = {
                    "kind": "ride",
                    "mode": "re" if bkey == "jugenheim" else "stadtbahn",
                    "line": "RE 60" if bkey == "jugenheim" else "S 51",
                    "lineName": "RE 60" if bkey == "jugenheim" else "S 51",
                    "op": "DB Regio",
                    "dep": fmt(t2),
                    "arr": fmt(t3),
                    "from": dict(mid_stop),
                    "to": {"name": binfo["stopName"], "lat": binfo["lat"], "lon": binfo["lon"], "dhid": binfo["dhid"], "ibnr": binfo.get("ibnr") or "8000956", "platform": "1"},
                    "dest": binfo["stopName"],
                    "intermediateStops": [],
                    "warnings": [],
                    "legs": [{"m": "re" if bkey == "jugenheim" else "stadtbahn", "l": "RE 60" if bkey == "jugenheim" else "S 51", "d": fmt(t2), "a": fmt(t3), "dist": 35000, "p": p_rev2}],
                }
                return [r1, r2], fmt(t0), fmt(t3)

            base_offset = 0 if bkey == "jugenheim" else 12
            f1, arr1 = make_fwd(8, 4 + base_offset)
            f2, arr2 = make_fwd(10, 4 + base_offset)
            f3, arr3 = make_fwd(13, 14 + base_offset)
            f4, arr4 = make_fwd(15, 44 + base_offset)
            r1, _, _ = make_rev(arr1 + stay_needed)
            r2, _, _ = make_rev(arr2 + stay_needed)
            r3, _, _ = make_rev(arr3 + stay_needed)
            r4, lsr_dep, lsr_arr = make_rev(max(arr4 + stay_needed, 20 * 60 + 15))

            for dkey in ("wd", "sa", "so"):
                plans[bkey][dkey] = {
                    "fwd": f1,
                    "rev": r1,
                    "alts_fwd": [f1, f2, f3, f4],
                    "alts_rev": [r1, r2, r3, r4],
                    "lastSafeReturn": {"dep": lsr_dep, "arr": lsr_arr, "minStayAchieved": stay_needed, "segs": r4},
                    "fallback": False,
                }
        t["durationMin"] = 360
        t["plans"] = plans
        t["unroutable"] = {"jugenheim": {}, "karlsruhe": {}}
        tours.append(t)

    return {
        "generated": cat.get("generated") or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "bases": bases,
        "days": days,
        "waypoints": cat.get("waypoints") or [],
        "tours": tours,
        "freq": {},
        "polys": polys,
    }


def main():
    tpl_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "template.html")
    data_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "data", "data.json")
    standalone = sys.argv[3] if len(sys.argv) > 3 else "/home/oliver/suedwest_touren.html"
    vendor = os.path.join(HERE, "vendor")

    tpl = open(tpl_path, encoding="utf-8").read()
    css = re.sub(r"url\(images/[^)]*\)", "none", open(os.path.join(vendor, "leaflet.css"), encoding="utf-8").read())
    js = open(os.path.join(vendor, "leaflet.js"), encoding="utf-8").read()

    if os.path.exists(data_path) and os.path.getsize(data_path) > 100:
        D = json.load(open(data_path, encoding="utf-8"))
    else:
        cat_full = os.path.join(HERE, "data", "tours_catalog_full.json")
        print(f"[build_pwa] {data_path} not ready yet; using fallback from {cat_full}")
        D = _synth_data_from_catalog(cat_full)

    import io
    from PIL import Image

    credits_path = os.path.join(HERE, "img_src", "credits.json")
    credits = json.load(open(credits_path, encoding="utf-8")) if os.path.exists(credits_path) else {}
    for t in D.get("tours", []):
        f = os.path.join(HERE, "img", t["id"] + ".jpg")
        if os.path.exists(f):
            try:
                with Image.open(f) as im:
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, format="JPEG", quality=48, optimize=True)
                    raw_bytes = buf.getvalue()
            except Exception:
                raw_bytes = open(f, "rb").read()
            t["img"] = "data:image/jpeg;base64," + base64.b64encode(raw_bytes).decode()
            c = credits.get(t["id"], {})
            t["imgCredit"] = {k: c.get(k, "") for k in ("file", "artist", "license", "licenseUrl", "page")}

    data = json.dumps(D, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = tpl.replace("/*LEAFLET_CSS*/", css).replace("/*LEAFLET_JS*/", js).replace("/*DATA*/", data)

    if standalone:
        open(standalone, "w", encoding="utf-8").write(page)
        print(standalone, os.path.getsize(standalone), "bytes")

    head = (
        '<link rel="manifest" href="manifest.json">\n'
        '<link rel="apple-touch-icon" href="icon-192.png">\n</head>'
    )
    reg = (
        '<script>if("serviceWorker" in navigator && /^https?:$/.test(location.protocol))'
        'navigator.serviceWorker.register("sw.js").catch(function(){});</script>\n</body>'
    )
    pwa = page.replace("</head>", head, 1)
    pwa = pwa[::-1].replace("</body>"[::-1], reg[::-1], 1)[::-1]
    index_path = os.path.join(HERE, "index.html")
    open(index_path, "w", encoding="utf-8").write(pwa)

    version = "suedwest-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    sw = open(os.path.join(HERE, "sw.template.js"), encoding="utf-8").read().replace("__VERSION__", version)
    open(os.path.join(HERE, "sw.js"), "w", encoding="utf-8").write(sw)
    print("index.html", os.path.getsize(index_path), "bytes,", version)


if __name__ == "__main__":
    main()
