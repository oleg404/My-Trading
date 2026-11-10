"""Offline tests for Claude's call after a position's review (tradingagents/dashboard/position_review.py): hold the
position or close half of it, and what the positions table then needs (when a position was opened). Claude is
mocked. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_position_review.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-position-review-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402
import lessons  # noqa: E402
import portfolios  # noqa: E402
import position_review  # noqa: E402
import store  # noqa: E402

TODAY = date.today().isoformat()
JOB = {"report": "LINK-USD_20261110_120000", "decision": "HOLD"}


def quotes(**prices):
    return mock.patch.object(store, "quotes", lambda tickers: {t: {"price": prices.get(t), "previous": None} for t in tickers})


class PositionReviewTest(unittest.TestCase):
    def setUp(self):
        store.DASH.mkdir(parents=True, exist_ok=True)
        portfolios.DIR.mkdir(parents=True, exist_ok=True)
        for f in list(portfolios.DIR.glob("*.json")) + [portfolios.REGISTRY]:
            f.unlink(missing_ok=True)
        portfolios._migrate(force_new=True)
        portfolios.set_cash({"cash": 10000})
        store._quotes.clear()
        for p in (mock.patch.object(store, "levels", return_value={"atrPct": 2.0}), mock.patch.object(store, "read_report", return_value={"rating": "Hold", "summary": "range-bound"}),
                  mock.patch.object(lessons, "brief", return_value={"market_regime": "no clear regime", "desk_record": []}),
                  mock.patch.object(claude_review, "ready", return_value={"ready": True, "reason": ""})):
            p.start(); self.addCleanup(p.stop)
        with quotes():
            portfolios.record_trade({"ticker": "LINK-USD", "side": "sell", "quantity": 27.6568, "price": 10.84, "stop": 11.4, "target": 9.56})
            portfolios.record_trade({"ticker": "NVDA", "side": "buy", "quantity": 1, "price": 180.0, "stop": 170.0, "target": 200.0})

    def position(self, ticker="LINK-USD"):
        return next((p for p in portfolios.book()["positions"] if p["ticker"] == ticker), None)

    def test_half_of_a_position_is_four_decimals_of_a_coin_and_whole_shares(self):
        self.assertEqual(position_review.half_quantity("LINK-USD", -27.6568), 13.8284)
        self.assertEqual(position_review.half_quantity("NVDA", 11), 5.0)
        self.assertEqual(position_review.half_quantity("NVDA", 1), 0.0)  # one share has no half

    def test_the_facts_measure_the_position_against_its_plan(self):
        f = position_review.facts(portfolios.book(), self.position(), JOB, 10.44, 10000.0)
        pos = f["position"]
        self.assertEqual((pos["side"], pos["pnl_pct"], pos["pct_to_stop"], pos["pct_to_target"]), ("short", 3.69, 9.2, 8.43))
        self.assertEqual((pos["pct_of_book"], pos["already_halved_on"]), (2.89, None))
        self.assertLess(pos["days_held"], 0.1)
        self.assertEqual((f["agents_review"]["call"], f["agents_review"]["rating"], f["market_regime"]), ("HOLD", "Hold", "no clear regime"))

    def test_close_half_covers_half_and_marks_the_position_trimmed(self):
        answer = {"action": "CLOSE HALF", "confidence": "medium", "reason": "The agents went to Hold with 8% left to the target."}
        with quotes(**{"LINK-USD": 10.44}), mock.patch.object(claude_review, "ask", return_value=(answer, ["sonnet"])) as ask:
            done = position_review.judge("main", "LINK-USD", JOB, notify := mock.Mock())
        self.assertEqual((done["action"], done["closed"]), ("CLOSE HALF", 13.8284))
        self.assertIn('"pct_to_target": 8.43', ask.call_args.args[0])
        p = self.position()
        self.assertAlmostEqual(p["quantity"], -13.8284, places=4)
        self.assertEqual((p["stop"], p["target"], p["trimmed"]), (11.4, 9.56, TODAY))  # the rest rides on the same plan
        self.assertEqual((p["review_claude"]["action"], p["review_claude"]["closed"], p["review_claude"]["date"]), ("CLOSE HALF", 13.8284, TODAY))
        trade = portfolios.book()["trades"][0]
        self.assertEqual((trade["side"], trade["quantity"], trade["price"]), ("buy", 13.8284, 10.44))
        self.assertTrue(trade["note"].startswith("Review: Claude closed half. The agents went to Hold"))
        notify.assert_called_once()

    def test_hold_changes_nothing_but_keeps_the_answer(self):
        answer = {"action": "HOLD", "confidence": "high", "reason": "The agents still say Sell."}
        with quotes(**{"LINK-USD": 10.44}), mock.patch.object(claude_review, "ask", return_value=(answer, [])):
            done = position_review.judge("main", "LINK-USD", JOB, notify := mock.Mock())
        p = self.position()
        self.assertEqual((done["closed"], p["quantity"], p["review_claude"]["action"]), (0.0, -27.6568, "HOLD"))
        self.assertNotIn("trimmed", p)
        self.assertEqual(len(portfolios.book()["trades"]), 2)
        notify.assert_not_called()

    def test_one_share_cannot_be_halved_and_a_failed_call_leaves_the_position(self):
        done = position_review.apply("main", "NVDA", {"action": "CLOSE HALF", "confidence": "low", "reason": "weak"}, 181.0)
        self.assertEqual((done["closed"], self.position("NVDA")["quantity"]), (0.0, 1))
        self.assertNotIn("trimmed", self.position("NVDA"))
        with quotes(**{"LINK-USD": 10.44}), mock.patch.object(claude_review, "ask", side_effect=RuntimeError("no answer")), mock.patch("builtins.print"):
            self.assertIsNone(position_review.judge("main", "LINK-USD", JOB))
        self.assertNotIn("review_claude", self.position())
        with mock.patch.object(claude_review, "ready", return_value={"ready": False, "reason": "logged out"}), mock.patch.object(claude_review, "ask") as ask:
            self.assertIsNone(position_review.judge("main", "LINK-USD", JOB))
        ask.assert_not_called()

    def test_a_position_knows_when_it_was_opened(self):
        self.assertAlmostEqual(self.position()["opened"], time.time(), delta=5)
        log = [{"time": 100, "ticker": "X", "side": "buy", "quantity": 2, "price": 1}, {"time": 200, "ticker": "X", "side": "buy", "quantity": 1, "price": 1},
               {"time": 300, "ticker": "X", "side": "sell", "quantity": 1, "price": 1}, {"time": 150, "ticker": "Y", "side": "sell", "quantity": 1, "price": 1}]
        self.assertEqual(portfolios.opened_at(log, "X"), 100)  # adding and reducing do not restart the clock
        self.assertEqual(portfolios.opened_at(log + [{"time": 400, "ticker": "X", "side": "sell", "quantity": 5, "price": 1}], "X"), 400)  # a flip does
        self.assertEqual(portfolios.opened_at(log + [{"time": 400, "ticker": "X", "side": "sell", "quantity": 2, "price": 1},
                                                    {"time": 500, "ticker": "X", "side": "buy", "quantity": 1, "price": 1}], "X"), 500)  # closed, then opened again
        self.assertIsNone(portfolios.opened_at(log, "Z"))
        book = portfolios.book()
        for p in book["positions"]:
            p.pop("opened")
        portfolios._save(book)
        with quotes():
            portfolios.ensure_plans()  # positions from before the field get it from the trade log
        self.assertAlmostEqual(self.position()["opened"], time.time(), delta=5)


if __name__ == "__main__":
    unittest.main()
