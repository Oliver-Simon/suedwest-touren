"""Timetabled middle stages for ÖPNV round trips (shortcut tours without GPX).

planner.py plans base → stop A (outbound) and stop B → base (return). For 360° rings and the
bus/train panorama tours, the part in between (A → vias → B, e.g. Koblenz → Limburg through the
Lahn valley) was never planned: no times on the tour page, a gap on the map, rings not closed.

This script queries Transitous (same router, cache and rate limit as planner.py) for every hop of
that middle chain over the whole day and writes data/midlegs.json:

    {"v": 1, "days": {...}, "tours": {tour_id: {base: {"chain": [stop...], "days": {day: [[itin, ...] per hop]}}}}}

Each itinerary is a list of segments in planner format; legs carry the encoded polyline as "p5".
data/data.json stays untouched – build_pwa.py merges the file into the app payload.

usage: python3 planer/midlegs.py [--only tour_id ...]
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import planner  # noqa: E402  (repo-root module)

OUT = ROOT / "data" / "midlegs.json"
KINDS = {"shortcut_360", "shortcut_standalone", "shortcut_pair"}
MIN_HOP_M = 800          # shorter hops are walked, not planned
MAX_ALTS = 18            # departures kept per hop and day
DEP_WINDOW = ("06:00", "20:30")
MAX_RIDES = 3
STOP_KEYS = ("name", "lat", "lon", "dhid", "remoteId", "platform")
SEG_KEYS = ("kind", "mode", "line", "lineName", "dep", "arr", "dest", "transfer")


def _dist_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (float(a["lat"]), float(a["lon"]), float(b["lat"]), float(b["lon"])))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _same(a, b):
    if not a or not b:
        return False
    if a.get("dhid") and a.get("dhid") == b.get("dhid"):
        return True
    return _dist_m(a, b) < 300


def middle_chain(tour, base):
    """Stops the traveller passes between outbound arrival (A) and return departure (B)."""
    tr = tour.get("transit") or {}
    if tr.get("kind") not in KINDS or (tour.get("hike") or {}).get("hasGpx"):
        return []
    a, b = planner.resolve_tour_endpoints(tour, base)
    vias = [v for v in (tr.get("vias") or []) if v and v.get("lat") is not None]
    ia = next((i for i, v in enumerate(vias) if _same(v, a)), -1)
    ib = next((i for i, v in enumerate(vias) if _same(v, b)), len(vias))
    inner = vias[ia + 1:ib] if ib > ia else []
    chain = [a] + [v for v in inner if not _same(v, a) and not _same(v, b)] + [b]
    # drop consecutive duplicates / tiny hops
    out = [chain[0]]
    for st in chain[1:]:
        if _dist_m(out[-1], st) >= MIN_HOP_M:
            out.append(st)
    return out if len(out) >= 2 else []


def _place(st):
    if st.get("transitousId"):
        return str(st["transitousId"]), False
    return f"{st['lat']},{st['lon']}", True


def hop_itineraries(router, x, y, day_key):
    date = planner.DAYS[day_key]
    fp, c1 = _place(x)
    tp, c2 = _place(y)
    valid = []
    for use_coords, f, t in ((c1 or c2, fp, tp), (True, f"{x['lat']},{x['lon']}", f"{y['lat']},{y['lon']}")):
        raw = router.query_transitous_raw(f, t, f"{date}T04:00:00Z", search_window=68400, use_coords_allowance=use_coords)
        for it in raw:
            parsed = router.parse_transitous_itinerary(it, x, y, date)
            if parsed:
                valid.append(parsed)
        if len({s[0]["dep"] for s in valid}) >= 4:
            break
    res = planner.dedupe_and_sort_alts(valid, max_alts=200)
    res = [it for it in res
           if DEP_WINDOW[0] <= (it[0].get("dep") or "") <= DEP_WINDOW[1]
           and sum(1 for s in it if s.get("kind") == "ride") <= MAX_RIDES]
    res.sort(key=lambda it: it[0].get("dep") or "")
    if len(res) > MAX_ALTS:          # thin out evenly over the day instead of cutting the evening
        step = len(res) / MAX_ALTS
        res = [res[int(i * step)] for i in range(MAX_ALTS)]
    out = []
    for itin in res:
        segs = []
        for seg in itin:
            slim = {k: seg.get(k) for k in SEG_KEYS if seg.get(k) not in (None, "")}
            for end in ("from", "to"):
                slim[end] = {k: (seg.get(end) or {}).get(k) for k in STOP_KEYS if (seg.get(end) or {}).get(k) not in (None, "")}
            p5s = []
            for sub in seg.get("legs") or []:
                i = sub.get("p")
                p5 = router.polys[i] if isinstance(i, int) and 0 <= i < len(router.polys) else sub.get("p5", "")
                if p5:
                    p5s.append(p5)
            slim["legs"] = [{"p5": x} for x in p5s]
            segs.append(slim)
        out.append(segs)
    return out


def main(argv):
    only = set(argv[argv.index("--only") + 1:]) if "--only" in argv else None
    D = json.loads((ROOT / "data" / "data.json").read_text(encoding="utf-8"))
    stop_meta = {}
    router = planner.DualEngineRouter(planner.CACHE_DIR, stop_meta)
    out = {"v": 2, "days": planner.DAYS, "tours": {}, "polys": []}
    if only and OUT.exists():
        print("--only: Datei wird komplett neu geschrieben, bitte ohne --only laufen lassen")
    n_hops = n_empty = 0
    for t in D["tours"]:
        if only and t["id"] not in only:
            continue
        per_base = {}
        for base in ("jugenheim", "karlsruhe"):
            chain = middle_chain(t, base)
            if not chain:
                continue
            days = {}
            for day in planner.DAYS:
                hops = []
                for x, y in zip(chain, chain[1:]):
                    its = hop_itineraries(router, x, y, day)
                    n_hops += 1
                    n_empty += not its
                    hops.append(its)
                days[day] = hops
            per_base[base] = {"chain": [{k: s.get(k) for k in ("name", "lat", "lon", "dhid")} for s in chain], "days": days}
        if per_base:
            out["tours"][t["id"]] = per_base
            first = next(iter(per_base.values()))
            print(f"{t['id']}: {' → '.join(s['name'] for s in first['chain'])} "
                  f"({', '.join(str(len(h)) for h in first['days']['wd'])} Abfahrten Mo–Fr)", flush=True)
    # polylines into a shared table, identical base chains stored once
    polys, pidx = list(out.get("polys") or []), {}
    for i, p5 in enumerate(polys):
        pidx[p5] = i
    for per_base in out["tours"].values():
        for base in list(per_base):
            for day_hops in (per_base[base].get("days") or {}).values():
                for its in day_hops:
                    for itin in its:
                        for seg in itin:
                            for lg in seg.get("legs") or []:
                                if "p5" in lg:
                                    p5 = lg.pop("p5")
                                    if p5 not in pidx:
                                        pidx[p5] = len(polys)
                                        polys.append(p5)
                                    lg["p"] = pidx[p5]
        if "jugenheim" in per_base and "karlsruhe" in per_base and per_base["karlsruhe"].get("days") == per_base["jugenheim"].get("days"):
            per_base["karlsruhe"] = {"ref": "jugenheim"}
    out["v"] = 2
    out["polys"] = polys
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(out['tours'])} Touren, {n_hops} Teilstrecken, davon {n_empty} ohne Verbindung → {OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
