"""Offline tests for the recheck list (tradingagents/dashboard/rechecks.py): how long a window-pull WAIT
waits, what an entry looks like, and when a finished run re-arms it. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_rechecks.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-recheck-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import rechecks  # noqa: E402


class WaitDaysTest(unittest.TestCase):
    def test_hourly_stock_window_needs_a_trading_week(self):
        # 32 new hourly candles are about five trading days: seven calendar days with the weekend.
        self.assertEqual(rechecks.wait_days("1h", 128, "stock"), 7)

    def test_coins_trade_round_the_clock(self):
        self.assertEqual(rechecks.wait_days("1h", 128, "crypto"), 2)

    def test_never_less_than_a_day_or_more_than_the_cap(self):
        self.assertEqual(rechecks.wait_days("5m", 32, "crypto"), 1)
        self.assertEqual(rechecks.wait_days("1d", 512, "stock"), rechecks.MAX_DAYS)


def entry(**kw):
    return {"id": "a" * 32, "ticker": "FLY", "asset": "stock", "status": "running", "days": 7, "tries": 0,
            "maxTries": 3, "runs": [], "note": "", **kw}


def run(pull, call="SHORT", verdict="TAKE SMALL", kronos_status="done"):
    return {"started": 1.0, "finished": 2.0, "pull": pull, "kronos": {"status": kronos_status, "error": None},
            "call": {"call": call, "why": []}, "claude": {"status": "done", "verdict": verdict}}


class SettleTest(unittest.TestCase):
    def test_still_pulling_waits_the_same_again(self):
        before = time.time()
        e = rechecks.settle(entry(), run(pull=True))
        self.assertEqual((e["status"], e["tries"], len(e["runs"])), ("waiting", 1, 1))
        self.assertGreaterEqual(e["due"], before + 7 * 86400 - 1)
        self.assertIn("still follows the window pull", e["note"])

    def test_pull_cleared_is_done_with_the_new_call(self):
        e = rechecks.settle(entry(), run(pull=False))
        self.assertEqual(e["status"], "done")
        self.assertIn("Desk call SHORT; Claude: TAKE SMALL", e["note"])

    def test_last_try_stops_even_while_pulling(self):
        e = rechecks.settle(entry(tries=2), run(pull=True))
        self.assertEqual((e["status"], e["tries"]), ("done", 3))
        self.assertIn("still pulling after 3 tries", e["note"])

    def test_kronos_failure_fails_the_entry(self):
        e = rechecks.settle(entry(), run(pull=None, kronos_status="failed"))
        self.assertEqual(e["status"], "failed")


class ListTest(unittest.TestCase):
    def setUp(self):
        rechecks.FILE.parent.mkdir(parents=True, exist_ok=True)
        rechecks._save([])
        patches = [mock.patch.object(rechecks.daily_scan, "race_levels", lambda t: {"stopPct": 2.0, "targetPct": 4.0}),
                   mock.patch.object(rechecks.daily_scan, "_portfolio_for", lambda t: "main"),
                   mock.patch.object(rechecks.portfolios, "book", lambda pid=None: {"id": pid})]
        for p in patches:
            p.start(); self.addCleanup(p.stop)

    def test_add_uses_the_due_rule_and_the_scan_defaults(self):
        e = rechecks.add({"ticker": "fly"})
        self.assertEqual((e["ticker"], e["days"], e["status"], e["kronos"]["timeframe"], e["kronos"]["stopPct"]), ("FLY", 7, "waiting", "1h", 2.0))
        self.assertAlmostEqual(e["due"] - e["created"], 7 * 86400, delta=2)
        self.assertEqual([x["ticker"] for x in rechecks.items()], ["FLY"])

    def test_a_ticker_waits_once(self):
        rechecks.add({"ticker": "FLY"})
        with self.assertRaises(ValueError):
            rechecks.add({"ticker": "FLY"})

    def test_days_can_be_chosen_within_the_cap(self):
        self.assertEqual(rechecks.add({"ticker": "FLY", "days": 3})["days"], 3)
        with self.assertRaises(ValueError):
            rechecks.add({"ticker": "AXON", "days": 99})

    def test_remove_drops_a_waiting_entry(self):
        e = rechecks.add({"ticker": "FLY"})
        self.assertEqual(rechecks.remove(e["id"])["status"], "removed")
        self.assertEqual(rechecks.items(), [])

    def test_restart_puts_a_running_entry_back_to_waiting(self):
        e = rechecks.add({"ticker": "FLY"})
        e["status"] = "running"
        rechecks._update(e)
        rechecks.recover()
        self.assertEqual(rechecks.items()[0]["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
