"""Offline tests for what the desk learns from its closed paper trades (tradingagents/dashboard/lessons.py): reading
round trips back from the trade logs with what the desk knew at the open, and turning the record into adjustments
(a view's weight, a side's starting points, the bar after a losing streak, probes off). No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_lessons.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-lessons-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import conviction  # noqa: E402
import lessons  # noqa: E402
import risk  # noqa: E402

NOTE = ("Workflow Crypto every 4h: probe short at score 37, WORTH ATTENTION, Claude TAKE SMALL, 1.25%, Claude's levels. "
        "short 37: agents short +25 · fan 56% short +2 · race lean -0.33 on the short -15 · band 4")


def t(time, ticker, side, quantity, price, note="", **more):
    return {"time": time, "ticker": ticker, "side": side, "quantity": quantity, "price": price, "note": note, **more}


def trip(pnl, market="crypto", side="short", probe=False, parts=None, with_regime=None, closed=0):
    return {"market": market, "side": side, "pnl": pnl, "pct": pnl / 10, "probe": probe, "parts": parts or {},
            "exitKind": "stop" if pnl < 0 else "target", "withRegime": with_regime, "closed": closed}


class RoundTripTest(unittest.TestCase):
    def test_a_short_closed_at_its_stop_is_a_loss_with_what_the_desk_knew(self):
        trips = lessons.round_trips([t(2, "AMD", "buy", 1, 488.0, "Auto COVER: stop 487.93 reached"),
                                     t(1, "AMD", "sell", 1, 449.0, "Workflow Stock every 10h: opened short at score 58, WORTH ATTENTION")], "Main")
        r = trips[0]
        self.assertEqual((r["ticker"], r["market"], r["side"], r["book"]), ("AMD", "stocks", "short", "Main"))
        self.assertAlmostEqual(r["pnl"], -39.0)
        self.assertAlmostEqual(r["pct"], -8.69, places=2)
        self.assertEqual((r["source"], r["score"], r["probe"], r["exitKind"]), ("workflow", 58, False, "stop"))

    def test_the_tally_in_an_old_note_gives_each_views_points(self):
        r = lessons.round_trips([t(1, "ADA-USD", "sell", 700, 0.18, NOTE), t(2, "ADA-USD", "buy", 700, 0.16, "Auto COVER: target 0.16 reached")])[0]
        self.assertEqual(r["parts"], {"agents": 25.0, "fan": 2.0, "race": -15.0})  # the note was cut before the band's points
        self.assertEqual((r["probe"], r["score"], r["exitKind"], r["market"]), (True, 37, "target", "crypto"))
        self.assertGreater(r["pnl"], 0)

    def test_the_entry_record_wins_over_the_note(self):
        entry = {"source": "workflow", "score": 71, "probe": False, "regime": "short", "parts": {"agents": 25, "review": 20}}
        trips = lessons.round_trips([t(1, "XLM-USD", "buy", 100, 0.15, "anything", entry=entry), t(2, "XLM-USD", "sell", 100, 0.14, "close position")])
        r = trips[0]
        self.assertEqual((r["score"], r["parts"]["review"], r["withRegime"], r["exitKind"]), (71, 20, False, "manual"))  # a long in a short regime

    def test_adds_average_in_partial_closes_and_flips_each_count(self):
        trips = lessons.round_trips([t(1, "NVDA", "buy", 10, 100.0, "From Study (agents Buy)"), t(2, "NVDA", "buy", 10, 110.0),
                                     t(3, "NVDA", "sell", 5, 115.0), t(4, "NVDA", "sell", 20, 95.0), t(5, "NVDA", "buy", 5, 90.0)])
        self.assertEqual([(r["side"], r["quantity"], round(r["pnl"], 2)) for r in trips], [("long", 5, 50.0), ("long", 15, -150.0), ("short", 5, 25.0)])
        self.assertEqual([r["source"] for r in trips], ["study", "study", "manual"])  # the flip opened by hand
        self.assertEqual(lessons._source("short: Underweight from ZEC-USD 2026-11-02 report, stop 1,448.81"), "agents")
        self.assertEqual(lessons._source("Kronos 1h scan 2026-11-05T14:37: 212 target / 88 stop of 300"), "kronos")

    def test_an_open_position_is_not_a_closed_trade(self):
        self.assertEqual(lessons.round_trips([t(1, "ZEC-USD", "buy", 2.6881, 1223.56, "review")]), [])


class LearnTest(unittest.TestCase):
    def test_nothing_changes_under_four_trades(self):
        a = lessons.learn([trip(-10), trip(-10), trip(-10, parts={"agents": 25})], "crypto")
        self.assertEqual((a["handicap"], a["multipliers"]), ({"long": 0, "short": 0}, {}))
        self.assertEqual((a["streak"], a["barBump"]), (3, 10))  # the streak rule needs no minimum

    def test_a_losing_side_starts_with_points_off_and_a_winning_one_with_a_few_on(self):
        stocks = [trip(-10, "stocks", "short") for _ in range(5)] + [trip(10, "stocks", "long") for _ in range(12)]
        a = lessons.learn(stocks, "stocks")
        self.assertEqual(a["handicap"], {"long": 6, "short": -8})   # shorts: 40 * 5/13 * -0.5; longs capped at +6
        self.assertEqual(lessons.learn(stocks, "crypto")["n"], 0)   # the other market learns nothing from it
        self.assertEqual(lessons.handicap([trip(-10)] * 40), -12)   # never more than 12 off

    def test_a_view_that_agreed_with_losers_counts_for_less(self):
        trips = [trip(-10, parts={"agents": 25, "fan": -3}) for _ in range(8)] + [trip(10, parts={"fan": 9}) for _ in range(8)]
        a = lessons.learn(trips, "crypto")
        self.assertEqual(a["multipliers"], {"agents": 0.75, "fan": 1.25})   # 1 -+ 8/16 * 0.5; a view that disagreed is not counted
        s = conviction.score({"ticker": "X-USD", "agents": {"decision": "Underweight"}}, None, {"multipliers": a["multipliers"]})
        self.assertEqual(s["score"], 19)                                    # 25 * 0.75
        self.assertIn("agents short x0.75 +19", s["why"])

    def test_the_streak_ends_at_the_last_win_and_sets_the_bar(self):
        self.assertEqual(lessons.losing_streak([trip(-1), trip(5), trip(-1), trip(0), trip(-1)]), 2)   # a flat trade is skipped
        self.assertEqual([lessons.bar_bump(n) for n in range(6)], [0, 0, 5, 10, 15, 15])
        self.assertEqual(lessons.learn([trip(-1), trip(-1), trip(5)], "crypto")["barBump"], 0)
        old = [trip(-1, closed=1000.0)] * 3   # three losses, long ago: the bar comes back down by itself
        self.assertEqual(lessons.learn(old, "crypto", now=1000.0 + 15 * 86400)["barBump"], 0)
        self.assertEqual(lessons.learn(old, "crypto", now=1000.0 + 13 * 86400)["barBump"], 10)

    def test_probes_stop_after_three_losses_or_a_losing_probe_record(self):
        self.assertIn("3 losses in a row", lessons.learn([trip(-1)] * 3, "crypto")["probesOff"])
        mixed = [trip(-1, probe=True)] * 3 + [trip(5, probe=True), trip(5)]
        self.assertIn("probes won 1 of 4", lessons.learn(mixed, "crypto")["probesOff"])
        self.assertEqual(lessons.learn([trip(5, probe=True)] * 4, "crypto")["probesOff"], "")

    def test_the_lessons_read_as_sentences(self):
        trips = [trip(-10, "stocks", "short", with_regime=False) for _ in range(5)] + [trip(10, "stocks", "long")]
        text = " ".join(lessons.sentences(trips, "stocks"))
        self.assertIn("Closed trades on stocks: 1 of 6 won", text)
        self.assertIn("Shorts on stocks: 0 of 5 won, -1.0% on average; a new short now starts 8 points down.", text)
        self.assertIn("Longs on stocks: 1 of 1 won, +1.0% on average; no adjustment under 4 trades.", text)
        self.assertIn("5 of 6 stopped out.", text)
        self.assertIn("Trades against the market regime: 0 of 5 won", text)
        self.assertEqual(lessons.sentences([], "crypto"), ["No closed trade on coins yet, so nothing is adjusted."])

    def test_context_hands_the_score_the_regime_and_the_record(self):
        trips = [trip(-10, "stocks", "short") for _ in range(5)]
        reg = risk.regime_from("stocks", [100 + i for i in range(30)], [])
        with mock.patch.object(lessons, "journal", return_value=[dict(r, closed=time.time()) for r in trips]), mock.patch.object(risk, "regime", return_value=reg):
            ctx = lessons.context("stocks")
        self.assertEqual((ctx["handicap"]["short"], ctx["barBump"], ctx["regime"]["side"]), (-8, 15, "long"))
        nvda = {"ticker": "NVDA", "agents": {"decision": "Underweight"}, "claude": {"verdict": "TAKE", "side": "short"}}
        s = conviction.score(nvda, None, ctx)
        self.assertEqual((s["side"], s["score"], s["against"]), ("short", 27, "lean"))   # 45 - 10 for the regime - 8 for the record


if __name__ == "__main__":
    unittest.main()
