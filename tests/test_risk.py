"""Offline tests for the desk's risk rules (tradingagents/dashboard/risk.py and their use in portfolios.py and
checks.py): the 5% position cap on every trade that opens or adds, close-only trades that cannot open a position in
the wrong book, the over-cap flag in the daily check, and the market regime. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_risk.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-risk-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import checks  # noqa: E402
import conviction  # noqa: E402
import portfolios  # noqa: E402
import risk  # noqa: E402
import store  # noqa: E402

FALLING = [100 - i for i in range(30)]   # below its 20-day average and down over five days
RISING = [100 + i for i in range(30)]
FLAT = [100.0] * 30


class BookTest(unittest.TestCase):
    def setUp(self):
        store.DASH.mkdir(parents=True, exist_ok=True)
        portfolios.DIR.mkdir(parents=True, exist_ok=True)
        for f in list(portfolios.DIR.glob("*.json")) + [portfolios.REGISTRY]:
            f.unlink(missing_ok=True)
        store._quotes.clear()
        for target, value in ((store, "levels"), (store, "quotes")):
            patch = mock.patch.object(target, value, **({"return_value": {"atrPct": 2.0}} if value == "levels" else
                                                         {"side_effect": lambda tickers: {t: {"price": None, "previous": None} for t in tickers}}))
            patch.start()
        portfolios._migrate(force_new=True)
        portfolios.set_cash({"cash": 10000})
        self.main = portfolios.active_id()

    def tearDown(self):
        mock.patch.stopall()

    def trade(self, **body):
        return portfolios.record_trade({"portfolio": self.main, **body})


class CapTest(BookTest):
    def test_a_position_over_five_percent_is_refused_and_says_the_most_it_can_be(self):
        with self.assertRaises(ValueError) as err:
            self.trade(ticker="ZEC-USD", side="buy", quantity=2.6881, price=1223.56)  # the trade that made a 31% position
        self.assertIn("32.9% of Main", str(err.exception))
        self.assertIn("at most 0.4086", str(err.exception))
        self.assertEqual(portfolios.book(self.main)["positions"], [])

    def test_five_percent_fits_and_a_price_tick_over_it_is_forgiven(self):
        self.trade(ticker="NVDA", side="buy", quantity=5, price=100)       # exactly 5%
        self.trade(ticker="AMD", side="buy", quantity=5, price=101.5)      # 5.075%: sized at the cap, filled a tick higher
        with self.assertRaises(ValueError):
            self.trade(ticker="TSM", side="buy", quantity=5, price=103)    # 5.15% is over

    def test_the_cap_counts_the_whole_position_when_adding(self):
        self.trade(ticker="NVDA", side="buy", quantity=3, price=100)
        with self.assertRaises(ValueError) as err:
            self.trade(ticker="NVDA", side="buy", quantity=3, price=100)   # 6% in all
        self.assertIn("6.0% of Main", str(err.exception))
        self.trade(ticker="NVDA", side="buy", quantity=2, price=100)       # 5% in all

    def test_shorts_and_flips_are_capped_too(self):
        with self.assertRaises(ValueError):
            self.trade(ticker="LTC-USD", side="sell", quantity=20, price=50)   # a 10% short
        self.trade(ticker="LTC-USD", side="sell", quantity=10, price=50)
        with self.assertRaises(ValueError):
            self.trade(ticker="LTC-USD", side="buy", quantity=30, price=50)    # covers 10 and flips to a 10% long
        self.trade(ticker="LTC-USD", side="buy", quantity=20, price=50)        # covers and flips to a 5% long
        self.assertEqual(portfolios.book(self.main)["positions"][0]["quantity"], 10)

    def test_closing_an_oversized_position_always_works(self):
        book = portfolios.book(self.main)
        book["positions"].append({"ticker": "TRX-USD", "quantity": -10000.0, "average_price": 0.3, "stop": 0.33, "target": 0.27,
                                  "review_by": "2030-01-01"})  # 30% of the book, opened before the cap
        portfolios._save(book)
        self.trade(ticker="TRX-USD", side="buy", quantity=4000, price=0.3)   # a trim
        self.trade(ticker="TRX-USD", side="buy", quantity=6000, price=0.3)   # the rest
        self.assertEqual(portfolios.book(self.main)["positions"], [])

    def test_sizing_helpers(self):
        self.assertEqual(risk.max_quantity(10000, 1223.56, "ZEC-USD"), 0.4086)  # coins to four decimals
        self.assertEqual(risk.max_quantity(10000, 180, "NVDA"), 2.0)            # whole shares
        self.assertIsNone(risk.over_cap(5, 105, 10000))                         # 5.25%: drift, not worth a trim
        self.assertEqual(risk.over_cap(-2.6881, 1150, 9800), 31.5)
        self.assertIsNone(risk.position_pct(1, 100, 0))


class CloseOnlyTest(BookTest):
    def setUp(self):
        super().setUp()
        self.coins = portfolios.create({"name": "Crypto only", "rule": "crypto", "cash": 10000, "activate": True})["active"]
        self.trade(ticker="ZEC-USD", side="sell", quantity=0.4, price=1200)  # the short sits in Main; Crypto only is active

    def test_a_cover_aimed_at_the_wrong_book_is_refused_and_names_the_right_one(self):
        with self.assertRaises(ValueError) as err:
            portfolios.record_trade({"portfolio": self.coins, "ticker": "ZEC-USD", "side": "buy", "quantity": 0.4, "price": 1150, "close": True})
        self.assertIn("Crypto only holds no ZEC-USD short to cover", str(err.exception))
        self.assertIn("That position is in Main", str(err.exception))
        self.assertEqual(portfolios.book(self.coins)["positions"], [])

    def test_a_close_with_no_portfolio_named_does_not_fall_into_the_active_book(self):
        with self.assertRaises(ValueError):
            portfolios.record_trade({"ticker": "ZEC-USD", "side": "buy", "quantity": 0.4, "price": 1150, "close": True})

    def test_a_close_in_the_right_book_closes_and_cannot_be_for_more(self):
        with self.assertRaises(ValueError) as err:
            self.trade(ticker="ZEC-USD", side="buy", quantity=0.5, price=1150, close=True)
        self.assertIn("a close cannot be for more", str(err.exception))
        self.trade(ticker="ZEC-USD", side="buy", quantity=0.1, price=1150, close=True)   # a trim
        self.trade(ticker="ZEC-USD", side="buy", quantity=0.3, price=1150, close=True)
        self.assertEqual(portfolios.book(self.main)["positions"], [])

    def test_a_sell_all_cannot_open_a_short(self):
        with self.assertRaises(ValueError) as err:
            portfolios.record_trade({"portfolio": self.coins, "ticker": "DOGE-USD", "side": "sell", "quantity": 100, "price": 0.07, "close": True})
        self.assertIn("holds no DOGE-USD long to sell", str(err.exception))


class CheckTest(BookTest):
    def test_the_daily_check_flags_a_position_over_the_cap(self):
        book = portfolios.book(self.main)
        book["positions"] = [{"ticker": "ZEC-USD", "quantity": 2.5, "average_price": 1200, "stop": 1100, "target": 1400, "review_by": "2030-01-01"},
                             {"ticker": "NVDA", "quantity": 2, "average_price": 200, "stop": 190, "target": 220, "review_by": "2030-01-01"}]
        book["cash"] = 6600
        portfolios._save(book)
        with mock.patch.object(checks, "_indicators", return_value=None), mock.patch.object(checks, "notify"):
            items = {i["ticker"]: i for i in checks.run_check()["items"]}
        self.assertEqual((items["ZEC-USD"]["level"], items["ZEC-USD"]["bookPct"]), ("act", 30.0))
        self.assertIn("OVER CAP: 30% of the book, the cap is 5%", items["ZEC-USD"]["notes"][0])
        self.assertEqual((items["NVDA"]["level"], items["NVDA"]["bookPct"]), ("ok", None))

    def test_a_job_with_no_kind_does_not_stop_the_automatic_review(self):
        self.trade(ticker="LTC-USD", side="sell", quantity=5, price=50, reviewBy="2020-01-01")
        queued = []
        checks.configure(enqueue=lambda job: queued.append(job) or "j" * 32, jobs={"old": {"id": "old", "status": "done"}})
        with mock.patch.object(store, "settings", return_value={"ready": True}), mock.patch.object(checks, "prefs", return_value={"autoReanalyze": True}):
            out = checks.auto_review()
        self.assertEqual([(o["ticker"], o["action"]) for o in out], [("LTC-USD", "queued")])
        self.assertEqual(queued[0]["ticker"], "LTC-USD")
        checks._desk.clear()


class RegimeTest(unittest.TestCase):
    def test_the_trend_names_the_side_and_breadth_makes_it_strong(self):
        r = risk.regime_from("crypto", FALLING, [-1] * 9 + [1])
        self.assertEqual((r["side"], r["strength"]), ("short", "strong"))
        self.assertIn("strong short regime", r["why"])
        self.assertIn("1 long and 9 short", r["why"])
        r = risk.regime_from("stocks", RISING, [-1] * 9 + [1])   # the agents short a rising market: the trend wins, as a lean
        self.assertEqual((r["side"], r["strength"]), ("long", "lean"))
        self.assertEqual(risk.regime_from("stocks", RISING, [])["strength"], "lean")

    def test_a_flat_trend_takes_a_one_sided_breadth_as_a_lean(self):
        r = risk.regime_from("crypto", FLAT, [-1] * 8)
        self.assertEqual((r["side"], r["strength"]), ("short", "lean"))
        self.assertEqual(risk.regime_from("crypto", FLAT, [-1, -1, 1, 1])["side"], None)   # split calls
        self.assertEqual(risk.regime_from("crypto", FLAT, [-1, -1, -1])["side"], None)     # too few calls
        self.assertEqual(risk.regime_from("crypto", [100.0] * 5, [])["side"], None)        # too little history

    def test_points_and_against(self):
        short = risk.regime_from("crypto", FALLING, [-1] * 9)
        self.assertEqual(risk.regime_points(short, 1)[0], -20)
        self.assertEqual(risk.regime_points(short, -1)[0], 5)
        self.assertEqual((risk.against(short, "long"), risk.against(short, "short")), ("strong", None))
        lean = risk.regime_from("stocks", RISING, [])
        self.assertEqual((risk.regime_points(lean, -1)[0], risk.regime_points(lean, 1)[0]), (-10, 0))
        self.assertIsNone(risk.regime_points(risk.regime_from("crypto", FLAT, []), 1))

    def test_the_score_takes_the_regime_and_says_so(self):
        t = {"ticker": "ZEC-USD", "agents": {"decision": "Overweight"}, "claude": {"verdict": "TAKE", "side": "long"}}
        plain = conviction.score(t)
        ctx = {"regime": risk.regime_from("crypto", FALLING, [-1] * 9)}
        s = conviction.score(t, None, ctx)
        self.assertEqual((plain["score"], plain["against"]), (45, None))
        self.assertEqual((s["score"], s["against"]), (25, "strong"))
        self.assertIn("strong short regime, against it -20", s["why"])
        self.assertEqual(conviction.brief(t, ctx=ctx)["against_regime"], "strong")

    def test_breadth_reads_each_tickers_newest_report(self):
        reports = [{"ticker": "ETH-USD", "date": "2999-01-02", "generated": "b", "rating": "Underweight"},
                   {"ticker": "ETH-USD", "date": "2999-01-01", "generated": "a", "rating": "Overweight"},
                   {"ticker": "XRP-USD", "date": "2999-01-02", "generated": "a", "rating": "Sell"},
                   {"ticker": "NVDA", "date": "2999-01-02", "generated": "a", "rating": "Buy"},
                   {"ticker": "OLD-USD", "date": "2001-01-01", "generated": "a", "rating": "Buy"}]
        with mock.patch.object(store, "list_reports", return_value=reports):
            self.assertEqual(sorted(risk._agent_sides("crypto")), [-1, -1])
            self.assertEqual(risk._agent_sides("stocks"), [1])


if __name__ == "__main__":
    unittest.main()
