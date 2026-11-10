"""Offline tests for the desk's workflow rules (tradingagents/dashboard/workflows.py) and the
conviction score they trade on (conviction.py): validation, the score, and how one pass turns
a scan into paper trades, including the position cap, the market regime and the lessons from
closed trades. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_workflows.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-workflows-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402
import conviction  # noqa: E402
import daily_scan  # noqa: E402
import lessons  # noqa: E402
import portfolios  # noqa: E402
import risk  # noqa: E402
import store  # noqa: E402
import workflows  # noqa: E402

PRICES = {"BTC-USD": {"price": 100000.0, "previous": 99000.0}, "SOL-USD": {"price": 200.0, "previous": 210.0},
          "ETH-USD": {"price": 4000.0, "previous": 4100.0}, "NVDA": {"price": 180.0, "previous": 179.0}}


def pick(ticker, label, side, size=8.0, why="lines up"):
    return {"ticker": ticker, "label": label, "side": side, "size_pct": size, "confidence": "high", "why": why}


def studied(ticker, verdict="TAKE", pull=False, plan=None, review_id=None, lean="long", agents="auto", confidence="high",
            fan=(240, 60), race=0.27, cal=0.6, screen="auto"):
    """A scanned ticker whose views all point `lean`: agents, Kronos's fan and race, Claude's review, the screen."""
    flip = lean == "short"
    up, down = (fan[1], fan[0]) if flip else fan
    if agents == "auto":
        agents = "Underweight" if flip else "Overweight"
    return {"ticker": ticker, "price": PRICES[ticker]["price"], "source": "coin market", "side": lean if screen == "auto" else screen, "why": [],
            "agents": {"status": "done", "decision": agents} if agents else None,
            "kronos": {"id": "k" * 32, "status": "done", "result": {"reversion": {"flagged": pull}, "plan": plan, "side": lean,
                                                                    "up": up, "down": down, "n": up + down, "lean": race, "calibration": cal}},
            "claude": {"id": review_id, "status": "done", "verdict": verdict, "side": lean if verdict != "PASS" else "none",
                       "size_pct": 8 if verdict != "PASS" else 0, "confidence": confidence}}


def regime(side=None, strength=None, market="crypto"):
    return {"market": market, "side": side, "strength": strength, "why": f"{strength or 'no'} {side or 'clear'} regime"}


def lost(n, market="crypto", side="long", probe=False):
    """n closed losing trades, as lessons.journal() returns them."""
    return [{"market": market, "side": side, "pnl": -10.0, "pct": -2.0, "probe": probe, "parts": {}, "exitKind": "stop",
             "withRegime": None, "closed": time.time() - 3600 + i} for i in range(n)]


def scan(tickers, picks):
    return {"id": "s" * 32, "status": "done", "market": "crypto", "tickers": tickers, "picks": {"summary": "", "picks": picks}}


class ConvictionTest(unittest.TestCase):
    def test_every_view_agreeing_scores_near_the_top(self):
        s = conviction.score(studied("BTC-USD"), pick("BTC-USD", "STRONG BUY", "long"))
        self.assertEqual((s["side"], s["score"]), ("long", 90))  # 25 + 9 + 15 + 6 + 20 + 10 + 5
        self.assertIn("agents long +25", s["why"])
        s = conviction.score(studied("BTC-USD", lean="short"), pick("BTC-USD", "STRONG SELL", "short"))
        self.assertEqual((s["side"], s["score"]), ("short", 90))

    def test_claudes_skip_no_longer_hides_a_trade(self):
        s = conviction.score(studied("BTC-USD"), pick("BTC-USD", "SKIP", "none"))
        self.assertEqual((s["side"], s["score"]), ("long", 75))  # the rank costs 5 and gives no side; the rest agree
        s = conviction.score(studied("BTC-USD", verdict="PASS", confidence="high"), pick("BTC-USD", "SKIP", "none"))
        self.assertEqual(s["score"], 43)  # PASS at high confidence costs 12, the SKIP label 5

    def test_the_pick_side_wins_and_disagreement_subtracts(self):
        s = conviction.score(studied("BTC-USD"), pick("BTC-USD", "WORTH ATTENTION", "short"))
        self.assertEqual(s["side"], "short")
        self.assertEqual(s["score"], 0)  # every other view is long: the short cannot score

    def test_window_pull_is_a_discount_not_a_veto(self):
        s = conviction.score(studied("BTC-USD", pull=True), None)
        self.assertEqual((s["side"], s["score"], s["kronos_alone"]), ("long", 62, False))  # fan and race keep a quarter
        alone = studied("BTC-USD", pull=True, verdict="PASS", agents=None, screen=None)
        s = conviction.score(alone, None)
        self.assertTrue(s["kronos_alone"] and s["pulled"])

    def test_band_grades_against_the_desks_own_record(self):
        self.assertEqual([conviction.band_grade(x)[0] for x in (None, 0.35, 0.45, 0.6, 0.79)], ["unchecked", "weak", "ordinary", "good", "strong"])

    def test_no_view_means_no_side(self):
        t = {"ticker": "ETH-USD", "price": 4000.0, "side": None, "why": [], "agents": None, "kronos": {}, "claude": {}}
        s = conviction.score(t, pick("ETH-USD", "SKIP", "none"))
        self.assertEqual((s["side"], s["score"]), (None, 0))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        # Patched before the book is created: creating one values every book, which would otherwise fetch live quotes.
        self.quotes = mock.patch.object(store, "quotes", side_effect=lambda t: {x: PRICES.get(x, {"price": None}) for x in t})
        self.quotes.start()
        store._quotes.clear()
        self.pid = portfolios.create({"name": f"Coins {self._testMethodName}"[:40], "rule": "crypto", "cash": 10000, "activate": True})["active"]
        self.w = workflows.clean({"name": "Crypto every 4h", "portfolio": self.pid})
        self.w.update(id="a" * 32, runs=[], lastRun=None)
        self.notify = mock.patch.object(workflows.checks, "notify")
        self.notify.start()
        self.review = mock.patch.object(claude_review, "get", return_value={"review": {"stop": 97000.0, "target": 106000.0}})
        self.review.start()
        self.regime = mock.patch.object(risk, "regime", return_value=regime())  # no clear regime unless a test sets one
        self.regime.start()
        self.journal = mock.patch.object(lessons, "journal", return_value=[])   # no closed trades unless a test gives some
        self.journal.start()

    def tearDown(self):
        mock.patch.stopall()

    def run_pass(self, doc):
        run = {"id": "r" * 32, "decisions": [], "stage": ""}
        workflows._decide(self.w, run, doc)
        return run

    def book(self):
        return portfolios.book(self.pid)

    def test_defaults_and_validation(self):
        self.assertEqual((self.w["market"], self.w["limit"], self.w["everyHours"], self.w["sizePct"]), ("crypto", 10, 4, 5.0))
        self.assertEqual((self.w["minLabel"], self.w["minScore"], self.w["probe"], self.w["requireClaude"]), ("score", 50, True, False))
        with self.assertRaises(ValueError):
            workflows.clean({"name": "", "portfolio": self.pid})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": "nope"})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "market": "stocks"})  # a crypto-only book
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "stopPct": 5, "targetPct": 4})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "sizePct": 6})  # over the desk's 5% position cap
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "minScore": 101})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "minLabel": "never"})

    def test_old_workflows_are_upgraded_to_the_score(self):
        old = {"id": "o" * 32, "name": "Crypto", "minLabel": "strong", "requireClaude": True, "runs": []}
        w = workflows._upgrade(dict(old))
        self.assertEqual((w["minLabel"], w["minScore"], w["probe"], w["requireClaude"]), ("score", 50, True, False))
        self.assertIn("upgraded", w)
        self.assertNotIn("upgraded", workflows._upgrade({"id": "n" * 32, "minScore": 60}))
        self.assertEqual(workflows._upgrade({"id": "n" * 32, "minScore": 60, "sizePct": 10.0})["sizePct"], 5.0)  # saved before the cap
        # Saved before the desk filled its books: room for 18 positions at the cap, once; a later edit to fewer stays.
        self.assertEqual(workflows._upgrade({"id": "n" * 32, "minScore": 60, "maxPositions": 5})["maxPositions"], 18)
        self.assertEqual(workflows._upgrade({"id": "n" * 32, "minScore": 60, "maxPositions": 5, "deploy": True})["maxPositions"], 5)
        self.assertEqual((self.w["maxPositions"], self.w["deploy"]), (18, True))

    def test_strong_buy_opens_a_long_with_claudes_levels(self):
        run = self.run_pass(scan([studied("BTC-USD", review_id="c" * 32)], [pick("BTC-USD", "STRONG BUY", "long")]))
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["score"], d["probe"]), ("opened", 90, False))
        self.assertEqual((d["stop"], d["target"], d["levels"]), (97000.0, 106000.0, "Claude's levels"))
        self.assertAlmostEqual(d["quantity"], 0.005, places=4)  # Claude asked for 8%; the cap is 5% of 10,000 at 100,000
        entry = self.book()["trades"][0]["entry"]  # what the desk knew, kept for the lessons
        self.assertEqual((entry["source"], entry["score"], entry["probe"], entry["regime"], entry["parts"]["agents"]), ("workflow", 90, False, "none", 25))
        pos = self.book()["positions"][0]
        self.assertEqual((pos["ticker"], pos["stop"], pos["target"]), ("BTC-USD", 97000.0, 106000.0))
        self.assertIn("review_by", pos)
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Workflow Crypto every 4h: opened long at score 90"))

    def test_size_is_the_full_size_once_the_bar_is_cleared(self):
        self.w["sizePct"] = 4.0
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long", size=20)]))
        self.assertEqual(run["decisions"][0]["pct"], 4.0)
        self.assertAlmostEqual(run["decisions"][0]["quantity"], 2.0, places=4)
        self.w["sizePct"] = 10.0  # a workflow saved before the cap: the cap still wins
        run = self.run_pass(scan([studied("ETH-USD", verdict="PASS", confidence="low")], [pick("ETH-USD", "WORTH ATTENTION", "long")]))
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["score"], d["pct"]), ("opened", 62, 5.0))  # the 5% cap in full: the score no longer shrinks it

    def test_a_held_pick_is_topped_up_to_the_full_size(self):
        portfolios.record_trade({"portfolio": self.pid, "ticker": "SOL-USD", "side": "buy", "quantity": 0.5, "price": 200.0,
                                 "stop": 190.0, "target": 230.0, "note": "Workflow first: probe long at score 45"})  # 1% of the book
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long")]))
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["pct"], run["added"], run["opened"]), ("added", 4.0, 1, 0))
        pos = self.book()["positions"][0]
        self.assertAlmostEqual(pos["quantity"], 2.5, places=4)  # 5% of 10,000 at 200
        self.assertEqual((pos["stop"], pos["target"]), (190.0, 230.0))  # the plan it had stays
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Workflow Crypto every 4h: added to the long at score 90"))
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long")]))
        self.assertEqual((run["decisions"][0]["reason"], run["added"]), ("held", 0))
        self.assertIn("its full size", run["decisions"][0]["note"])

    def test_no_top_up_under_the_bar_or_after_a_review_halved_it(self):
        portfolios.record_trade({"portfolio": self.pid, "ticker": "SOL-USD", "side": "buy", "quantity": 0.5, "price": 200.0, "note": "Study tab"})
        weak = studied("SOL-USD", verdict="TAKE SMALL", agents=None, fan=(180, 120), race=0.08, cal=0.5)
        run = self.run_pass(scan([weak], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual((run["decisions"][0]["reason"], run["opened"], run["added"]), ("held", 0, 0))  # and it is not probed either
        book = self.book()
        book["positions"][0]["trimmed"] = "2026-11-10"
        portfolios._save(book)
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long")]))
        self.assertEqual(run["decisions"][0]["reason"], "held")
        self.assertIn("closed half", run["decisions"][0]["note"])
        self.assertAlmostEqual(self.book()["positions"][0]["quantity"], 0.5, places=4)

    def test_the_book_is_filled_to_the_invested_limit_and_no_further(self):
        with mock.patch.object(risk, "MAX_INVESTED_PCT", 8.0):
            doc = scan([studied("BTC-USD"), studied("SOL-USD"), studied("ETH-USD")],
                       [pick("BTC-USD", "STRONG BUY", "long"), pick("SOL-USD", "STRONG BUY", "long"), pick("ETH-USD", "STRONG BUY", "long")])
            run = self.run_pass(doc)
        self.assertEqual([(d["action"], d.get("pct"), d.get("reason")) for d in run["decisions"]],
                         [("opened", 5.0, None), ("opened", 3.0, None), ("skipped", None, "invested")])
        self.assertIn("8% of its equity", run["decisions"][2]["note"])

    def test_a_skip_label_still_trades_when_the_desk_agrees(self):
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "SKIP", "none", size=0, why="Claude did not rank it.")]))
        d = run["decisions"][0]
        self.assertEqual((d["action"], d["deskSide"], d["score"]), ("opened", "long", 75))

    def test_label_floors_still_gate_when_asked(self):
        self.w["minLabel"] = "strong"
        run = self.run_pass(scan([studied("ETH-USD")], [pick("ETH-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual((run["decisions"][0]["action"], run["decisions"][0]["reason"]), ("skipped", "label"))
        self.w["minLabel"] = "attention"
        run = self.run_pass(scan([studied("ETH-USD")], [pick("ETH-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual(run["decisions"][0]["action"], "opened")

    def test_claudes_pass_gates_only_when_required(self):
        self.w["requireClaude"] = True
        run = self.run_pass(scan([studied("SOL-USD", verdict="PASS")], [pick("SOL-USD", "STRONG BUY", "long")]))
        self.assertEqual(run["decisions"][0]["reason"], "review")
        self.w["requireClaude"] = False
        run = self.run_pass(scan([studied("SOL-USD", verdict="PASS")], [pick("SOL-USD", "STRONG BUY", "long")]))
        self.assertEqual((run["decisions"][0]["action"], run["decisions"][0]["score"]), ("opened", 58))

    def test_window_pull_vetoes_only_a_kronos_only_side(self):
        alone = studied("BTC-USD", pull=True, verdict="PASS", agents=None, screen=None)
        run = self.run_pass(scan([alone, studied("SOL-USD", pull=True)], [pick("BTC-USD", "SKIP", "none"), pick("SOL-USD", "SKIP", "none")]))
        notes = {d["ticker"]: (d["action"], d.get("reason")) for d in run["decisions"]}
        self.assertEqual(notes["BTC-USD"], ("skipped", "pull"))
        self.assertEqual(notes["SOL-USD"], ("opened", None))  # the agents and Claude give the side; the pulled fan is discounted

    def test_under_the_bar_is_skipped_and_the_best_is_probed_at_half_size(self):
        weak = studied("SOL-USD", verdict="TAKE SMALL", agents=None, fan=(180, 120), race=0.08, cal=0.5)
        weaker = studied("ETH-USD", verdict="PASS", agents=None, fan=(180, 120), race=0.08, cal=0.5)
        run = self.run_pass(scan([weak, weaker], [pick("SOL-USD", "WORTH ATTENTION", "long"), pick("ETH-USD", "WORTH ATTENTION", "long")]))
        by = {d["ticker"]: d for d in run["decisions"]}
        self.assertEqual((by["ETH-USD"]["action"], by["ETH-USD"]["reason"]), ("skipped", "score"))
        self.assertEqual((by["SOL-USD"]["action"], by["SOL-USD"]["probe"], by["SOL-USD"]["score"]), ("opened", True, 36))
        self.assertEqual(by["SOL-USD"]["pct"], 2.5)  # the 5% cap, half for a probe
        self.assertEqual(run["opened"], 1)
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Workflow Crypto every 4h: probe long at score 36"))

    def test_no_probe_without_the_option_or_under_the_floor(self):
        self.w["probe"] = False
        weak = studied("SOL-USD", verdict="TAKE SMALL", agents=None, fan=(180, 120), race=0.08, cal=0.5)
        run = self.run_pass(scan([weak], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual(run["opened"], 0)
        self.w["probe"], self.w["minScore"] = True, 80
        run = self.run_pass(scan([weak], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual(run["opened"], 0)  # the floor is 65; a 36 is not probed
        self.assertEqual(workflows.probe_floor({"minScore": 40}), 30)

    def test_fallback_levels_when_claude_gave_none(self):
        self.review.stop()
        with mock.patch.object(claude_review, "get", return_value={"review": {"stop": None, "target": None}}):
            run = self.run_pass(scan([studied("SOL-USD", plan={"side": "long", "stop": 196.0, "target": 208.0})], [pick("SOL-USD", "STRONG BUY", "long")]))
            self.assertEqual(run["decisions"][0]["levels"], "Kronos's plan")
            run = self.run_pass(scan([studied("ETH-USD")], [pick("ETH-USD", "STRONG BUY", "long")]))
            d = run["decisions"][0]
            self.assertEqual((d["stop"], d["target"]), (3880.0, 4240.0))  # 3% / 6% of 4,000
        self.review.start()

    def test_caps_per_pass_and_per_portfolio(self):
        self.w["maxNewPerRun"] = 1
        doc = scan([studied("BTC-USD"), studied("SOL-USD")], [pick("BTC-USD", "STRONG BUY", "long"), pick("SOL-USD", "STRONG BUY", "long")])
        run = self.run_pass(doc)
        self.assertEqual([d["action"] for d in run["decisions"]], ["opened", "skipped"])
        self.assertIn("max 1", run["decisions"][1]["note"])
        self.w["maxNewPerRun"], self.w["maxPositions"] = 5, 1
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long")]))
        self.assertIn("already holds 1", run["decisions"][0]["note"])
        self.assertEqual(self.book()["positions"][0]["workflow"], self.w["id"])

    def test_position_cap_counts_only_what_this_workflow_opened(self):
        # The Study tab and another workflow fill the same book; neither counts toward this workflow's cap.
        portfolios.record_trade({"portfolio": self.pid, "ticker": "ETH-USD", "side": "sell", "quantity": 0.1, "price": 4000.0,
                                 "note": "Study tab: paper short"})
        portfolios.record_trade({"portfolio": self.pid, "ticker": "SOL-USD", "side": "sell", "quantity": 1.0, "price": 200.0,
                                 "note": "Workflow first: probe short at score 45"})
        self.w["maxPositions"] = 1
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        self.assertEqual(run["decisions"][0]["action"], "opened")
        self.assertEqual(len(self.book()["positions"]), 3)
        # A position this workflow opened before positions carried the tag still counts, by its trade note.
        book = portfolios.book(self.pid)
        for h in book["positions"]:
            h.pop("workflow", None)
        book["trades"][0]["note"] = f"Workflow {self.w['name']}: opened long at score 80"
        portfolios._save(book)
        run = self.run_pass(scan([studied("NVDA")], [pick("NVDA", "STRONG BUY", "long")]))
        self.assertEqual(run["decisions"][0]["reason"], "full")
        self.assertIn("already holds 1 of", run["decisions"][0]["note"])

    def test_strong_sell_opens_a_short_unless_shorts_are_off(self):
        self.w["allowShort"] = False
        run = self.run_pass(scan([studied("SOL-USD", lean="short")], [pick("SOL-USD", "STRONG SELL", "short")]))
        self.assertIn("shorts are off", run["decisions"][0]["note"])
        self.w["allowShort"] = True
        run = self.run_pass(scan([studied("SOL-USD", lean="short")], [pick("SOL-USD", "STRONG SELL", "short")]))
        self.assertEqual(run["decisions"][0]["action"], "opened")
        pos = self.book()["positions"][0]
        self.assertLess(pos["quantity"], 0)
        self.assertEqual((pos["stop"], pos["target"]), (206.0, 188.0))  # Claude's long levels don't fit a short: 3% / 6% fallback

    def test_reverse_pick_closes_the_held_position(self):
        self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        self.assertEqual(len(self.book()["positions"]), 1)
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        self.assertIn("already long", run["decisions"][0]["note"])  # at its full size: nothing to add
        run = self.run_pass(scan([studied("BTC-USD", verdict="PASS")], [pick("BTC-USD", "WORTH ATTENTION", "short")]))
        self.assertEqual(run["decisions"][0]["reason"], "score")  # a weak reverse (every view still long) does not flip the book
        run = self.run_pass(scan([studied("BTC-USD", lean="short")], [pick("BTC-USD", "STRONG SELL", "short")]))
        self.assertEqual((run["decisions"][0]["action"], run["closed"]), ("closed", 1))
        self.assertEqual(self.book()["positions"], [])
        self.assertIn("closed the long", self.book()["trades"][0]["note"])

    def test_mixed_signal_and_wrong_market_are_refused(self):
        run = self.run_pass(scan([studied("BTC-USD"), studied("NVDA")], [pick("BTC-USD", "STRONG BUY", "short"), pick("NVDA", "STRONG BUY", "long")]))
        self.assertIn("mixed signals", run["decisions"][0]["note"])
        self.assertEqual(run["decisions"][1]["action"], "failed")  # the crypto-only book refuses a stock
        self.assertIn("crypto only", run["decisions"][1]["note"])

    def test_race_levels_follow_the_atr(self):
        with mock.patch.object(store, "levels", return_value={"atrPct": 4.37}):
            self.assertEqual(daily_scan.race_levels("SOL-USD"), {"stopPct": 4.37, "targetPct": 8.74, "atrPct": 4.37})
        with mock.patch.object(store, "levels", return_value={"atrPct": 0.6}):
            self.assertEqual(daily_scan.race_levels("KO")["stopPct"], 1.0)  # never tighter than the old 1%
        with mock.patch.object(store, "levels", return_value={"atrPct": 25.0}):
            self.assertEqual(daily_scan.race_levels("PONS-USD")["targetPct"], 20.0)  # capped at 10% / 20%
        with mock.patch.object(store, "levels", return_value={"atrPct": None}):
            self.assertEqual(daily_scan.race_levels("NEW-USD"), {"stopPct": 1.0, "targetPct": 2.0, "atrPct": None})

    def test_pass_keeps_claudes_summary_and_bar(self):
        t = {"ticker": "BTC-USD", "price": 100000.0, "side": None, "why": [], "agents": None, "kronos": {}, "claude": {}}
        doc = scan([t], [pick("BTC-USD", "SKIP", "none")])
        doc["picks"]["summary"] = "No trade today."
        run = self.run_pass(doc)
        self.assertEqual((run["summary"], run["bar"], run["decisions"][0]["reason"]), ("No trade today.", 50, "side"))

    def test_a_pick_against_a_strong_regime_is_not_opened(self):
        # Every view says long, but the coin market is in a strong short regime: the desk does not buy into it.
        self.regime.stop()
        with mock.patch.object(risk, "regime", return_value=regime("short", "strong")):
            run = self.run_pass(scan([studied("BTC-USD"), studied("SOL-USD", lean="short")],
                                     [pick("BTC-USD", "STRONG BUY", "long"), pick("SOL-USD", "STRONG SELL", "short")]))
        by = {d["ticker"]: d for d in run["decisions"]}
        self.assertEqual((by["BTC-USD"]["action"], by["BTC-USD"]["reason"], by["BTC-USD"]["score"]), ("skipped", "regime", 70))  # 90 - 20
        self.assertEqual((by["SOL-USD"]["action"], by["SOL-USD"]["score"]), ("opened", 95))  # with the regime: 90 + 5
        self.assertIn("strong short regime", run["regime"])
        self.assertEqual([p["ticker"] for p in self.book()["positions"]], ["SOL-USD"])
        self.regime.start()

    def test_against_a_lean_regime_costs_points_and_size_and_is_never_a_probe(self):
        self.regime.stop()
        with mock.patch.object(risk, "regime", return_value=regime("short", "lean")):
            run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
            d = run["decisions"][0]
            self.assertEqual((d["action"], d["score"], d["pct"]), ("opened", 80, 2.5))  # 90 - 10, and half of the 5%
            weak = studied("SOL-USD", verdict="TAKE SMALL", agents=None, fan=(180, 120), race=0.08, cal=0.5)
            run = self.run_pass(scan([weak], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
            self.assertEqual((run["opened"], run["decisions"][0]["reason"]), (0, "score"))  # 26 is under the probe floor of 35
            near = studied("SOL-USD", verdict="TAKE SMALL", agents=None)  # 53 - 10 = 43: over the probe floor, under the bar
            run = self.run_pass(scan([near], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
            self.assertEqual((run["opened"], run["decisions"][0]["score"], run["decisions"][0]["reason"]), (0, 43, "regime"))
        self.regime.start()

    def test_a_losing_streak_raises_the_bar_and_stops_the_probes(self):
        self.journal.stop()
        weak = studied("SOL-USD", verdict="TAKE SMALL", agents=None, fan=(180, 120), race=0.08, cal=0.5)  # scores 36
        with mock.patch.object(lessons, "journal", return_value=lost(3, side="short")):
            run = self.run_pass(scan([studied("ETH-USD", verdict="PASS")], [pick("ETH-USD", "STRONG BUY", "long")]))
            self.assertEqual((run["bar"], run["decisions"][0]["reason"]), (60, "score"))  # a 58 cleared 50, not 50 + 10
            self.assertIn("3 losses in a row", run["decisions"][0]["note"])
            run = self.run_pass(scan([weak], [pick("SOL-USD", "WORTH ATTENTION", "long")]))
            self.assertEqual(run["opened"], 0)  # three losses: no probes until a trade wins
        self.journal.start()

    def test_the_desks_record_on_a_side_moves_its_score(self):
        self.journal.stop()
        with mock.patch.object(lessons, "journal", return_value=lost(8, side="long") + [dict(lost(1)[0], pnl=5.0, pct=1.0, closed=time.time())]):
            run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        d = run["decisions"][0]  # longs won 1 of 9: 40 * 9/17 * (0.11 - 0.5) = -8 points
        self.assertEqual((d["action"], d["score"]), ("opened", 82))
        self.assertIn("the desk's record on longs -8", d["tally"])
        self.journal.start()

    def test_next_run_and_recover(self):
        w = workflows.create({"name": "Nightly", "portfolio": self.pid, "everyHours": 6})["workflows"][-1]
        self.assertGreater(w["nextRun"], w["created"])
        w = workflows.update(w["id"], {"enabled": False})["workflows"][-1]
        self.assertIsNone(w["nextRun"])
        w["runs"] = [{"id": "x" * 32, "status": "running"}]
        workflows._save(w)
        workflows.recover()
        self.assertEqual(workflows._load(w["id"])["runs"][0]["status"], "interrupted")
        workflows.delete(w["id"])
        with self.assertRaises(ValueError):
            workflows._load(w["id"])


if __name__ == "__main__":
    unittest.main()
