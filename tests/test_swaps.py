"""Offline tests for the desk's swaps (tradingagents/dashboard/swaps.py): which held winner may give its room to a
stronger pick, how the picks and the positions are paired, and what a pass does with a full book. No model, no
network: the quotes and the daily bars are made up.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_swaps.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-swaps-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402
import lessons  # noqa: E402
import portfolios  # noqa: E402
import risk  # noqa: E402
import store  # noqa: E402
import swaps  # noqa: E402
import workflows  # noqa: E402
from test_workflows import PRICES, pick, regime, scan, studied  # noqa: E402

NOW = time.time()
DAY = 86400


def bars(lows=(196.0, 194.0, 198.0), highs=None):
    """One daily bar per value, the last one today (UTC); a short's best price is the lowest low."""
    today = datetime.now(timezone.utc).date()
    highs = highs or [x + 8 for x in lows]
    return [{"date": (today - timedelta(days=len(lows) - 1 - i)).isoformat(), "high": h, "low": lo, "close": (h + lo) / 2}
            for i, (lo, h) in enumerate(zip(lows, highs))]


def short(ticker="SOL-USD", avg=220.0, stop=230.0, target=190.0, days=3.0, **more):
    """A short a workflow opened `days` ago at `avg`; SOL trades at 200 now: up 9.1%, 5% left, 15% back."""
    return {"ticker": ticker, "quantity": -2.5, "average_price": avg, "opened": NOW - days * DAY, "stop": stop, "target": target,
            "workflow": "a" * 32, **more}


def hold(ticker, pct, ratio):
    return {"ticker": ticker, "pct": pct, "ratio": ratio}


def want(ticker, score, full=5.0):
    return {"ticker": ticker, "score": score, "full": full}


class StandingTest(unittest.TestCase):
    def stand(self, p, price=200.0, b=None):
        return swaps.standing(p, price, NOW, lambda t, d: bars() if b is None else b)

    def test_a_stalled_winner_with_little_left_may_go(self):
        s = self.stand(short())
        self.assertEqual((s["side"], s["pnlPct"], s["left"], s["back"], s["ratio"]), ("short", -(200 - 220) / 220 * 100 // 0.01 / 100, 5.0, 15.0, 0.333))

    def test_a_loser_is_left_to_its_stop(self):
        self.assertIsNone(self.stand(short(avg=190.0)))

    def test_a_winner_with_plenty_left_stays(self):
        self.assertIsNone(self.stand(short(target=150.0)))            # 25% left against 15% back
        self.assertIsNotNone(self.stand(short(target=177.5)))         # 11.25% left: exactly three quarters of 15%

    def test_a_position_of_the_last_day_or_under_review_or_not_a_workflows_stays(self):
        self.assertIsNone(self.stand(short(days=0.9)))
        self.assertIsNone(self.stand(short(review_run={"job": "j", "date": "2026-11-16"})))
        self.assertIsNone(self.stand({k: v for k, v in short().items() if k != "workflow"}))
        self.assertIsNone(self.stand(short(), price=None))

    def test_a_winner_still_running_stays(self):
        self.assertIsNone(self.stand(short(), b=bars((196.0, 194.0, 193.0))))        # today's low is the best so far
        self.assertIsNone(self.stand(short(), price=193.5, b=bars((196.0, 194.0, 198.0))))  # the live price is
        self.assertIsNone(self.stand(short(days=1.2), b=bars((196.0, 194.0, 198.0))))  # only today's bar after the entry day
        self.assertIsNone(self.stand(short(), b=[]))                                  # no bars: no swap on missing data

    def test_a_long_reads_the_highs(self):
        long = short(avg=180.0, stop=170.0, target=210.0) | {"quantity": 2.5}
        self.assertIsNotNone(self.stand(long, b=bars((180.0, 185.0, 188.0), highs=(190.0, 206.0, 201.0))))
        self.assertIsNone(self.stand(long, b=bars((180.0, 185.0, 188.0), highs=(190.0, 201.0, 206.0))))

    def test_candidates_come_weakest_first_with_their_size(self):
        book = {"positions": [short(), short("ETH-USD", avg=4400.0, stop=4600.0, target=3900.0), short("BTC-USD", avg=95000.0)]}
        with mock.patch.object(store, "daily_bars", side_effect=lambda t, d: bars() if t == "SOL-USD" else bars((3990.0, 3950.0, 3995.0))):
            got = swaps.candidates(book, PRICES, 10000.0, now=NOW)
            self.assertEqual([(s["ticker"], s["pct"]) for s in got], [("ETH-USD", 100.0), ("SOL-USD", 5.0)])  # 2.5 ETH at 4000
            self.assertEqual([s["ticker"] for s in swaps.candidates(book, PRICES, 10000.0, own=["SOL-USD"], now=NOW)], ["SOL-USD"])


class PlanTest(unittest.TestCase):
    def test_the_best_pick_takes_the_weakest_position(self):
        steps = swaps.plan([want("B", 70), want("A", 99)], [hold("X", 5.0, 0.3), hold("Y", 5.0, 0.5)], 0.2)
        self.assertEqual([(s["pick"], [h["ticker"] for h in s["out"]], s["pct"]) for s in steps], [("A", ["X"], 5.0), ("B", ["Y"], 5.0)])

    def test_a_pick_takes_two_small_positions_for_its_full_size(self):
        steps = swaps.plan([want("A", 99)], [hold("X", 2.1, 0.3), hold("Y", 2.5, 0.4), hold("Z", 5.0, 0.5)], 0.6)
        self.assertEqual([(s["pick"], [h["ticker"] for h in s["out"]], s["pct"]) for s in steps], [("A", ["X", "Y"], 5.0)])

    def test_nothing_is_closed_for_a_pick_that_would_get_under_half_its_size(self):
        self.assertEqual(swaps.plan([want("A", 99)], [hold("X", 2.0, 0.3)], -1.0), [])       # a book over its limit: 1% is no seat
        steps = swaps.plan([want("A", 99)], [hold("X", 4.4, 0.3)], -1.5)
        self.assertEqual([(s["pick"], s["pct"]) for s in steps], [("A", 2.9)])

    def test_a_pass_seats_two_picks_and_closes_three_positions_at_most(self):
        holds = [hold(t, 5.0, 0.3) for t in "WXYZ"]
        self.assertEqual([s["pick"] for s in swaps.plan([want("A", 99), want("B", 90), want("C", 80)], holds, 0.0)], ["A", "B"])
        self.assertEqual([s["pick"] for s in swaps.plan([want("A", 99), want("B", 90)], holds, 0.0, seats=1)], ["A"])
        small = [hold(t, 2.6, 0.3) for t in "WXYZ"]
        steps = swaps.plan([want("A", 99), want("B", 90)], small, 0.0)
        self.assertEqual([(s["pick"], len(s["out"]), s["pct"]) for s in steps], [("A", 2, 5.0), ("B", 1, 2.8)])

    def test_a_workflow_at_its_count_frees_one_of_its_own_even_with_room(self):
        steps = swaps.plan([want("A", 99)], [hold("X", 5.0, 0.3)], 40.0, need_out=True)
        self.assertEqual([(s["pick"], [h["ticker"] for h in s["out"]]) for s in steps], [("A", ["X"])])
        self.assertEqual(swaps.plan([want("A", 99)], [], 40.0, need_out=True), [])


class PassTest(unittest.TestCase):
    """A pass on a full book: SOL short from 220, now 200, stalled; BTC is the new pick, every view long (score 90)."""

    def setUp(self):
        self.quotes = mock.patch.object(store, "quotes", side_effect=lambda t: {x: PRICES.get(x, {"price": None}) for x in t})
        self.quotes.start()
        store._quotes.clear()
        self.pid = portfolios.create({"name": f"Swaps {self._testMethodName}"[:40], "rule": "crypto", "cash": 10000, "activate": True})["active"]
        self.w = workflows.clean({"name": "Crypto every 4h", "portfolio": self.pid})
        self.w.update(id="a" * 32, runs=[], lastRun=None)
        for target, name, value in ((workflows.checks, "notify", None), (claude_review, "get", {"review": {"stop": 97000.0, "target": 106000.0}}),
                                    (risk, "regime", regime()), (lessons, "journal", []), (store, "daily_bars", bars()),
                                    (risk, "snapback", {"sides": [], "up": 1.0, "down": 1.0, "why": ""}),
                                    (workflows.market_hours, "is_open", True)):
            mock.patch.object(target, name, return_value=value).start()
        mock.patch.object(risk, "MAX_INVESTED_PCT", 5.2).start()
        self.doc = scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")])

    def tearDown(self):
        mock.patch.stopall()

    def hold(self, avg=220.0, days=3.0):
        portfolios.record_trade({"portfolio": self.pid, "ticker": "SOL-USD", "side": "sell", "quantity": 2.5, "price": 200.0,
                                 "stop": 230.0, "target": 190.0, "workflow": self.w["id"], "reviewBy": "2030-01-01"})
        book = portfolios.book(self.pid)                # 5% of the book at today's 200; the entry is moved back in time and price
        book["positions"][0].update(opened=time.time() - days * DAY, average_price=avg)
        portfolios._save(book)

    def run_pass(self):
        run = {"id": "r" * 32, "decisions": [], "stage": ""}
        workflows._decide(self.w, run, self.doc)
        return run

    def held(self):
        return [p["ticker"] for p in portfolios.book(self.pid)["positions"]]

    def test_a_strong_pick_takes_the_room_of_a_stalled_winner(self):
        self.hold()
        run = self.run_pass()
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["pct"], run["opened"], run["closed"]), ("opened", 5.0, 1, 1))
        self.assertIn("took the room of SOL-USD", d["note"])
        self.assertEqual(self.held(), ["BTC-USD"])
        self.assertEqual([(s["out"], s["for"], s["score"], s["left"], s["back"]) for s in run["swaps"]], [("SOL-USD", "BTC-USD", 90, 5.0, 15.0)])
        self.assertIn("Swapped out SOL-USD (short, up 9.09%, 5% left to the target, 15% back to the stop) for BTC-USD at score 90.", run["notes"])
        book = portfolios.book(self.pid)
        closing = next(t for t in book["trades"] if t["ticker"] == "SOL-USD")
        self.assertEqual((closing["side"], closing["quantity"]), ("buy", 2.5))
        self.assertIn("Workflow Crypto every 4h: swapped out the short for BTC-USD (score 90)", closing["note"])
        self.assertEqual(lessons.round_trips(book["trades"])[0]["exitKind"], "swap")
        self.assertIn("swapped out at", workflows.scan_skips(self.w, book)["SOL-USD"])   # it rests a day, in every workflow on the book
        self.assertIsNone(workflows.cooldown(book, "SOL-USD", "short"))                  # a swap is not a stop-out

    def test_a_pick_just_over_the_bar_does_not_cost_a_winner(self):
        self.hold()
        self.w["minScore"] = 80                                        # 90 clears the bar, not the bar plus 15
        run = self.run_pass()
        self.assertEqual((run["decisions"][0]["action"], run["decisions"][0]["reason"]), ("skipped", "invested"))
        self.assertEqual((self.held(), run.get("swaps")), (["SOL-USD"], None))

    def kept(self, avg=220.0, days=3.0, swap=True):
        self.hold(avg, days)
        self.w["swap"] = swap
        run = self.run_pass()
        self.assertEqual((run["decisions"][0]["reason"], self.held(), run["closed"]), ("invested", ["SOL-USD"], 0))

    def test_a_loser_keeps_its_room(self):
        self.kept(avg=190.0)

    def test_a_position_of_the_last_day_keeps_its_room(self):
        self.kept(days=0.5)

    def test_the_switch_turned_off_keeps_the_book_as_it_is(self):
        self.kept(swap=False)

    def test_a_pick_against_the_regime_does_not_cost_a_winner(self):
        self.hold()
        with mock.patch.object(risk, "regime", return_value=regime("short", "lean")):
            run = self.run_pass()
        self.assertEqual((run["decisions"][0]["action"], self.held()), ("skipped", ["SOL-USD"]))

    def test_a_workflow_at_its_count_swaps_one_of_its_own(self):
        self.hold()
        self.w["maxPositions"] = 1
        with mock.patch.object(risk, "MAX_INVESTED_PCT", 95.0):
            run = self.run_pass()
        self.assertEqual((run["decisions"][0]["action"], self.held(), run["closed"]), ("opened", ["BTC-USD"], 1))

    def test_a_full_book_gets_its_pass_only_while_a_winner_is_ready(self):
        self.hold()
        self.assertIsNone(workflows.no_room(self.w, portfolios.book(self.pid)))
        self.w["swap"] = False
        self.assertIn("5% invested, the desk's limit", workflows.no_room(self.w, portfolios.book(self.pid)))
        self.w["swap"] = True
        with mock.patch.object(store, "daily_bars", return_value=bars((196.0, 194.0, 193.0))):   # still running
            self.assertIn("nothing to study until a position closes", workflows.no_room(self.w, portfolios.book(self.pid)))

    def test_the_switch_is_on_by_default_and_saved(self):
        self.assertTrue(workflows.DEFAULTS["swap"] and self.w["swap"])
        self.assertFalse(workflows.clean({"name": "x", "portfolio": self.pid, "swap": False})["swap"])


if __name__ == "__main__":
    unittest.main()
