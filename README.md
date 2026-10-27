# car-log-mcp-server

A small MCP (Model Context Protocol) server for your car(s), written with the
Python standard library only — no dependencies, no accounts, no cloud. It talks
JSON-RPC over stdio, so any MCP client (Claude Desktop, Claude Code, opencode,
) can drive it.

Track, in plain local JSON:

- **Fuel fill-ups** with automatic **MPG** math (needs two fill-ups; back-to-back compare)
- **Maintenance events** with **due-next intervals** (miles and/or months → "due at 65,100 mi / due 2027-02-15")
- **Tire season notes** — when you swapped to winter/summer and your notes for next swap
- **Insurance expiry reminders** — OK / DUE_SOON / EXPIRED
- **Repair-shop visits** with **costs and quotes** (open quotes tracked)

A sample car (`sample-civic`) is included out of the box so you can explore every
tool before logging anything of your own.

Complements two friends without overlap:

| Repo | Covers |
| --- | --- |
| `home-maintenance-mcp-server` | the house |
| `free-car-care-pack` | printable car-care checklists |
| **`car-log-mcp-server`** (this repo) | the car's actual logbook, reads/written by an AI agent |

By **Asdale Dynamics, LLC** — beginner-friendly AI education and practical tools.

## Requirements

- Python 3.10+
- Nothing else. `server.py` uses only the standard library.

## Run it (smoke test in 60 seconds)

```bash
# 1) run the offline test suite (51 tests: mpg math, interval math, security, protocol)
python3 -m unittest discover -s tests -v

# 2) talk to the server over stdio by hand
python3 - <<'EOF'
import json, subprocess, sys
reqs = "\n".join([
    json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}),
    json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
    json.dumps({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
        "name":"mpg_summary","arguments":{"car_id":"sample-civic"}}}),
]) + "\n"
proc = subprocess.run([sys.executable, "server.py"], input=reqs,
                      capture_output=True, text=True, timeout=30)
for line in proc.stdout.splitlines():
    r = json.loads(line)
    print(r["id"], "->", json.dumps(r.get("result", r.get("error")))[:120])
EOF
```

Data will appear in `./data/car-log.json` (created on first run, sample car
seeded). Custom locations:

```bash
python3 server.py my-car-log.json          # file inside ➜ <cwd>/data/
CAR_LOG_DATA_DIR=~/car-data python3 server.py   # custom base directory
```

## Wire it into your MCP client

Add to your MCP client config (e.g. Claude Desktop `claude_desktop_config.json`
or opencode's `mcp` config):

```json
{
  "mcpServers": {
    "car-log": {
      "command": "python3",
      "args": "/full/path/to/car-log-mcp-server/server.py"
    }
  }
}
```

## Tools

| Tool | What it does |
| --- | --- |
| `add_car` / `list_cars` / `get_car` | create and browse your garage |
| `add_fill` | log a fill-up; MPG auto-computed from the previous odometer reading |
| `mpg_summary` | average / best / worst MPG |
| `add_maintenance` | log an event with `interval_miles` and/or `interval_months`; due-next is computed |
| `due_maintenance` | what's due or overdue right now (compares both odometer and calendar) |
| `add_tire_note` / `list_tire_notes` | season swaps (winter/summer/all-season) + current season |
| `set_car_insurance` / `check_insurance` | policy on file; OK / DUE_SOON (≤30 days) / EXPIRED |
| `add_shop_visit` | shop visits — cost when done, `quote` when it's still a quote |
| `car_summary` | one-car dashboard: spend totals, due items, insurance status, tire season |

All tool output is human-readable JSON text; the assistant can read it and answer
in plain English ("Your oil change is overdue by 320 miles").

## Data, safety, and privacy

- **Local-first:** everything lives in one JSON file on your disk. No network, no secrets.
- **Path safety:** the data path can never escape its base directory — `..`,
  traversal chains, absolute system paths, symlinks, and non-`.json` names are rejected.
- **Auto-backup:** every save rotates up to 5 backups (`car-log.1.bak` … `car-log.5.bak`);
  a corrupt data file automatically falls back to the newest readable backup.
- **Atomic writes:** the data file is written to a temp file then moved into place, so a
  crash mid-write can't destroy your logbook.

## Project layout

```
server.py            the MCP server (run this)
tests/test_carlog.py offline unit tests (51): MPG math, interval math,
                     insurance reminder math, path-safety security cases,
                     backup rotation, protocol handling, live stdio smoke test
data/car-log.json    created at first run — your data (backup rotation lives next to it)
```

## Books in the same spirit

See *The Massive AI Manual* and dylanasdale.github.io for AI explained in plain
English, step by step, never condescending.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Asdale Dynamics, LLC.
