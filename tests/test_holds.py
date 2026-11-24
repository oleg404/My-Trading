"""Offline tests for the desk's three holds, added after the losses of Nov 23 2026: the market-hours rule
(market_hours.py: stock passes trade only while the market is open), the per-book brake (workflow_rules.brake:
three stop-outs in a day, no new trade in that book), the snapback (risk.snapback: no new trade on the side a
bounce runs against) with its guard (bounce_guard.py: half of each position already on that side closed, once),
and the blank closes the regime used to read. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_holds.py -v
"""

import time
import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

import test_workflows as tw  # noqa: E402  sets the desk's home first; imported whole so its tests do not run twice here
from test_workflows import pick, regime, scan, studied  # noqa: E402

import bounce_guard  # noqa: E402
import market_hours  # noqa: E402
import portfolios  # noqa: E402
import risk  # noqa: E402
import store  # noqa: E402
import workflows  # noqa: E402

REAL_IS_OPEN = market_hours.is_open  # the tests below patch it open; the clock tests need the real one
NY = market_hours.NY


def ny(*args) -> float:
    return datetime(*args, tzinfo=NY).timestamp()


class MarketHoursTest(unittest.TestCase):
    def test_stocks_trade_weekdays_from_half_past_nine_to_four_new_york_time(self):
        self.assertTrue(REAL_IS_OPEN("stocks", ny(2026, 11, 24, 10, 0)))   # a Tuesday morning
        self.assertFalse(REAL_IS_OPEN("stocks", ny(2026, 11, 24, 9, 29)))  # before the open
        self.assertFalse(REAL_IS_OPEN("stocks", ny(2026, 11, 24, 16, 0)))  # at the close
        self.assertFalse(REAL_IS_OPEN("stocks", ny(2026, 11, 21, 12, 0)))  # a Saturday
        self.assertTrue(REAL_IS_OPEN("crypto", ny(2026, 11, 21, 12, 0)))   # coins never close

    def test_holidays_and_half_days(self):
        self.assertFalse(REAL_IS_OPEN("stocks", ny(2026, 11, 26, 11, 0)))  # Thanksgiving
        self.assertTrue(REAL_IS_OPEN("stocks", ny(2026, 11, 27, 12, 59)))  # the day after closes at one
        self.assertFalse(REAL_IS_OPEN("stocks", ny(2026, 11, 27, 13, 0)))

    def test_the_next_open(self):
        with mock.patch.object(market_hours, "is_open", REAL_IS_OPEN):
            self.assertEqual(market_hours.next_open("stocks", ny(2026, 11, 24, 8, 0)), ny(2026, 11, 24, 9, 30))
            self.assertEqual(market_hours.next_open("stocks", ny(2026, 11, 25, 17, 0)), ny(2026, 11, 27, 9, 30))  # over Thanksgiving
            self.assertEqual(market_hours.next_open("stocks", ny(2026, 11, 27, 14, 0)), ny(2026, 11, 30, 9, 30))  # over the weekend
            now = ny(2026, 11, 24, 11, 0)
            self.assertEqual(market_hours.next_open("stocks", now), now)
            self.assertIn("closed until Mon Nov 30 09:30", market_hours.words("stocks", ny(2026, 11, 28, 9, 0)))


class SnapbackTest(unittest.TestCase):
    def test_a_bounce_of_six_percent_off_the_low_blocks_new_shorts(self):
        s = risk.snapback_from("crypto", [100.0, 94.0, 95.0, 99.64])  # 6.0% above the low
        self.assertEqual(s["sides"], ["short"])
        self.assertIn("BTC-USD is 6% above its 72-hour low", s["why"])
        self.assertEqual(risk.snapback_from("crypto", [100.0, 94.0, 99.4])["sides"], [])  # 5.7%: not yet

    def test_a_drop_off_the_high_blocks_new_longs_and_a_swing_both_ways_blocks_both(self):
        self.assertEqual(risk.snapback_from("crypto", [100.0, 106.0, 99.6])["sides"], ["long"])
        self.assertEqual(risk.snapback_from("crypto", [100.0, 90.0, 110.0, 100.0])["sides"], ["short", "long"])

    def test_stocks_and_missing_data_have_no_snapback(self):
        self.assertEqual(risk.snapback_from("stocks", [100.0, 90.0, 99.0])["sides"], [])  # tested on coins only
        self.assertEqual(risk.snapback_from("crypto", [])["sides"], [])


class BlankClosesTest(unittest.TestCase):
    def test_a_blank_close_before_the_open_is_dropped(self):
        frame = pd.DataFrame({"Close": [689.81, float("nan")]}, index=pd.to_datetime(["2026-11-20", "2026-11-23"]))
        self.assertEqual(store.closes_rows(frame), [{"date": "2026-11-20", "close": 689.81}])


class HoldsTest(unittest.TestCase):
    setUp, tearDown = tw.WorkflowTest.setUp, tw.WorkflowTest.tearDown
    run_pass, book, hold, closed = tw.WorkflowTest.run_pass, tw.WorkflowTest.book, tw.WorkflowTest.hold, tw.WorkflowTest.closed

    def three_stop_outs(self):
        self.hold("SOL-USD", "sell", 1.0)
        self.closed("SOL-USD", "buy", 1.0, "Auto COVER: stop 210.00 reached")
        self.hold("ETH-USD", "sell", 0.1)
        self.closed("ETH-USD", "buy", 0.1, "Auto COVER: stop 4,200.00 reached")
        self.hold("BTC-USD", "sell", 0.004)

    # ---- the brake ----

    def test_three_stop_outs_in_a_day_put_the_brake_on_the_book(self):
        self.three_stop_outs()
        self.assertIsNone(workflows.brake(self.book()))  # two so far
        self.closed("BTC-USD", "buy", 0.004, "Auto COVER: stop 102,000.00 reached")
        now = time.time()
        self.assertIn("3 positions in", workflows.brake(self.book(), now))
        self.assertIsNone(workflows.brake(self.book(), now + 25 * 3600))  # a day later it lifts
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))  # the long is not cooled down
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["reason"]), ("skipped", "brake"))
        self.assertEqual(self.book()["positions"], [])

    def test_targets_and_hand_closes_do_not_count_toward_the_brake(self):
        self.hold("SOL-USD", "sell", 1.0)
        self.closed("SOL-USD", "buy", 1.0, "Auto COVER: target 190.00 reached")
        self.hold("ETH-USD", "sell", 0.1)
        self.closed("ETH-USD", "buy", 0.1, "Closed by hand")
        self.hold("BTC-USD", "sell", 0.004)
        self.closed("BTC-USD", "buy", 0.004, "Auto COVER: stop 102,000.00 reached")
        self.assertIsNone(workflows.brake(self.book()))

    # ---- the snapback ----

    def test_no_new_trade_on_the_side_a_snapback_runs_against(self):
        snap = {"sides": ["short"], "up": 7.0, "down": 1.0, "why": "BTC-USD is 7% above its 72-hour low"}
        with mock.patch.object(risk, "snapback", return_value=snap):
            run = self.run_pass(scan([studied("BTC-USD"), studied("SOL-USD", lean="short")],
                                     [pick("BTC-USD", "STRONG BUY", "long"), pick("SOL-USD", "STRONG SELL", "short")]))
        by = {d["ticker"]: d for d in run["decisions"]}
        self.assertEqual((by["SOL-USD"]["action"], by["SOL-USD"]["reason"]), ("skipped", "snapback"))
        self.assertEqual(by["BTC-USD"]["action"], "opened")  # the other side stays open

    def test_the_guard_closes_half_of_each_position_the_snapback_runs_against_once(self):
        self.hold("SOL-USD", "sell", 2.0)
        self.hold("ETH-USD", "buy", 0.1)
        snap = {"sides": ["short"], "up": 7.0, "down": 1.0, "why": "BTC-USD is 7% above its 72-hour low"}
        notify = mock.Mock()
        with mock.patch.object(risk, "snapback", return_value=snap):
            done = bounce_guard.protect(notify, today="2026-11-23")
            self.assertEqual(bounce_guard.protect(notify, today="2026-11-23"), [])  # once per position
        self.assertEqual([(d["ticker"], d["closed"]) for d in done if d["portfolio"] == self.book()["name"]], [("SOL-USD", 1.0)])
        pos = {p["ticker"]: p for p in self.book()["positions"]}
        self.assertEqual((pos["SOL-USD"]["quantity"], pos["SOL-USD"]["snapback_half"], pos["SOL-USD"]["trimmed"]), (-1.0, "2026-11-23", "2026-11-23"))
        self.assertEqual(pos["ETH-USD"]["quantity"], 0.1)  # a long is on the snapback's side: left alone
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Snapback guard: closed half."))

    def test_without_a_snapback_the_guard_does_nothing(self):
        self.hold("SOL-USD", "sell", 2.0)
        self.assertEqual(bounce_guard.protect(mock.Mock()), [])  # setUp's snapback is quiet
        self.assertEqual(self.book()["positions"][0]["quantity"], -2.0)

    # ---- a pass that could open nothing skips its scan ----

    def test_held_back_names_why_a_pass_would_open_nothing(self):
        self.assertIsNone(workflows.held_back(self.w, self.book()))
        with mock.patch.object(market_hours, "is_open", return_value=False):
            self.assertIn("closed until", workflows.held_back(self.w, self.book()))
            run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
            self.assertEqual((run["decisions"][0]["action"], run["decisions"][0]["reason"]), ("skipped", "closed"))
        snap = {"sides": ["short"], "up": 7.0, "down": 1.0, "why": "BTC-USD is 7% above its 72-hour low"}
        with mock.patch.object(risk, "snapback", return_value=snap):
            self.assertIsNone(workflows.held_back(self.w, self.book()))  # no regime: longs may still open
            with mock.patch.object(risk, "regime", return_value=regime("short", "strong")):
                self.assertIn("the other side goes against the strong regime", workflows.held_back(self.w, self.book()))
        self.three_stop_outs()
        self.closed("BTC-USD", "buy", 0.004, "Auto COVER: stop 102,000.00 reached")
        self.assertIn("hit their stops", workflows.held_back(self.w, self.book()))

    def test_a_due_stock_pass_waits_for_the_open(self):
        pid = portfolios.create({"name": f"Any {self._testMethodName}"[:40], "rule": "any", "cash": 10000, "activate": True})["active"]
        w = workflows.create({"name": "Stocks", "portfolio": pid, "market": "stocks"})["workflows"][-1]
        with mock.patch.object(market_hours, "is_open", REAL_IS_OPEN):
            workflows.defer(w["id"], ny(2026, 11, 28, 9, 0))  # a Saturday
        self.assertEqual(workflows._load(w["id"])["nextRun"], ny(2026, 11, 30, 9, 30) + workflows.DEFER_AFTER_OPEN)
        workflows.delete(w["id"])


if __name__ == "__main__":
    unittest.main()
