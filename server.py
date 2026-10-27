#!/usr/bin/env python3
"""car-log-mcp-server

A small Model Context Protocol (MCP) server over stdio, built with the
Python standard library only. It tracks your car(s):

  - fuel fill-ups with automatic MPG math
  - maintenance events with "due next" intervals (miles/months)
  - tire season notes (when you swapped to winter/summer tires)
  - insurance expiry reminders
  - repair-shop visits with costs and quotes

Data is stored as plain JSON (local-first, no cloud, no secrets) with
path safety and an automatic rotating backup on every save.

Run:   python3 server.py            (stdio; point your MCP client at this)
Test:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage with path safety + auto-backup
# ---------------------------------------------------------------------------

BACKUP_KEEP = 5


def resolve_safe_path(base_dir: str, user_path: str) -> Path:
    """Resolve ``user_path`` inside ``base_dir``; raise ValueError if it
    escapes the base directory (path traversal), is a symlink escape,
    or is not a ``.json`` file."""
    if not isinstance(user_path, str) or not user_path.strip():
        raise ValueError("data path must be a non-empty string")
    if "\x00" in user_path:
        raise ValueError("data path contains a null byte")
    base = Path(base_dir).expanduser().resolve()
    candidate = (base / user_path).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"path escapes the data directory: {user_path!r}")
    if candidate.is_symlink():
        raise ValueError("symlinked data paths are not allowed")
    if candidate.suffix != ".json":
        raise ValueError("data file must have a .json extension")
    return candidate


class Storage:
    """JSON file storage with automatic rotating backups."""

    def __init__(self, base_dir: str, data_path: str):
        self.base = Path(base_dir).expanduser().resolve()
        self.path = resolve_safe_path(str(self.base), data_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file: fall back to newest backup.
            for bak in sorted(self._backups(), reverse=True):
                try:
                    with open(bak, "r", encoding="utf-8") as fh:
                        return json.load(fh)
                except (json.JSONDecodeError, OSError):
                    continue
            raise ValueError(
                f"data file {self.path.name} is corrupt and no usable backup exists"
            ) from None
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self._rotate_backups()
        if self.path.exists():
            shutil.copy2(self.path, self._backup_name(1))
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def _backup_name(self, n: int) -> Path:
        return self.path.with_name(f"{self.path.stem}.{n}.bak")

    def _backups(self) -> list[Path]:
        return [self._backup_name(n) for n in range(1, BACKUP_KEEP + 1)]

    def _rotate_backups(self) -> None:
        for n in range(BACKUP_KEEP, 1, -1):
            src, dst = self._backup_name(n - 1), self._backup_name(n)
            if src.exists():
                os.replace(src, dst)
            else:
                dst.unlink(missing_ok=True)


def rotate_backups_for_tests(
    backups: list[Path], keep: int = BACKUP_KEEP
) -> None:
    """Pure rotation logic exposed for unit tests."""
    for n in range(keep, 1, -1):
        src, dst = backups[n - 2], backups[n - 1]
        if src.exists():
            os.replace(src, dst)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def parse_date(value: Any, field: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a YYYY-MM-DD string")
    try:
        y, m, d = value.strip().split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a valid YYYY-MM-DD date, got {value!r}") from None


def add_months(d: date, months: int) -> date:
    month_index = (d.month - 1) + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def days_until(target: date, today: date | None = None) -> int:
    return (target - (today or date.today())).days


# ---------------------------------------------------------------------------
# Domain math
# ---------------------------------------------------------------------------


def compute_mpg(prev_odometer: float, odometer: float, gallons: float) -> float | None:
    """MPG for this fill-up. Returns None when it cannot be computed
    (first fill-up, lower odometer rollback, or non-positive gallons)."""
    if gallons is None or prev_odometer is None or odometer is None:
        return None
    if gallons <= 0 or odometer <= prev_odometer:
        return None
    return round((odometer - prev_odometer) / gallons, 2)


def interval_due(
    event_date: str, last_odometer: float, interval_miles: float,
    interval_months: int, today: str | None = None,
) -> dict[str, Any]:
    """Compute when a maintenance event is due next."""
    when = parse_date(event_date, "event_date")
    as_of = parse_date(today, "today") if today else date.today()
    due_odometer = round(last_odometer + interval_miles, 1) if interval_miles else None
    due_date = add_months(when, interval_months) if interval_months else None
    miles_left = date_left = None
    if due_odometer is not None:
        miles_left = due_odometer - last_odometer  # last known odometer
    if due_date is not None:
        date_left = days_until(due_date, as_of)
    return {
        "due_odometer": due_odometer,
        "due_date": due_date.isoformat() if due_date else None,
        "interval_miles": interval_miles,
        "interval_months": interval_months,
        "days_until_due": date_left,
        "miles_elapsed_since": None,
    }


def insurance_status(expiry_date: str, warn_days: int = 30, today: str | None = None) -> dict[str, Any]:
    when = parse_date(expiry_date, "expiry_date")
    as_of = parse_date(today, "today") if today else date.today()
    left = days_until(when, as_of)
    if left < 0:
        state = "EXPIRED"
    elif left <= warn_days:
        state = "DUE_SOON"
    else:
        state = "OK"
    return {"expires": when.isoformat(), "days_until_expiry": left, "status": state}


# ---------------------------------------------------------------------------
# Sample car (seeded on first run)
# ---------------------------------------------------------------------------

SAMPLE_CAR: dict[str, Any] = {
    "id": "sample-civic",
    "name": "Sample Civic",
    "make": "Honda",
    "model": "Civic LX",
    "year": 2019,
    "created": "2026-10-27",
    "fills": [
        {
            "date": "2026-09-01", "odometer": 60210.0, "gallons": 10.5,
            "price_per_gallon": 3.29, "total_cost": 34.55, "mpg": None,
            "station": "Sample Mart", "notes": "first tracked fill-up",
        },
        {
            "date": "2026-09-20", "odometer": 60580.0, "gallons": 9.8,
            "price_per_gallon": 3.19, "total_cost": 31.26, "mpg": 37.76,
            "station": "Sample Mart", "notes": "mostly highway",
        },
    ],
    "maintenance": [
        {
            "date": "2026-08-15", "type": "oil change", "odometer": 60100.0,
            "interval_miles": 5000, "interval_months": 6,
            "due_odometer": 65100.0, "due_date": "2027-02-15",
            "shop": "Sample Auto", "notes": "full synthetic 0W-20",
        }
    ],
    "tires": [
        {
            "date": "2025-11-08", "season": "winter", "odometer": 57500.0,
            "notes": "swapped to snow tires, stored summer set in garage",
        },
        {
            "date": "2026-04-12", "season": "summer", "odometer": 58800.0,
            "notes": "back to all-seasons, checking tread depth next swap",
        },
    ],
    "insurance": {
        "provider": "Sample Mutual",
        "policy": "SC-000001",
        "expiry_date": "2026-11-15",
        "semiannual_premium": 410.0,
    },
    "shops": [
        {
            "date": "2026-08-15", "shop": "Sample Auto", "work": "oil change + inspection",
            "cost": 89.99, "quote": None, "status": "done",
            "notes": "inspection passed, brake pads ~50%",
        },
        {
            "date": "2026-10-05", "shop": "Tire Pros", "work": "alignment quote",
            "cost": None, "quote": 129.0, "status": "quote",
            "notes": "get it done before winter swap",
        },
    ],
}


def seed_sample_car(data: dict[str, Any]) -> dict[str, Any]:
    """For a brand-new store, add a sample car so the server is usable
    immediately. Returns the (possibly modified) data dict."""
    if not data:
        data = {"cars": {}, "version": 1}
    cars = data.setdefault("cars", {})
    if "sample-civic" not in cars:
        # deepcopy so callers can never mutate the shared SAMPLE_CAR template
        cars["sample-civic"] = copy.deepcopy(SAMPLE_CAR)
    return data


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

NO_DATA = "no data yet for car '{car_id}'"
UNKNOWN_CAR = "unknown car id: '{car_id}'"


class CarLogServer:
    def __init__(self, base_dir: str, data_path: str, seed: bool = True, today: str | None = None):
        self.storage = Storage(base_dir, data_path)
        self.seed = seed
        self.today = today  # injectable "today" for tests; None = real clock
        if self.seed and not self.storage.path.exists():
            # Brand-new store: seed the sample car and write the file so the
            # data directory always contains something real on first run.
            self.storage.save(seed_sample_car(self.storage.load()))

    # -- helpers -----------------------------------------------------------

    def _date(self, value: str | None, field: str = "today") -> date:
        return parse_date(value, field) if value else (parse_date(self.today) if self.today else date.today())

    def _load(self) -> dict[str, Any]:
        data = self.storage.load()
        if self.seed:
            data = seed_sample_car(data)
        return data

    def _car(self, data: dict[str, Any], car_id: str) -> dict[str, Any]:
        cars = data.get("cars", {})
        if car_id not in cars:
            raise LookupError(UNKNOWN_CAR.format(car_id=car_id))
        return cars[car_id]

    # -- tools ---------------------------------------------------------------

    def add_car(self, *, name: str, make: str = "", model: str = "", year: int | None = None,
                car_id: str | None = None) -> dict[str, Any]:
        data = self._load()
        cars = data.setdefault("cars", {})
        if not car_id:
            slug = "-".join(w.lower() for w in (name or make or "car").split() if w.isalnum() or "-" in w)
            car_id = slug or "car"
            base_id, n = car_id, 2
            while car_id in cars:
                car_id = f"{base_id}-{n}"
                n += 1
        if car_id in cars:
            raise ValueError(f"car id already exists: {car_id}")
        cars[car_id] = {
            "id": car_id, "name": name, "make": make, "model": model,
            "year": year, "created": self._date(None).isoformat(),
            "fills": [], "maintenance": [], "tires": [], "insurance": {}, "shops": [],
        }
        self.storage.save(data)
        return {"car_id": car_id, "message": f"added car {car_id!r} ({name})"}

    def list_cars(self) -> dict[str, Any]:
        data = self._load()
        cars = data.get("cars", {})
        return {"count": len(cars), "cars": [
            {"id": c["id"], "name": c.get("name"), "make": c.get("make"),
             "model": c.get("model"), "year": c.get("year"),
             "fills": len(c.get("fills", [])), "maintenance": len(c.get("maintenance", []))}
            for c in cars.values()]}

    def get_car(self, car_id: str) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        return {"car": car}

    def add_fill(self, *, car_id: str, date_: str, odometer: float, gallons: float,
                 price_per_gallon: float | None = None, total_cost: float | None = None,
                 station: str = "", notes: str = "") -> dict[str, Any]:
        data = self._load()
        car = self._car(data, car_id)
        fills = car.setdefault("fills", [])
        fills.sort(key=lambda f: f["odometer"])
        prev = fills[-1]["odometer"] if fills else None
        ppg = price_per_gallon
        if ppg is None and total_cost is not None and gallons > 0:
            ppg = round(total_cost / gallons, 3)
        if total_cost is None and ppg is not None:
            total_cost = round(ppg * gallons, 2)
        mpg = compute_mpg(prev, odometer, gallons) if prev is not None else None
        entry = {
            "date": parse_date(date_, "date").isoformat(), "odometer": odometer,
            "gallons": gallons, "price_per_gallon": ppg, "total_cost": total_cost,
            "mpg": mpg, "station": station, "notes": notes,
        }
        fills.append(entry)
        fills.sort(key=lambda f: f["odometer"])
        self.storage.save(data)
        msg = f"logged fill-up: {gallons} gal at {odometer} mi"
        if mpg is not None:
            msg += f" ({mpg} mpg)"
        else:
            msg += " (mpg needs one prior fill-up to compute)"
        return {"car_id": car_id, "fill": entry, "mpg": mpg, "message": msg}

    def mpg_summary(self, car_id: str) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        fills = [f for f in car.get("fills", []) if f.get("mpg") is not None]
        if not fills:
            return {"car_id": car_id, "fills_with_mpg": 0,
                    "message": "Log at least two fill-ups to see MPG."}
        mpgs = [f["mpg"] for f in fills]
        return {
            "car_id": car_id,
            "fills_with_mpg": len(fills),
            "avg_mpg": round(sum(mpgs) / len(mpgs), 2),
            "best_mpg": max(mpgs),
            "worst_mpg": min(mpgs),
            "latest_mpg": fills[-1]["mpg"],
            "latest_date": fills[-1]["date"],
        }

    def add_maintenance(self, *, car_id: str, date_: str, type_: str, odometer: float,
                        interval_miles: float = 0, interval_months: int = 0,
                        shop: str = "", cost: float | None = None,
                        notes: str = "") -> dict[str, Any]:
        data = self._load()
        car = self._car(data, car_id)
        due = interval_due(date_, odometer, interval_miles, interval_months,
                           today=self.today)
        entry = {
            "date": parse_date(date_, "date").isoformat(), "type": type_,
            "odometer": odometer, "interval_miles": interval_miles or None,
            "interval_months": interval_months or None,
            "due_odometer": due["due_odometer"], "due_date": due["due_date"],
            "shop": shop, "cost": cost, "notes": notes,
        }
        car.setdefault("maintenance", []).append(entry)
        car.setdefault("maintenance", []).sort(key=lambda e: e["date"])
        self.storage.save(data)
        next_bits = []
        if entry["due_odometer"]:
            next_bits.append(f"due at {entry['due_odometer']} mi")
        if entry["due_date"]:
            next_bits.append(f"due {entry['due_date']}")
        return {"car_id": car_id, "event": entry,
                "message": f"logged {type_}" + (f"; next: {', '.join(next_bits)}" if next_bits else "")}

    def due_maintenance(self, car_id: str, today: str | None = None) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        as_of = self._date(today)
        current_odo = self._current_odometer(car)
        rows = []
        for ev in car.get("maintenance", []):
            row = {"date": ev["date"], "type": ev["type"], "shop": ev.get("shop")}
            overdue = []
            if ev.get("due_odometer") and current_odo is not None:
                left_miles = round(ev["due_odometer"] - current_odo, 1)
                row["miles_until_due"] = left_miles
                overdue.append(left_miles <= 0)
            if ev.get("due_date"):
                left_days = days_until(parse_date(ev["due_date"]), as_of)
                row["days_until_due"] = left_days
                overdue.append(left_days <= 0)
            row["status"] = "OVERDUE" if overdue and any(overdue) else "OK"
            rows.append(row)
        rows.sort(key=lambda r: 0 if r["status"] == "OVERDUE" else 1)
        return {"car_id": car_id, "current_odometer": current_odo, "events": rows}

    def _current_odometer(self, car: dict[str, Any]) -> float | None:
        candidates = [f["odometer"] for f in car.get("fills", []) if "odometer" in f]
        candidates += [ev["odometer"] for ev in car.get("maintenance", []) if "odometer" in ev]
        return max(candidates) if candidates else None

    def add_tire_note(self, *, car_id: str, date_: str, season: str, odometer: float,
                      notes: str = "") -> dict[str, Any]:
        data = self._load()
        car = self._car(data, car_id)
        entry = {
            "date": parse_date(date_, "date").isoformat(), "season": season,
            "odometer": odometer, "notes": notes,
        }
        car.setdefault("tires", []).append(entry)
        car["tires"].sort(key=lambda t: t["date"])
        self.storage.save(data)
        return {"car_id": car_id, "tire_note": entry, "message": f"logged tire note ({season})"}

    def list_tire_notes(self, car_id: str) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        tires = car.get("tires", [])
        current = {"current_season": tires[-1]["season"], "since": tires[-1]["date"]} if tires else None
        return {"car_id": car_id, "current": current, "notes": list(reversed(tires))}

    def set_insurance(self, *, car_id: str, provider: str, expiry_date: str,
                      policy: str = "", premium: float | None = None) -> dict[str, Any]:
        data = self._load()
        car = self._car(data, car_id)
        car["insurance"] = {
            "provider": provider,
            "policy": policy,
            "expiry_date": parse_date(expiry_date, "expiry_date").isoformat(),
            "premium": premium,
        }
        self.storage.save(data)
        return {"car_id": car_id, "insurance": car["insurance"], "message": f"insurance saved ({provider})"}

    def check_insurance(self, car_id: str, warn_days: int = 30, today: str | None = None) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        ins = car.get("insurance")
        if not ins or not ins.get("expiry_date"):
            return {"car_id": car_id, "status": "NONE",
                    "message": "No insurance policy on file. Use set_car_insurance."}
        status = insurance_status(ins["expiry_date"], warn_days=warn_days, today=self.today or today)
        return {"car_id": car_id, "provider": ins.get("provider"), **status}

    def add_shop_visit(self, *, car_id: str, date_: str, shop: str, work: str,
                       cost: float | None = None, quote: float | None = None,
                       status: str = "", notes: str = "") -> dict[str, Any]:
        data = self._load()
        car = self._car(data, car_id)
        if not status:
            status = "quote" if quote is not None and cost is None else "done"
        entry = {
            "date": parse_date(date_, "date").isoformat(), "shop": shop, "work": work,
            "cost": cost, "quote": quote, "status": status, "notes": notes,
        }
        car.setdefault("shops", []).append(entry)
        self.storage.save(data)
        return {"car_id": car_id, "visit": entry,
                "message": f"logged shop {status}: {work} at {shop}"}

    def car_summary(self, car_id: str) -> dict[str, Any]:
        car = self._car(self._load(), car_id)
        total_spent = sum(f.get("total_cost") or 0 for f in car.get("fills", []))
        maintenance_spent = sum(ev.get("cost") or 0 for ev in car.get("maintenance", []))
        shop_spent = sum(v.get("cost") or 0 for v in car.get("shops", []) if v.get("cost"))
        open_quotes = [v for v in car.get("shops", []) if v.get("status") == "quote"]
        return {
            "car_id": car_id,
            "name": car.get("name"),
            "current_odometer": self._current_odometer(car),
            "fuel_spent": round(total_spent, 2),
            "maintenance_spent": round(maintenance_spent, 2),
            "shop_visits_spent": round(shop_spent, 2),
            "open_quote_count": len(open_quotes),
            "insurance": self.check_insurance(car_id),
            "due_soon": self.due_maintenance(car_id).get("events", []),
            "tire_season": (car.get("tires") or [{}])[-1].get("season"),
        }


# ---------------------------------------------------------------------------
# MCP protocol plumbing (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO = {"name": "car-log-mcp-server", "version": "1.0.0"}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


TOOLS: list[dict[str, Any]] = [
    _tool("add_car", "Add a car to track.",
          {"name": {"type": "string"}, "make": {"type": "string"},
           "model": {"type": "string"}, "year": {"type": "integer"},
           "car_id": {"type": "string"}}, ["name"]),
    _tool("list_cars", "List every car on file with quick counts.", {}, []),
    _tool("get_car", "Get full details for one car.",
          {"car_id": {"type": "string"}}, ["car_id"]),
    _tool("add_fill", "Log a fuel fill-up; MPG is computed automatically when a prior fill-up exists.",
          {"car_id": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
           "odometer": {"type": "number"}, "gallons": {"type": "number"},
           "price_per_gallon": {"type": "number"}, "total_cost": {"type": "number"},
           "station": {"type": "string"}, "notes": {"type": "string"}},
          ["car_id", "date", "odometer", "gallons"]),
    _tool("mpg_summary", "Average / best / worst MPG across logged fill-ups.",
          {"car_id": {"type": "string"}}, ["car_id"]),
    _tool("add_maintenance", "Log a maintenance event with due-next intervals (miles and/or months).",
          {"car_id": {"type": "string"}, "date": {"type": "string"},
           "type": {"type": "string"}, "odometer": {"type": "number"},
           "interval_miles": {"type": "number"}, "interval_months": {"type": "integer"},
           "shop": {"type": "string"}, "cost": {"type": "number"}, "notes": {"type": "string"}},
          ["car_id", "date", "type", "odometer"]),
    _tool("due_maintenance", "Which maintenance is due / overdue right now.",
          {"car_id": {"type": "string"}}, ["car_id"]),
    _tool("add_tire_note", "Record a tire season swap (winter/summer/all-season) with notes.",
          {"car_id": {"type": "string"}, "date": {"type": "string"}, "season": {"type": "string"},
           "odometer": {"type": "number"}, "notes": {"type": "string"}},
          ["car_id", "date", "season", "odometer"]),
    _tool("list_tire_notes", "List tire season notes and the current season.",
          {"car_id": {"type": "string"}}, ["car_id"]),
    _tool("set_car_insurance", "Save the current insurance policy (provider, expiry, premium).",
          {"car_id": {"type": "string"}, "provider": {"type": "string"},
           "expiry_date": {"type": "string"}, "policy": {"type": "string"},
           "premium": {"type": "number"}},
          ["car_id", "provider", "expiry_date"]),
    _tool("check_insurance", "Is the policy still active / expiring soon / expired?",
          {"car_id": {"type": "string"}, "warn_days": {"type": "integer"}}, ["car_id"]),
    _tool("add_shop_visit", "Log a repair-shop visit, or a quote you're considering (costs and quotes).",
          {"car_id": {"type": "string"}, "date": {"type": "string"}, "shop": {"type": "string"},
           "work": {"type": "string"}, "cost": {"type": "number"}, "quote": {"type": "number"},
           "status": {"type": "string"}, "notes": {"type": "string"}},
          ["car_id", "date", "shop", "work"]),
    _tool("car_summary", "One-car dashboard: spend totals, due items, insurance, tire season.",
          {"car_id": {"type": "string"}}, ["car_id"]),
]

TOOL_ALIASES = {
    "add_fill": "add_fill", "mpg_summary": "mpg_summary",
    "set_car_insurance": "set_insurance", "check_insurance": "check_insurance",
}


def dispatch(server: CarLogServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool by name with dict arguments. Raises LookupError/ValueError on bad input."""
    handler_name = TOOL_ALIASES.get(name, name)
    method = getattr(server, handler_name, None)
    if not callable(method):
        raise LookupError(f"unknown tool: {name}")
    kwargs = dict(args or {})
    kwargs["date_"] = kwargs.pop("date", None)
    # Only keep kwargs the method actually accepts.
    import inspect
    params = inspect.signature(method).parameters
    cleaned = {k: v for k, v in kwargs.items() if k in params}
    return method(**cleaned)


def handle_request(server: CarLogServer, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC request. Returns a response dict, or None for notifications."""
    if not isinstance(request, dict):
        return _error(None, -32600, "request must be an object")
    method = request.get("method")
    req_id = request.get("id")
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": request.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized" or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params", {}) or {}
        tool_name = params.get("name")
        try:
            result = dispatch(server, tool_name, params.get("arguments", {}) or {})
            text_out = json.dumps(result, indent=2, sort_keys=True, default=str)
            return _ok(req_id, {"content": [{"type": "text", "text": text_out}], "isError": False})
        except LookupError as exc:
            return _tool_error(req_id, str(exc))
        except ValueError as exc:
            return _tool_error(req_id, str(exc))
        except (OSError, TypeError) as exc:
            return _tool_error(req_id, f"internal error: {exc}")
    if method is None:
        return None
    return _error(req_id, -32601, f"unknown method: {method}")


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_error(req_id: Any, message: str) -> dict[str, Any]:
    return _ok(req_id, {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True})


def serve(server: CarLogServer, stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    import threading
    lock = threading.Lock()
    w = stdout or sys.stdout
    for line in (stdin or sys.stdin):
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _error(None, -32700, "invalid JSON")
        else:
            response = handle_request(server, request)
        if response is None:
            continue
        with lock:
            w.write(json.dumps(response) + "\n")
            w.flush()
    return 0


def main(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    base_dir = os.environ.get("CAR_LOG_DATA_DIR", os.path.join(os.getcwd(), "data"))
    data_path = argv[1] if len(argv) > 1 else "car-log.json"
    log = CarLogServer(base_dir, data_path)
    return serve(log)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
