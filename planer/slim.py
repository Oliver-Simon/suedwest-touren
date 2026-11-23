#!/usr/bin/env python3
"""Shrink data/data.json: deduplicate/cap departure alternatives & compact polyline table.

Can be imported by `planner.py` (`slim_data_dict(D)`) or run standalone:
    python3 planer/slim.py [data/data.json] [data/data.json]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def parse_hhmm(val: Any) -> int:
    if not val:
        return 0
    s = str(val).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0][-2:]) * 60 + int(parts[1][:2])
        except ValueError:
            return 0
    return 0


def segs_of(item: Any) -> list[dict[str, Any]]:
    if isinstance(item, dict):
        return item.get("segs") or []
    if isinstance(item, list):
        return item
    return []


def alt_sig(segs: list[dict[str, Any]]) -> tuple[ Any, ...]:
    if not segs:
        return ("", "", ())
    return (
        segs[0].get("dep", ""),
        segs[-1].get("arr", ""),
        tuple(s.get("line") for s in segs if s.get("kind") == "ride"),
    )


def dur_min(segs: list[dict[str, Any]]) -> int:
    if not segs:
        return 9999
    d0 = parse_hhmm(segs[0].get("dep"))
    d1 = parse_hhmm(segs[-1].get("arr"))
    diff = d1 - d0
    if diff < 0:
        diff += 1440
    return diff


def dedupe_and_sort_alts(
    alts: list[Any],
    main: list[dict[str, Any]] | None = None,
    max_alts: int = 8,
) -> list[list[dict[str, Any]]]:
    """Deduplicate by (dep, arr, ride_lines) and sort strictly ascending by dep."""
    raw_list: list[list[dict[str, Any]]] = []
    if main:
        raw_list.append(main)
    for a in alts or []:
        s = segs_of(a)
        if s:
            raw_list.append(s)
    if not raw_list:
        return []

    seen: set[tuple[Any, ...]] = set()
    unique: list[list[dict[str, Any]]] = []
    # Sort first by departure time, then by duration (shortest wins on exact tie)
    raw_list.sort(key=lambda s: (parse_hhmm(s[0].get("dep")), dur_min(s)))
    for s in raw_list:
        sig = alt_sig(s)
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(s)

    if len(unique) > max_alts:
        main_sig = alt_sig(main) if main else None
        # Ensure we keep diverse departure windows (morning, mid-morning, midday, afternoon)
        # plus the main itinerary
        buckets: dict[int, list[dict[str, Any]]] = {}
        kept: list[list[dict[str, Any]]] = []
        kept_sigs: set[tuple[Any, ...]] = set()
        if main and main_sig:
            kept.append(main)
            kept_sigs.add(main_sig)
        for s in unique:
            sig = alt_sig(s)
            if sig in kept_sigs:
                continue
            dep_h = parse_hhmm(s[0].get("dep")) // 90
            if dep_h not in buckets:
                buckets[dep_h] = s
        for _, s in sorted(buckets.items()):
            if len(kept) >= max_alts:
                break
            sig = alt_sig(s)
            if sig not in kept_sigs:
                kept.append(s)
                kept_sigs.add(sig)
        for s in unique:
            if len(kept) >= max_alts:
                break
            sig = alt_sig(s)
            if sig not in kept_sigs:
                kept.append(s)
                kept_sigs.add(sig)
        unique = sorted(kept, key=lambda s: parse_hhmm(s[0].get("dep")))

    return unique


def slim_data_dict(D: dict[str, Any], max_alts: int = 4) -> dict[str, Any]:
    """In-place deduplicate/cap alternatives, compact stop/segment payloads, and compact `D["polys"]`."""
    removed = 0
    for tour in D.get("tours", []):
        plans = tour.get("plans") or {}
        for base_plans in plans.values():
            if not isinstance(base_plans, dict):
                continue
            for day_plan in base_plans.values():
                if not isinstance(day_plan, dict) or day_plan.get("fallback"):
                    continue
                for alt_key, main_key in (("alts_fwd", "fwd"), ("alts_rev", "rev")):
                    old_alts = day_plan.get(alt_key) or []
                    main_segs = day_plan.get(main_key)
                    new_alts = dedupe_and_sort_alts(old_alts, main=main_segs, max_alts=max_alts)
                    removed += max(0, len(old_alts) - len(new_alts))
                    day_plan[alt_key] = new_alts

    # Re-index and compact polylines table + strip redundant bloat from segments
    old_polys: list[str] = D.get("polys") or []
    poly_str_to_new_idx: dict[str, int] = {}
    new_polys: list[str] = []

    def compact_stop(st: Any) -> Any:
        if not isinstance(st, dict):
            return st
        out = {}
        for k, v in st.items():
            if v is None or v == "":
                continue
            if k in ("lat", "lon") and isinstance(v, float):
                out[k] = round(v, 5)
            else:
                out[k] = v
        return out

    def remap_segs(segs: list[dict[str, Any]] | None, keep_inter: bool = True, protected_ids: set[int] | None = None) -> None:
        for s in segs or []:
            if "from" in s:
                s["from"] = compact_stop(s["from"])
            if "to" in s:
                s["to"] = compact_stop(s["to"])
            for k in ("warnings", "note", "stayNote", "dest", "lineName", "op"):
                if k in s and (s[k] is None or s[k] == "" or s[k] == []):
                    s.pop(k, None)
            inter = s.get("intermediateStops")
            if inter is not None:
                if keep_inter or (protected_ids is not None and id(s) in protected_ids):
                    if inter:
                        s["intermediateStops"] = [
                            {k: st[k] for k in ("name", "arr", "dep", "platform") if st.get(k)}
                            for st in inter
                            if isinstance(st, dict) and st.get("name")
                        ]
                    else:
                        s.pop("intermediateStops", None)
                else:
                    s.pop("intermediateStops", None)
            for leg in s.get("legs") or []:
                old_idx = leg.get("p")
                leg.pop("p5", None)
                if isinstance(old_idx, int) and 0 <= old_idx < len(old_polys):
                    p_str = old_polys[old_idx]
                else:
                    p_str = ""
                if p_str not in poly_str_to_new_idx:
                    poly_str_to_new_idx[p_str] = len(new_polys)
                    new_polys.append(p_str)
                leg["p"] = poly_str_to_new_idx[p_str]

    for tour in D.get("tours", []):
        plans = tour.get("plans") or {}
        for base_plans in plans.values():
            if not isinstance(base_plans, dict):
                continue
            for day_plan in base_plans.values():
                if not isinstance(day_plan, dict):
                    continue
                protected: set[int] = set()
                for s in (day_plan.get("fwd") or []) + (day_plan.get("rev") or []):
                    protected.add(id(s))
                remap_segs(day_plan.get("fwd"), keep_inter=True, protected_ids=protected)
                remap_segs(day_plan.get("rev"), keep_inter=True, protected_ids=protected)
                for a in day_plan.get("alts_fwd") or []:
                    remap_segs(segs_of(a), keep_inter=False, protected_ids=protected)
                for a in day_plan.get("alts_rev") or []:
                    remap_segs(segs_of(a), keep_inter=False, protected_ids=protected)
                lsr = day_plan.get("lastSafeReturn")
                if isinstance(lsr, dict):
                    remap_segs(lsr.get("segs"), keep_inter=False, protected_ids=protected)

    D["polys"] = new_polys
    return D


def main() -> int:
    default_path = Path(__file__).resolve().parent.parent / "data" / "data.json"
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src
    if not src.exists():
        print(f"File not found: {src}", file=sys.stderr)
        return 1
    before_bytes = src.stat().st_size
    D = json.loads(src.read_text(encoding="utf-8"))
    old_poly_cnt = len(D.get("polys") or [])
    slim_data_dict(D)
    dst.write_text(json.dumps(D, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    after_bytes = dst.stat().st_size
    print(
        f"slim.py: polys {old_poly_cnt} -> {len(D['polys'])}, "
        f"bytes {before_bytes} -> {after_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
