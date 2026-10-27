# STATUS.md — car-log-mcp-server

**Date:** 2026-10-27 · **Owner:** Asdale Dynamics, LLC · **License:** MIT

## What was built

A small MCP server for tracking your car(s), Python 3.10+ **stdlib only**
(no dependencies), speaking JSON-RPC 2.0 over stdio per the MCP convention.

`server.py` — 13 tools:

| Area | Tools |
| --- | --- |
| Garage | `add_car`, `list_cars`, `get_car` |
| Fuel | `add_fill` (auto-MPG from previous odometer), `mpg_summary` |
| Maintenance | `add_maintenance` (due-next via miles and/or months), `due_maintenance` (OVERDUE/OK) |
| Tires | `add_tire_note`, `list_tire_notes` (season swaps + current season) |
| Insurance | `set_car_insurance`, `check_insurance` (OK / DUE_SOON ≤30 days / EXPIRED) |
| Shops | `add_shop_visit` (costs and quotes, open quotes kept) |
| Dashboard | `car_summary` (spend totals, due items, insurance, tire season) |

Storage and safety features:

- Plain JSON storage (`data/car-log.json` by default), created and seeded with a
  **sample car** (`sample-civic`) on first run.
- **Path safety:** data paths must stay inside the base directory — traversal
  (`../`, stacked `..`), absolute paths, symlinks, embeds, and non-`.json`
  extensions are rejected; null bytes rejected.
- **Auto-backup:** every save rotates up to 5 backups; a corrupt data file falls
  back to the newest readable backup; writes are atomic (temp file + `os.replace`).
- `CAR_LOG_DATA_DIR` env var or positional arg to relocate storage.

## How to run

```bash
python3 server.py                 # stdio MCP server; data in ./data/car-log.json
python3 server.py my-log.json     # custom filename inside ./data/
CAR_LOG_DATA_DIR=/path python3 server.py   # custom base directory
```

Point your MCP client at `python3 /full/path/to/server.py` (README has a config
snippet). All results are JSON text over the usual MCP `tools/call` content format.

## Verification results

All run today, from a clean checkout state:

- `python3 -m unittest discover -s tests -v` → **51 tests, OK** (≈0.07 s, fully offline)
  - MPG math: basic, first-fill, rollback/same-odometer, zero/negative gallons, rounding
  - Interval math: due odometer/date, month clamping (Jan 31 → Feb 28, leap Feb 29), overdue/OK states
  - Insurance math: OK / DUE_SOON / EXPIRED boundaries
  - Security: 8 path-safety cases (traversal, deep traversal, absolute escape,
    non-json, empty, null byte, symlink rule)
  - Storage: round-trip, backup creation, 5-deep rotation cap, corrupt-file
    fallback, corrupt-with-no-backup raises, sample-car seeding
  - Server logic: 13 in-process scenarios (fill + mpg, due calc, insurance flows, summary totals)
  - Protocol: initialize, tools/list schema check, notifications, ping, unknown
    tool/method, tool-error wrapping (input errors never crash the server)
- Live stdio smoke test (real subprocess): initialize → tools/list → car_summary
  → add_fill all returned correct results; data file + backup appeared on disk; clean exit code 0.
- Lint sanity: `compileall` clean; unused import removed.

Bugs found and fixed during verification:
1. Sample seed used a shared dict reference (later writes mutated the template) → now deep-copied.
2. Fresh store existed only in memory until first mutation → now seeded and saved at startup.
3. Insurance reminder demo was 34 days out (status OK) → sample set to 19 days so DUE_SOON actually shows.
4. Stdio smoke test rewritten to batch I/O (interactive readline deadlocked under the unittest runner).

## Known gaps

- Single data file holds all cars — no multi-file or per-car export.
- Interval math uses meters-behind-the-wheel defaults; no "reset interval on tire swap" automation.
- No editing/deleting of existing entries (append-only logbook) — delete by hand in the JSON if needed.
- No recurring auto-logging (e.g. calendar reminders); `due_maintenance` is pull-only, called by the agent.
- MPG ignores mixed-units (US gallons assumed); no metric (L/100 km) support yet.
- No interactive MCP client was available to test with; verification was hand-rolled JSON-RPC,
  which mirrors what a real client sends.
