"""Compact frontend payload for the PWA / Android app (lossless for everything the UI reads).

data/data.json repeats full stop objects (name, coords, dhid, ibnr, remoteId, ...) and whole
segment structures in every alternative departure (alts_fwd / alts_rev / lastSafeReturn).
This module interns stops, segments and itineraries into lookup tables:

  DATA.stops = [[name, lat, lon, dhid, ibnr, remoteId], ...]
  DATA.segs  = [[kind, mode, line, lineName, op, dep, arr,
                 fromStop, fromPlatform, toStop, toPlatform, dest, transfer,
                 warnings | 0, intermediateStops | 0, polyIdx[]], ...]
               intermediateStops = [[stopIdx, arr, dep, platform], ...]
  DATA.its   = [[segIdx, ...], ...]            (one itinerary = list of segments)
  tour.plans[base][day] = {f: [itIdx...], r: [itIdx...], fi, ri, lsr, fb, reason}
      f / r  = departure windows / return options (chronological, deduplicated)
      fi / ri = index of the planner's recommended fwd / rev inside f / r
      lsr    = {dep, arr, minStayAchieved, i: itIdx}

template.html hydrates these lazily back into the original segment objects (see hydrateSeg()).
"""

from __future__ import annotations

import json
import math

TOUR_KEYS = (
    "id", "source", "region", "category", "difficulty", "ring", "title", "subtitle", "sun",
    "tips", "eveningSun", "places", "tag", "durationMin", "unroutable",
)
STOP_KEYS = ("name", "lat", "lon", "dhid", "ibnr", "remoteId")
HIKE_KEYS = ("hasGpx", "distKm", "elevGainM", "elevLossM", "walkMin", "poly5", "elevProfile")


def _num(v, nd=6):
    if v is None or v == "":
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


class _Interner:
    def __init__(self):
        self.items = []
        self.index = {}

    def add(self, item):
        key = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        idx = self.index.get(key)
        if idx is None:
            idx = len(self.items)
            self.index[key] = idx
            self.items.append(item)
        return idx


def _clean_id(v):
    s = str(v or "").strip()
    if not s or s.lower() in ("null", "none", "undefined") or "L=null" in s:
        return None
    return s


def _slim_stop(st):
    if not isinstance(st, dict):
        return None
    out = {k: st.get(k) for k in STOP_KEYS if st.get(k) not in (None, "")}
    if "lat" in out:
        out["lat"] = _num(out["lat"])
    if "lon" in out:
        out["lon"] = _num(out["lon"])
    if st.get("durationMin") is not None:
        out["durationMin"] = st["durationMin"]
    if st.get("stopName"):
        out["stopName"] = st["stopName"]
    return out


def _decode5(enc):
    out, i, lat, lng = [], 0, 0, 0
    while i < len(enc):
        for k in range(2):
            shift = res = 0
            while True:
                b = ord(enc[i]) - 63
                i += 1
                res |= (b & 31) << shift
                shift += 5
                if b < 32 or i >= len(enc):
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0:
                lat += d
            else:
                lng += d
        out.append((lat / 1e5, lng / 1e5))
    return out


def _dist_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(x)))


class PolyFixer:
    """Repairs segment → polyline references in data.json.

    planer/slim.py (before the fix) re-indexed legs that are shared between `fwd`/`rev` and one
    `alts_*` entry twice, so ~85-90 % of the recommended itineraries pointed at unrelated polylines.
    A leg is accepted if its polyline starts near the segment's `from` and ends near its `to` stop;
    otherwise the polyline of an identical, correctly mapped segment (same line + stops) is used, then
    any polyline with matching endpoints, else none (the UI draws a straight from→to line).
    """

    TOL_M = 1500
    SEARCH_M = 400

    def __init__(self, D):
        self.polys = D.get("polys") or []
        self.ends = []
        for enc in self.polys:
            pts = _decode5(enc) if enc else []
            self.ends.append((pts[0], pts[-1]) if len(pts) >= 2 else None)
        self.good = {}
        self.stats = {"ok": 0, "by_signature": 0, "by_endpoints": 0, "dropped": 0}
        for segs in self._all_segs(D):
            for s in segs:
                ps = self._polys(s)
                if ps and self._matches(s, ps):
                    self.good.setdefault(self._sig(s), ps)

    @staticmethod
    def _all_segs(D):
        for t in D.get("tours", []):
            for days in (t.get("plans") or {}).values():
                for p in (days or {}).values():
                    if not isinstance(p, dict):
                        continue
                    for key in ("fwd", "rev"):
                        yield p.get(key) or []
                    for key in ("alts_fwd", "alts_rev"):
                        for a in p.get(key) or []:
                            yield a.get("segs") if isinstance(a, dict) else a
                    if isinstance(p.get("lastSafeReturn"), dict):
                        yield p["lastSafeReturn"].get("segs") or []

    @staticmethod
    def _sig(s):
        fr, to = s.get("from") or {}, s.get("to") or {}
        return (s.get("kind"), s.get("line"), fr.get("name"), to.get("name"))

    @staticmethod
    def _polys(s):
        return [l.get("p") for l in (s.get("legs") or []) if isinstance(l, dict) and isinstance(l.get("p"), int)]

    @staticmethod
    def _pt(st):
        try:
            return (float(st["lat"]), float(st["lon"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _matches(self, s, ps):
        a, b = self._pt(s.get("from") or {}), self._pt(s.get("to") or {})
        ends = [self.ends[p] for p in ps if 0 <= p < len(self.ends)]
        if not a or not b or not ends or any(e is None for e in ends):
            return False
        return _dist_m(ends[0][0], a) < self.TOL_M and _dist_m(ends[-1][1], b) < self.TOL_M

    def fix(self, s):
        ps = self._polys(s)
        if not ps:
            return []
        if self._matches(s, ps):
            self.stats["ok"] += 1
            return ps
        good = self.good.get(self._sig(s))
        if good:
            self.stats["by_signature"] += 1
            return good
        a, b = self._pt(s.get("from") or {}), self._pt(s.get("to") or {})
        if a and b:
            best = None
            for i, e in enumerate(self.ends):
                if e is None:
                    continue
                d = _dist_m(e[0], a) + _dist_m(e[1], b)
                if d < 2 * self.SEARCH_M and (best is None or d < best[0]):
                    best = (d, i)
            if best:
                self.stats["by_endpoints"] += 1
                return [best[1]]
        self.stats["dropped"] += 1
        return []


class Compactor:
    def __init__(self, fixer=None):
        self.stops = _Interner()
        self.segs = _Interner()
        self.its = _Interner()
        self.fixer = fixer

    def stop(self, st):
        if not isinstance(st, dict) or not st.get("name"):
            return -1
        row = [
            st.get("name"),
            _num(st.get("lat")),
            _num(st.get("lon")),
            _clean_id(st.get("dhid")),
            _clean_id(st.get("ibnr")),
            _clean_id(st.get("remoteId")),
        ]
        while row and row[-1] is None:
            row.pop()
        return self.stops.add(row)

    def seg(self, s):
        inter = []
        for x in s.get("intermediateStops") or []:
            if not x or not x.get("name"):
                continue
            inter.append([self.stop(x), x.get("arr") or "", x.get("dep") or "", x.get("platform") or ""])
        warns = [{"type": w.get("type"), "text": w.get("text")} for w in (s.get("warnings") or []) if isinstance(w, dict)]
        if self.fixer is not None:
            polys = self.fixer.fix(s)
        else:
            polys = [l.get("p") for l in (s.get("legs") or []) if isinstance(l, dict) and l.get("p") is not None]
        fr = s.get("from") or {}
        to = s.get("to") or {}
        row = [
            s.get("kind") or "ride",
            s.get("mode") or "",
            s.get("line") or "",
            s.get("lineName") or "",
            s.get("op") or "",
            s.get("dep") or "",
            s.get("arr") or "",
            self.stop(fr),
            fr.get("platform") or "",
            self.stop(to),
            to.get("platform") or "",
            s.get("dest") or "",
            s.get("transfer") if s.get("transfer") is not None else -1,
            warns or 0,
            inter or 0,
            polys,
        ]
        return self.segs.add(row)

    def itin(self, segs):
        if isinstance(segs, dict):
            segs = segs.get("segs") or []
        if not isinstance(segs, list) or not segs:
            return None
        return self.its.add([self.seg(s) for s in segs])

    def plan(self, p):
        if not isinstance(p, dict):
            return None
        out = {}
        fwd_i = self.itin(p.get("fwd"))
        rev_i = self.itin(p.get("rev"))
        f = [i for i in (self.itin(a) for a in (p.get("alts_fwd") or [])) if i is not None]
        r = [i for i in (self.itin(a) for a in (p.get("alts_rev") or [])) if i is not None]
        if fwd_i is not None and fwd_i not in f:
            f.append(fwd_i)
        if rev_i is not None and rev_i not in r:
            r.append(rev_i)
        f = list(dict.fromkeys(f))
        r = list(dict.fromkeys(r))
        its = self.its.items
        segs = self.segs.items

        def first_dep(i):
            hhmm = segs[its[i][0]][5] or "99:99"
            try:
                h, m = (int(x) for x in hhmm.split(":")[:2])
            except ValueError:
                return 9999
            mins = h * 60 + m
            return mins + 1440 if mins < 240 else mins  # after-midnight returns sort last

        f.sort(key=first_dep)
        r.sort(key=first_dep)
        out["f"] = f
        out["r"] = r
        out["fi"] = f.index(fwd_i) if fwd_i in f else 0
        out["ri"] = r.index(rev_i) if rev_i in r else 0
        lsr = p.get("lastSafeReturn")
        if isinstance(lsr, dict) and lsr.get("dep"):
            li = self.itin(lsr.get("segs"))
            out["lsr"] = {
                "dep": lsr.get("dep"),
                "arr": lsr.get("arr"),
                "minStayAchieved": lsr.get("minStayAchieved"),
                "i": li if li is not None else -1,
            }
        if p.get("fallback"):
            out["fb"] = p.get("fallback")
        if p.get("reason"):
            out["reason"] = p.get("reason")
        return out

    def tour(self, t):
        out = {k: t[k] for k in TOUR_KEYS if k in t and t[k] not in (None, "", [], {})}
        tr = t.get("transit") or {}
        out["transit"] = {
            "kind": tr.get("kind"),
            "startStop": _slim_stop(tr.get("startStop")),
            "endStop": _slim_stop(tr.get("endStop")),
            "vias": [v for v in (_slim_stop(v) for v in (tr.get("vias") or [])) if v],
        }
        h = t.get("hike") or {}
        out["hike"] = {k: h.get(k) for k in HIKE_KEYS if h.get(k) not in (None, "", [])}
        plans = {}
        for base, days in (t.get("plans") or {}).items():
            plans[base] = {}
            for day, p in (days or {}).items():
                cp = self.plan(p)
                if cp is not None:
                    plans[base][day] = cp
        out["plans"] = plans
        return out


def compact(D, images=None, credits=None, verbose=False):
    """Return the compact frontend payload for data.json dict D.

    images: optional {tour_id: url-or-data-uri}; credits: optional {tour_id: {...}}.
    """
    c = Compactor(PolyFixer(D))
    tours = []
    for t in D.get("tours", []):
        ct = c.tour(t)
        if images and t["id"] in images:
            ct["img"] = images[t["id"]]
        if credits is not None:
            cr = credits.get(t["id"], {})
            ct["imgCredit"] = {k: cr.get(k, "") for k in ("artist", "license", "licenseUrl", "page")}
        tours.append(ct)
    if verbose:
        print("appdata: segment polylines", c.fixer.stats)
    return {
        "v": 2,
        "generated": D.get("generated"),
        "bases": D.get("bases"),
        "days": D.get("days"),
        "tours": tours,
        "polys": D.get("polys") or [],
        "stops": c.stops.items,
        "segs": c.segs.items,
        "its": c.its.items,
    }
