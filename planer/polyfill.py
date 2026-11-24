"""Fill in missing segment geometry.

A few rides in data/data.json lost their polyline (see PolyFixer in planer/appdata.py: "dropped").
The app then had nothing to draw – gaps in the round trips, or (earlier) straight lines across the map.
This script asks Transitous for the very same ride (line, stops, departure) and stores its geometry in
data/polyfill.json: {"v": 1, "fill": {"kind|line|from|to": "<poly5>"}}. build_pwa.py → appdata uses it
for segments that would otherwise be dropped. data/data.json stays untouched.

usage: python3 planer/polyfill.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import planner  # noqa: E402
from planer.appdata import PolyFixer, _dist_m  # noqa: E402

OUT = ROOT / "data" / "polyfill.json"
TOL_M = 1500


def plan_segs(D):
    for t in D.get("tours", []):
        for base, days in (t.get("plans") or {}).items():
            for day, p in (days or {}).items():
                if not isinstance(p, dict):
                    continue
                lists = [p.get("fwd") or [], p.get("rev") or []]
                for key in ("alts_fwd", "alts_rev"):
                    lists += [(a.get("segs") if isinstance(a, dict) else a) or [] for a in (p.get(key) or [])]
                if isinstance(p.get("lastSafeReturn"), dict):
                    lists.append(p["lastSafeReturn"].get("segs") or [])
                for segs in lists:
                    for s in segs:
                        yield day, s


def dropped(fx, s):
    ps = fx._polys(s)
    if ps and fx._matches(s, ps):
        return False
    if fx.good.get(fx._sig(s)):
        return False
    a, b = fx._pt(s.get("from") or {}), fx._pt(s.get("to") or {})
    if not a or not b:
        return False
    for e in fx.ends:
        if e is not None and _dist_m(e[0], a) + _dist_m(e[1], b) < 2 * fx.SEARCH_M:
            return False
    return True


def key_of(s):
    fr, to = s.get("from") or {}, s.get("to") or {}
    return "|".join(str(x) for x in (s.get("kind"), s.get("line"), fr.get("name"), to.get("name")))


def utc_of(date, hhmm, minus=3):
    h, m = map(int, hhmm.split(":"))
    total = h * 60 + m - 60 - minus          # CET (UTC+1) in late November
    return f"{date}T{max(0, total) // 60:02d}:{max(0, total) % 60:02d}:00Z"


def main():
    D = json.loads((ROOT / "data" / "data.json").read_text(encoding="utf-8"))
    fx = PolyFixer(D)
    todo = {}
    for day, s in plan_segs(D):
        if s.get("kind") != "ride" or not s.get("dep"):
            continue
        if dropped(fx, s):
            todo.setdefault(key_of(s), (day, s))
    print(f"{len(todo)} Fahrten ohne Streckenverlauf")
    router = planner.DualEngineRouter(planner.CACHE_DIR, {})
    fill = {}
    for k, (day, s) in todo.items():
        fr, to = s["from"], s["to"]
        date = planner.DAYS[day]
        raw = router.query_transitous_raw(f"{fr['lat']},{fr['lon']}", f"{to['lat']},{to['lon']}",
                                          utc_of(date, s["dep"]), search_window=2400, use_coords_allowance=True)
        found = None
        for it in raw:
            segs = router.parse_transitous_itinerary(it, fr, to, date) or []
            for cand in segs:
                if cand.get("kind") != "ride" or (cand.get("line") or "") != (s.get("line") or ""):
                    continue
                cf, ct = cand.get("from") or {}, cand.get("to") or {}
                try:
                    if _dist_m((cf["lat"], cf["lon"]), (fr["lat"], fr["lon"])) > TOL_M or _dist_m((ct["lat"], ct["lon"]), (to["lat"], to["lon"])) > TOL_M:
                        continue
                except (KeyError, TypeError):
                    continue
                p5 = "".join(router.polys[l["p"]] for l in (cand.get("legs") or [])[:1] if isinstance(l.get("p"), int))
                if p5:
                    found = p5
                    break
            if found:
                break
        print(("✓ " if found else "– ") + k, flush=True)
        if found:
            fill[k] = found
    OUT.write_text(json.dumps({"v": 1, "fill": fill}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(fill)}/{len(todo)} ergänzt → {OUT}")


if __name__ == "__main__":
    main()
