"""Offline tests for the desk connector's rules (src/desk_mcp/logic.py).

Run: python3 -m unittest discover -s tests -p 'test_desk_mcp*.py' -v
"""

import sys
import time
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "desk_mcp"))

import logic  # noqa: E402

TODAY = date(2026, 11, 4)


def report(rating, day="2026-11-02"):
    return {"ticker": "NVDA", "rating": rating, "date": day}


def scan(down, up, side, lean, calibration=0.8, hours_ago=2, stop_first=10, target_first=40):
    return {"created": time.time() - hours_ago * 3600, "params": {"ticker": "NVDA"}, "status": "done",
            "result": {"down": down, "up": up, "n": down + up, "lean": lean, "calibration": calibration,
                       "timeframe": "1h", "horizon": 48, "stop_first": stop_first, "target_first": target_first,
                       "plan": {"side": side, "stop_pct": 1.0, "target_pct": 2.0}}}


def call(rating, k_scan, **kw):
    a = logic.agent_view(report(rating, **kw), today=TODAY) if rating else None
    k = logic.kronos_view(k_scan) if k_scan else None
    return logic.combine(a, k)["call"]


class CombineTest(unittest.TestCase):
    def test_agree_and_clean_is_a_full_call(self):
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.2)), "LONG")
        self.assertEqual(call("Underweight", scan(40, 10, "short", 0.2)), "SHORT")

    def test_disagreement_is_no_trade(self):
        self.assertEqual(call("Overweight", scan(40, 10, "short", 0.2)), "NO TRADE")

    def test_right_way_wrong_levels_is_wait(self):
        # The NVDA case on the desk: fan up, race favours the stop.
        self.assertEqual(call("Overweight", scan(10, 40, "long", -0.0933, calibration=None, stop_first=38, target_first=12)), "WAIT")

    def test_caveats_make_a_lean(self):
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.2, calibration=0.49)), "LEAN LONG")
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.2, calibration=None)), "LEAN LONG")
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.01)), "LEAN LONG")
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.2), day="2026-10-20"), "LEAN LONG")
        self.assertEqual(call("Overweight", scan(10, 40, "long", 0.2, hours_ago=30)), "LEAN LONG")

    def test_hold_or_even_fan_is_wait(self):
        self.assertEqual(call("Hold", scan(10, 40, "long", 0.2)), "WAIT")
        self.assertEqual(call("Overweight", scan(25, 25, "long", 0.2)), "WAIT")

    def test_one_side_missing(self):
        self.assertEqual(call("Overweight", None), "HALF A CALL")
        self.assertEqual(call(None, scan(10, 40, "long", 0.2)), "HALF A CALL")
        self.assertEqual(call(None, None), "NO CALL YET")


class PortfolioTest(unittest.TestCase):
    pfs = [{"id": "main", "name": "Main", "rule": "any", "held": {}},
           {"id": "c1", "name": "Crypto only", "rule": "crypto", "held": {}},
           {"id": "s1", "name": "Stocks only", "rule": "stocks", "held": {}}]

    def test_active_unless_its_rule_refuses(self):
        self.assertEqual(logic.pick_portfolio(self.pfs, "c1", "BTC-USD")["id"], "c1")
        self.assertEqual(logic.pick_portfolio(self.pfs, "c1", "NVDA")["id"], "main")
        self.assertEqual(logic.pick_portfolio(self.pfs, "s1", "ETH-USD")["id"], "main")

    def test_by_name_any_case(self):
        self.assertEqual(logic.pick_portfolio(self.pfs, "main", "BTC-USD", "crypto ONLY")["id"], "c1")
        with self.assertRaises(ValueError):
            logic.pick_portfolio(self.pfs, "main", "BTC-USD", "Nope")

    def test_rule_allows_closing_what_is_held(self):
        held = {"id": "c1", "name": "Crypto only", "rule": "crypto", "held": {"NVDA": 3}}
        self.assertTrue(logic.takes(held, "NVDA"))
        self.assertFalse(logic.takes(self.pfs[1], "NVDA"))

    def test_sizing_is_ten_percent_of_equity(self):
        self.assertEqual(logic.size_quantity(25000, 204.64, 10, "NVDA"), 12.0)
        self.assertEqual(logic.size_quantity(10000, 2300, 10, "ETH-USD"), 0.4347)
        self.assertEqual(logic.size_quantity(0, 100, 10, "NVDA"), 0.0)

    def test_window_text(self):
        self.assertEqual(logic.window_text("1h", 48), "2 days")
        self.assertEqual(logic.window_text("5m", 48), "4 hours")


if __name__ == "__main__":
    unittest.main()


class ReversionTest(unittest.TestCase):
    def test_pulled_kronos_fan_is_a_wait_whatever_the_agents_say(self):
        k = scan(0, 300, "long", 0.4)
        k["result"]["reversion"] = {"flagged": True, "z": -3.0, "label": "The fan follows the window, not the market."}
        view = logic.kronos_view(k)
        self.assertTrue(view["reversion"])
        for a in (None, logic.agent_view(report("Overweight"), TODAY), logic.agent_view(report("Underweight"), TODAY)):
            c = logic.combine(a, view)
            self.assertEqual(c["call"], "WAIT")
            self.assertIn("follows the window", c["why"][0])

    def test_unpulled_fan_keeps_the_old_call(self):
        view = logic.kronos_view(scan(0, 300, "long", 0.4))
        self.assertFalse(view["reversion"])
        self.assertEqual(logic.combine(logic.agent_view(report("Overweight"), TODAY), view)["call"], "LONG")
