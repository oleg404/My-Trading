"""Offline tests for the profit guard (tradingagents/dashboard/profit_guard.py): a winning position whose stop-out
would cost more than 1% of the book has its stop moved to the entry and its review brought forward to today, so
the agents re-run and Claude holds it or closes half. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_profit_guard.py -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-profit-guard-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import lessons  # noqa: E402
import portfolios  # noqa: E402
import position_review  # noqa: E402
import profit_guard  # noqa: E402
import store  # noqa: E402

TODAY = date.today().isoformat()
IN_THREE = (date.today() + timedelta(days=portfolios.REVIEW_DAYS)).isoformat()
SHORT = {"ticker": "AKE-USD", "quantity": -80000, "average_price": 0.006, "stop": 0.0065, "target": 0.0045}
LONG = {"ticker": "NVDA", "quantity": 5, "average_price": 100.0, "stop": 92.0, "target": 130.0}


def quotes(**prices):
    return mock.patch.object(store, "quotes", lambda tickers: {t: {"price": prices.get(t), "previous": None} for t in tickers})


class DueTest(unittest.TestCase):
    def test_a_winning_short_whose_stop_out_costs_over_one_percent_is_due(self):
        # 0.006 to 0.005 earned $80; back up to the stop at 0.0065 loses $120 from here, 1.2% of a $10,000 book
        self.assertEqual(profit_guard.due(SHORT, 0.005, 10000), {"risk_pct": 1.2, "move_stop": True})

    def test_a_winning_long_is_judged_the_same_way(self):
        self.assertEqual(profit_guard.due(LONG, 115.0, 10000), {"risk_pct": 1.15, "move_stop": True})

    def test_a_losing_position_is_left_to_its_stop(self):
        self.assertIsNone(profit_guard.due(SHORT, 0.0062, 10000))
        self.assertIsNone(profit_guard.due({**SHORT, "stop": 0.009}, 0.0062, 10000))  # heavy, but not a winner

    def test_a_stop_out_under_one_percent_of_the_book_is_left_alone(self):
        self.assertIsNone(profit_guard.due(SHORT, 0.0056, 10000))  # $72 at risk
        self.assertIsNone(profit_guard.due(SHORT, 0.005, 12000))   # $120 is exactly 1% here; the rule needs more

    def test_a_winner_that_has_not_yet_earned_what_it_risked_keeps_its_stop(self):
        # up $16 with the stop still $200 past the entry: a stop at the entry would be hit by noise
        self.assertIsNone(profit_guard.due({**SHORT, "stop": 0.0085}, 0.0058, 10000))

    def test_a_stop_already_at_the_entry_or_better_is_not_moved_but_the_review_still_comes(self):
        self.assertEqual(profit_guard.due({**LONG, "stop": 100.0}, 125.0, 10000), {"risk_pct": 1.25, "move_stop": False})
        self.assertEqual(profit_guard.due({**LONG, "stop": 105.0}, 130.0, 10000), {"risk_pct": 1.25, "move_stop": False})

    def test_nothing_is_due_without_a_stop_a_price_or_the_books_worth(self):
        self.assertIsNone(profit_guard.due({**SHORT, "stop": None}, 0.005, 10000))
        self.assertIsNone(profit_guard.due(SHORT, None, 10000))
        self.assertIsNone(profit_guard.due(SHORT, 0.005, 0))


class RunTest(unittest.TestCase):
    def setUp(self):
        store.DASH.mkdir(parents=True, exist_ok=True)
        portfolios.DIR.mkdir(parents=True, exist_ok=True)
        for f in list(portfolios.DIR.glob("*.json")) + [portfolios.REGISTRY]:
            f.unlink(missing_ok=True)
        portfolios._migrate(force_new=True)
        portfolios.set_cash({"cash": 10000})
        store._quotes.clear()
        p = mock.patch.object(store, "levels", return_value={"atrPct": 2.0})
        p.start(); self.addCleanup(p.stop)
        with quotes():
            portfolios.record_trade({"ticker": "AKE-USD", "side": "sell", "quantity": 80000, "price": 0.006, "stop": 0.0065, "target": 0.0045})
            portfolios.record_trade({"ticker": "LINK-USD", "side": "sell", "quantity": 40, "price": 10.0, "stop": 10.5, "target": 9.0})

    def position(self, ticker="AKE-USD"):
        return next(p for p in portfolios.book()["positions"] if p["ticker"] == ticker)

    def edit(self, **fields):
        data = portfolios.book()
        next(p for p in data["positions"] if p["ticker"] == "AKE-USD").update(fields)
        portfolios._save(data)

    def test_the_stop_moves_to_the_entry_and_the_review_comes_today(self):
        with quotes(**{"AKE-USD": 0.005, "LINK-USD": 9.9}):
            out = profit_guard.protect(notify := mock.Mock())
        p = self.position()
        self.assertEqual((p["stop"], p["target"], p["review_by"]), (0.006, 0.0045, TODAY))
        self.assertEqual(p["guard"], {"date": TODAY, "risk_pct": 1.19, "stop_from": 0.0065})
        self.assertIn("stop moved to the entry", p["levels"])
        self.assertEqual(out, [{"portfolio": portfolios.book()["name"], "ticker": "AKE-USD", "risk_pct": 1.19, "stop": 0.006, "stop_from": 0.0065, "review": True}])
        notify.assert_called_once()
        self.assertIn("AKE-USD", notify.call_args.args[0])
        link = self.position("LINK-USD")  # up a little, light risk: untouched
        self.assertEqual((link["stop"], link["review_by"]), (10.5, IN_THREE))
        self.assertNotIn("guard", link)

    def test_a_second_look_does_nothing_once_the_stop_is_at_the_entry(self):
        with quotes(**{"AKE-USD": 0.005, "LINK-USD": 9.9}):
            profit_guard.protect(mock.Mock())
            self.assertEqual(profit_guard.protect(notify := mock.Mock()), [])  # $80 of profit at risk is 0.8% of the book
        notify.assert_not_called()

    def test_a_position_reviewed_today_or_under_review_keeps_its_review_date_but_the_stop_still_moves(self):
        for state in ({"review_date": TODAY}, {"review_run": {"job": "j1", "date": TODAY}}):
            self.edit(**{"stop": 0.0065, "review_by": IN_THREE, "review_date": None, "review_run": None, **state})
            with quotes(**{"AKE-USD": 0.005}):
                out = profit_guard.protect(mock.Mock())
            p = self.position()
            self.assertEqual((p["stop"], p["review_by"], out[0]["review"]), (0.006, IN_THREE, False), state)

    def test_a_review_already_due_is_not_counted_as_brought_forward(self):
        self.edit(review_by=TODAY)
        with quotes(**{"AKE-USD": 0.005}):
            out = profit_guard.protect(mock.Mock())
        self.assertEqual((self.position()["stop"], out[0]["review"]), (0.006, False))

    def test_a_big_winner_with_its_stop_at_the_entry_gets_one_early_review_a_day(self):
        self.edit(stop=0.006)
        with quotes(**{"AKE-USD": 0.0046}):  # $112 of open profit, 1.11% of the book, all of it still at risk
            out = profit_guard.protect(notify := mock.Mock())
            self.assertEqual((out[0]["review"], out[0]["stop_from"], self.position()["review_by"]), (True, None, TODAY))
            self.assertEqual(profit_guard.protect(mock.Mock()), [])  # due today now, so not pulled again
        notify.assert_called_once()

    def test_a_position_without_a_quote_is_skipped(self):
        with quotes():
            self.assertEqual(profit_guard.protect(mock.Mock()), [])
        self.assertEqual(self.position()["stop"], 0.0065)

    def test_claude_is_told_the_review_came_early_and_what_a_stop_out_costs(self):
        with quotes(**{"AKE-USD": 0.005}):
            profit_guard.protect(mock.Mock())
        with mock.patch.object(store, "read_report", return_value={}), mock.patch.object(lessons, "brief", return_value={}):
            f = position_review.facts(portfolios.book(), self.position(), {"decision": "SELL"}, 0.005, 10080.0)
            self.assertEqual(f["position"]["stop_out_costs_pct_of_book"], 0.79)  # the stop is at the entry now
            self.assertIn("1.19% of the book", f["reviewed_early"])
            self.assertIn("moved its stop to the entry", f["reviewed_early"])
            self.edit(guard={"date": "2026-01-01", "risk_pct": 1.19})
            self.assertNotIn("reviewed_early", position_review.facts(portfolios.book(), self.position(), {"decision": "SELL"}, 0.005, 10080.0))


if __name__ == "__main__":
    unittest.main()
