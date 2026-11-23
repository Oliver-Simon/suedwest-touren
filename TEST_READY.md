# TEST_READY — Südwest-Touren (Jugenheim & Karlsruhe)

**Status**: READY (Opaque-Box E2E Suite Verified)
**Timestamp**: 2026-11-23T14:18:18Z
**Test Framework**: Python 3.12.3 `unittest` + `playwright` (Chromium `412×915` mobile harness) + `PIL` + `bs4` + `requests` + `xml.etree.ElementTree`

## Test Suite Inventory (`289` Tests across `N = 25` Features)

| Tier | File | Count | Threshold | Status |
|---|---|---|---|---|
| Tier 1 (Feature Coverage) | `tests/test_tier1_coverage.py` | **125** (`T1-001`..`T1-125`) | `>= 5*N = 125` | Verified |
| Tier 2 (Boundary & Corner Cases) | `tests/test_tier2_boundaries.py` | **125** (`T2-001`..`T2-125`) | `>= 5*N = 125` | Verified |
| Tier 3 (Cross-Feature Pairwise) | `tests/test_tier3_pairwise.py` | **26** (`T3-001`..`T3-026`) | `>= N = 25` | Verified |
| Tier 4 (Real-World Scenarios) | `tests/test_tier4_scenarios.py` | **13** (`T4-001`..`T4-013`) | `>= max(5, N//2) = 12` | Verified |
| **Total** | `tests/run_e2e.py` | **289** | `>= 288` | **Ready** |

## Verification Gate Passed
- `python3 -m py_compile tests/*.py` -> Exit 0 (`PY_COMPILE_OK`)
- `python3 tests/run_e2e.py --list` -> Exit 0 (`TOTAL: 289 tests`)
- `python3 tests/run_e2e.py --self-check` -> Exit 0 (`0` opaque-box violations, `289` discovered, `20/20` isolated synthetic temp-dir smoke checks passed in `24.360s`, temp dir cleaned up)

## Execution Commands for Milestone M5
```bash
# Local pre-deployment acceptance (Tiers 1 -> 2 -> 3 -> 4)
python3 tests/run_e2e.py --tier all --target local --json-report /tmp/e2e_local.json

# Live GitHub Pages post-deployment acceptance
python3 tests/run_e2e.py --tier all --target live --live-url https://oliver-simon.github.io/suedwest-touren/ --json-report /tmp/e2e_live.json
```
