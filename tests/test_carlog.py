#!/usr/bin/env python3
"""Offline unit tests for car-log-mcp-server.

Covers MPG math, interval math, insurance reminder math, storage
path-safety (security), auto-backup rotation, and MCP protocol handling.
No network, no external services; uses temp directories only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import server as srv


class MpgMathTests(unittest.TestCase):
    def test_basic_mpg(self):
        # 370 miles on 9.8 gallons -> 37.76 mpg
        self.assertEqual(srv.compute_mpg(60210, 60580, 9.8), 37.76)

    def test_first_fillup_has_no_prior(self):
        self.assertIsNone(srv.compute_mpg(None, 60580, 9.8))

    def test_odometer_rollback_rejected(self):
        self.assertIsNone(srv.compute_mpg(60580, 60210, 9.8))

    def test_same_odometer_rejected(self):
        self.assertIsNone(srv.compute_mpg(60580, 60580, 9.0))

    def test_zero_gallons_rejected(self):
        self.assertIsNone(srv.compute_mpg(60000, 60300, 0))

    def test_negative_gallons_rejected(self):
        self.assertIsNone(srv.compute_mpg(60000, 60300, -5))

    def test_rounding_to_two_places(self):
        # 100 miles / 3 gallons = 33.333...
        self.assertEqual(srv.compute_mpg(1000, 1100, 3), 33.33)


class IntervalMathTests(unittest.TestCase):
    def test_due_odometer_adds_interval(self):
        row = srv.interval_due("2026-08-15", 60100, 5000, 6)
        self.assertEqual(row["due_odometer"], 65100)
        self.assertEqual(row["due_date"], "2027-02-15")

    def test_no_intervals_means_no_due_fields(self):
        row = srv.interval_due("2026-08-15", 60000, 0, 0)
        self.assertIsNone(row["due_odometer"])
        self.assertIsNone(row["due_date"])

    def test_months_only(self):
        row = srv.interval_due("2026-01-31", 100.0, 0, 1, today="2026-02-01")
        self.assertEqual(row["due_date"], "2026-02-28")  # clamped, not skipped

    def testLeapYearClamp(self):
        row = srv.interval_due("2024-01-31", 100.0, 0, 1)
        self.assertEqual(row["due_date"], "2024-02-29")

    def test_days_until_due(self):
        row = srv.interval_due("2026-08-15", 60000, 0, 3, today="2026-10-27")
        self.assertEqual(row["days_until_due"], (date(2026, 11, 15) - date(2026, 10, 27)).days)

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValueError):
            srv.interval_due("2026-13-01", 60000, 5000, 6)

    def test_date_math(self):
        self.assertEqual(srv.days_until(date(2026, 11, 30), date(2026, 10, 27)), 34)
        self.assertEqual(srv.days_until(date(2026, 10, 1), date(2026, 10, 27)), -26)


class InsuranceMathTests(unittest.TestCase):
    def test_ok_state(self):
        st = srv.insurance_status("2026-11-30", warn_days=30, today="2026-10-27")
        self.assertEqual(st["status"], "OK")

    def test_due_soon_state(self):
        st = srv.insurance_status("2026-11-01", warn_days=30, today="2026-10-27")
        self.assertEqual(st["status"], "DUE_SOON")
        self.assertLessEqual(st["days_until_expiry"], 30)

    def test_expired_state(self):
        st = srv.insurance_status("2026-01-01", warn_days=30, today="2026-10-27")
        self.assertEqual(st["status"], "EXPIRED")
        self.assertLess(st["days_until_expiry"], 0)


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_relative_path_ok(self):
        p = srv.resolve_safe_path(str(self.base), "car-log.json")
        self.assertEqual(p.name, "car-log.json")

    def test_nested_path_ok(self):
        p = srv.resolve_safe_path(str(self.base), "cars/main.json")
        self.assertTrue(p.is_relative_to(self.base.resolve()))

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "../evil.json")

    def test_deep_traversal_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "a/b/../../../../evil.json")

    def test_absolute_escapes_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "/etc/passwd.json")

    def test_non_json_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "car-log.txt")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "  ")

    def test_null_byte_rejected(self):
        with self.assertRaises(ValueError):
            srv.resolve_safe_path(str(self.base), "car-log\x00.json")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make_storage(self, name="car-log.json"):
        return srv.Storage(str(self.base), name)

    def test_roundtrip(self):
        st = self.make_storage()
        st.save({"cars": {"x": {"name": "X"}}})
        self.assertEqual(st.load()["cars"]["x"]["name"], "X")

    def test_backup_created_on_overwrite(self):
        st = self.make_storage()
        st.save({"n": 1})
        st.save({"n": 2})
        bak = st.path.with_name("car-log.1.bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text())["n"], 1)
        self.assertEqual(st.load()["n"], 2)

    def test_backup_rotation_caps_keep(self):
        st = self.make_storage()
        for n in range(1, 9):
            st.save({"n": n})
        names = [st.path.with_name(f"car-log.{i}.bak") for i in range(1, 6)]
        self.assertTrue(all(p.exists() for p in names))
        self.assertFalse(st.path.with_name("car-log.6.bak").exists())
        newest = json.loads(names[0].read_text())["n"]
        self.assertEqual(newest, 8 - 1)  # previous value backed up

    def test_corrupt_file_falls_back_to_backup(self):
        st = self.make_storage()
        st.save({"n": 7})
        st.save({"n": 8})  # backup now holds {"n": 7}
        st.path.write_text("{not json")
        self.assertEqual(st.load()["n"], 7)

    def test_corrupt_file_with_no_backup_raises(self):
        st = self.make_storage()
        st.path.write_text("{not json")
        with self.assertRaises(ValueError):
            st.load()

    def test_seed_adds_sample_car(self):
        st = self.make_storage()
        data = st.load()
        seeded = srv.seed_sample_car(data)
        self.assertIn("sample-civic", seeded["cars"])


class ServerLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = srv.CarLogServer(self.tmp.name, "car-log.json",
                                  seed=True, today="2026-10-27")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sample_car_loads(self):
        result = self.s.list_cars()
        self.assertGreaterEqual(result["count"], 1)
        self.assertTrue(any(c["id"] == "sample-civic" for c in result["cars"]))

    def test_add_fill_and_mpg(self):
        self.s.add_car(name="Test Car", car_id="tc")
        r = self.s.add_fill(car_id="tc", date_="2026-10-01", odometer=10000, gallons=10)
        self.assertIsNone(r["mpg"])
        r2 = self.s.add_fill(car_id="tc", date_="2026-10-15", odometer=10380,
                             gallons=10, price_per_gallon=3.5)
        self.assertEqual(r2["mpg"], 38.0)
        self.assertEqual(r2["fill"]["total_cost"], 35.0)

    def test_mpg_rollback_rejected_keeps_data_clean(self):
        self.s.add_car(name="R", car_id="r")
        self.s.add_fill(car_id="r", date_="2026-10-01", odometer=5000, gallons=10)
        car = self.s._car(self.s._load(), "r")
        self.assertEqual(len(car["fills"]), 1)

    def test_maintenance_due_calculation(self):
        self.s.add_car(name="M", car_id="m")
        r = self.s.add_maintenance(car_id="m", date_="2025-10-01", type_="oil change",
                                   odometer=100, interval_miles=5000, interval_months=12)
        self.assertEqual(r["event"]["due_odometer"], 5100)
        self.assertEqual(r["event"]["due_date"], "2026-10-01")
        due = self.s.due_maintenance("m")
        row = [e for e in due["events"] if e["type"] == "oil change"][0]
        self.assertEqual(row["status"], "OVERDUE")

    def test_due_soon_not_overdue(self):
        self.s.add_car(name="S", car_id="s")
        self.s.add_maintenance(car_id="s", date_="2026-10-20", type_="tire rotation",
                               odometer=1000, interval_miles=5000, interval_months=12)
        due = self.s.due_maintenance("s")
        self.assertEqual(due["events"][0]["status"], "OK")

    def test_unknown_car_raises(self):
        with self.assertRaises(LookupError):
            self.s.get_car("does-not-exist")

    def test_duplicate_car_id_rejected(self):
        self.s.add_car(name="D", car_id="dup")
        with self.assertRaises(ValueError):
            self.s.add_car(name="D2", car_id="dup")

    def test_insurance_flow(self):
        self.s.add_car(name="I", car_id="i")
        self.s.set_insurance(car_id="i", provider="Acme", expiry_date="2026-11-30")
        st = self.s.check_insurance("i")
        self.assertEqual(st["status"], "OK")
        self.s.set_insurance(car_id="i", provider="Acme", expiry_date="2026-10-01")
        self.assertEqual(self.s.check_insurance("i")["status"], "EXPIRED")

    def test_tire_and_shop_flow(self):
        self.s.add_car(name="T", car_id="t")
        self.s.add_tire_note(car_id="t", date_="2026-04-01", season="summer", odometer=2000)
        self.s.add_tire_note(car_id="t", date_="2026-11-01", season="winter", odometer=2500,
                             notes="swap day")
        self.assertEqual(self.s.list_tire_notes("t")["current"]["current_season"], "winter")
        self.s.add_shop_visit(car_id="t", date_="2026-10-01", shop="Garage",
                              work="brakes", quote=300.0)
        visits = self.s._car(self.s._load(), "t")["shops"]
        self.assertEqual(visits[0]["status"], "quote")

    def test_summary_numbers(self):
        s = self.s.car_summary("sample-civic")
        self.assertEqual(s["insurance"]["status"], "DUE_SOON")
        self.assertAlmostEqual(s["fuel_spent"], 65.81, places=2)
        self.assertEqual(s["open_quote_count"], 1)
        self.assertEqual(s["tire_season"], "summer")

    def test_summary_insurance_ok_when_far_off(self):
        self.s.set_insurance(car_id="sample-civic", provider="Sample Mutual",
                             expiry_date="2027-03-01")
        # inject today so test stays deterministic even with warn_days
        self.assertEqual(self.s.check_insurance("sample-civic")["status"], "OK")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = srv.CarLogServer(self.tmp.name, "car-log.json", seed=True, today="2026-10-27")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, params=None, req_id=1):
        req = {"jsonrpc": "2.0", "id": req_id, "method": method,
               "params": params or {}}
        return srv.handle_request(self.s, req)

    def test_initialize(self):
        r = self.call("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(r["result"]["serverInfo"]["name"], "car-log-mcp-server")

    def test_tools_list(self):
        r = self.call("tools/list")
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertIn("add_fill", names)
        self.assertIn("check_insurance", names)
        for t in r["result"]["tools"]:
            self.assertIn("inputSchema", t)

    def test_unknown_tool_is_tool_error(self):
        r = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(r["result"]["isError"])

    def test_tools_call_success(self):
        r = self.call("tools/call", {"name": "list_cars", "arguments": {}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("sample-civic", r["result"]["content"][0]["text"])

    def test_bad_arguments_are_tool_errors_not_crashes(self):
        r = self.call("tools/call", {"name": "add_fill",
                                     "arguments": {"car_id": "sample-civic",
                                                   "date": "not-a-date",
                                                   "odometer": 1, "gallons": 1}})
        self.assertTrue(r["result"]["isError"])

    def test_notification_needs_no_response(self):
        r = self.call("notifications/initialized", req_id=None)
        self.assertIsNone(r)

    def test_ping(self):
        r = self.call("ping")
        self.assertEqual(r["result"], {})

    def test_unknown_method(self):
        r = self.call("bogus/method")
        self.assertEqual(r["error"]["code"], -32601)


class StdioSmokeTests(unittest.TestCase):
    """End-to-end: run server.py as a subprocess and speak MCP over stdio."""

    def test_full_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            server_path = str(Path(srv.__file__).resolve())
            requests = "\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "mpg_summary",
                                       "arguments": {"car_id": "sample-civic"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "car_summary",
                                       "arguments": {"car_id": "sample-civic"}}}),
                json.dumps("garbage"),  # invalid JSON must not crash the server
            ]) + "\n"
            proc = subprocess.run(
                [sys.executable, server_path, "car-log.json"],
                input=requests, capture_output=True, text=True,
                cwd=tmp, timeout=30,
            )
            lines = [l for l in proc.stdout.splitlines() if l.strip()]
            responses = [json.loads(l) for l in lines]
            ids = [r.get("id") for r in responses]
            self.assertEqual(ids, [1, 2, 3, 4, None])

            init = responses[0]
            self.assertEqual(init["result"]["serverInfo"]["name"], "car-log-mcp-server")

            tools = responses[1]["result"]["tools"]
            self.assertGreater(len(tools), 5)
            for tool in tools:
                self.assertIn("inputSchema", tool)

            mpg = json.loads(responses[2]["result"]["content"][0]["text"])
            self.assertEqual(mpg["car_id"], "sample-civic")
            self.assertGreater(mpg["fills_with_mpg"], 0)

            summary = json.loads(responses[3]["result"]["content"][0]["text"])
            self.assertEqual(summary["insurance"]["status"], "DUE_SOON")

            # "garbage" is valid JSON but not a request object -> id=None, -32600
            bad = responses[-1]
            self.assertEqual(bad.get("id"), None)
            self.assertEqual(bad["error"]["code"], -32600)

            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, "")
            self.assertTrue((Path(tmp) / "data" / "car-log.json").exists())


if __name__ == "__main__":
    unittest.main()
