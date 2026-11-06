"""Offline tests for the desk's workflow rules (tradingagents/dashboard/workflows.py):
validation, and how one pass turns Claude's picks into paper trades. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_workflows.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-workflows-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402
import portfolios  # noqa: E402
import store  # noqa: E402
import workflows  # noqa: E402

PRICES = {"BTC-USD": {"price": 100000.0, "previous": 99000.0}, "SOL-USD": {"price": 200.0, "previous": 210.0},
          "ETH-USD": {"price": 4000.0, "previous": 4100.0}, "NVDA": {"price": 180.0, "previous": 179.0}}


def pick(ticker, label, side, size=8.0, why="lines up"):
    return {"ticker": ticker, "label": label, "side": side, "size_pct": size, "confidence": "high", "why": why}


def studied(ticker, verdict="TAKE", pull=False, plan=None, review_id=None):
    return {"ticker": ticker, "price": PRICES[ticker]["price"], "source": "coin market", "side": "long", "why": [],
            "kronos": {"id": "k" * 32, "status": "done", "result": {"reversion": {"flagged": pull}, "plan": plan}},
            "claude": {"id": review_id, "status": "done", "verdict": verdict, "side": "long", "size_pct": 8}}


def scan(tickers, picks):
    return {"id": "s" * 32, "status": "done", "market": "crypto", "tickers": tickers, "picks": {"summary": "", "picks": picks}}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.pid = portfolios.create({"name": f"Coins {self._testMethodName}"[:40], "rule": "crypto", "cash": 10000, "activate": True})["active"]
        self.w = workflows.clean({"name": "Crypto every 4h", "portfolio": self.pid})
        self.w.update(id="w" * 32, runs=[], lastRun=None)
        self.quotes = mock.patch.object(store, "quotes", side_effect=lambda t: {x: PRICES.get(x, {"price": None}) for x in t})
        self.quotes.start()
        self.notify = mock.patch.object(workflows.checks, "notify")
        self.notify.start()
        self.review = mock.patch.object(claude_review, "get", return_value={"review": {"stop": 97000.0, "target": 106000.0}})
        self.review.start()

    def tearDown(self):
        mock.patch.stopall()

    def run_pass(self, doc):
        run = {"id": "r" * 32, "decisions": [], "stage": ""}
        workflows._decide(self.w, run, doc)
        return run

    def book(self):
        return portfolios.book(self.pid)

    def test_defaults_and_validation(self):
        self.assertEqual((self.w["market"], self.w["limit"], self.w["everyHours"], self.w["sizePct"]), ("crypto", 10, 4, 10.0))
        with self.assertRaises(ValueError):
            workflows.clean({"name": "", "portfolio": self.pid})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": "nope"})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "market": "stocks"})  # a crypto-only book
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "stopPct": 5, "targetPct": 4})
        with self.assertRaises(ValueError):
            workflows.clean({"name": "x", "portfolio": self.pid, "sizePct": 50})

    def test_strong_buy_opens_a_long_with_claudes_levels(self):
        run = self.run_pass(scan([studied("BTC-USD", review_id="c" * 32)], [pick("BTC-USD", "STRONG BUY", "long")]))
        d = run["decisions"][0]
        self.assertEqual(d["action"], "opened")
        self.assertEqual((d["stop"], d["target"], d["levels"]), (97000.0, 106000.0, "Claude's levels"))
        self.assertAlmostEqual(d["quantity"], 0.008, places=4)  # 8% of 10,000 at 100,000, 4 decimals
        pos = self.book()["positions"][0]
        self.assertEqual((pos["ticker"], pos["stop"], pos["target"]), ("BTC-USD", 97000.0, 106000.0))
        self.assertIn("review_by", pos)
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Workflow Crypto every 4h: STRONG BUY"))

    def test_size_is_capped_by_the_rule(self):
        self.w["sizePct"] = 5.0
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG BUY", "long", size=20)]))
        self.assertEqual(run["decisions"][0]["pct"], 5.0)
        self.assertAlmostEqual(run["decisions"][0]["quantity"], 2.5, places=4)

    def test_skips_window_pull_pass_and_weak_labels(self):
        doc = scan([studied("BTC-USD", pull=True), studied("SOL-USD", verdict="PASS"), studied("ETH-USD")],
                   [pick("BTC-USD", "STRONG BUY", "long"), pick("SOL-USD", "STRONG BUY", "long"), pick("ETH-USD", "WORTH ATTENTION", "long")])
        run = self.run_pass(doc)
        notes = {d["ticker"]: (d["action"], d["note"]) for d in run["decisions"]}
        self.assertEqual(notes["BTC-USD"][0], "skipped"); self.assertIn("window pull", notes["BTC-USD"][1])
        self.assertEqual(notes["SOL-USD"][0], "skipped"); self.assertIn("PASS", notes["SOL-USD"][1])
        self.assertEqual(notes["ETH-USD"][0], "skipped"); self.assertIn("not a trade", notes["ETH-USD"][1])
        self.assertEqual(self.book()["positions"], [])

    def test_attention_label_trades_when_allowed(self):
        self.w["minLabel"] = "attention"
        run = self.run_pass(scan([studied("ETH-USD")], [pick("ETH-USD", "WORTH ATTENTION", "long")]))
        self.assertEqual(run["decisions"][0]["action"], "opened")

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

    def test_strong_sell_opens_a_short_unless_shorts_are_off(self):
        self.w["allowShort"] = False
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG SELL", "short")]))
        self.assertIn("shorts are off", run["decisions"][0]["note"])
        self.w["allowShort"] = True
        run = self.run_pass(scan([studied("SOL-USD")], [pick("SOL-USD", "STRONG SELL", "short")]))
        self.assertEqual(run["decisions"][0]["action"], "opened")
        pos = self.book()["positions"][0]
        self.assertLess(pos["quantity"], 0)
        self.assertEqual((pos["stop"], pos["target"]), (206.0, 188.0))  # Claude's long levels don't fit a short: 3% / 6% fallback

    def test_reverse_pick_closes_the_held_position(self):
        self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        self.assertEqual(len(self.book()["positions"]), 1)
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG BUY", "long")]))
        self.assertIn("already long", run["decisions"][0]["note"])
        run = self.run_pass(scan([studied("BTC-USD")], [pick("BTC-USD", "STRONG SELL", "short")]))
        self.assertEqual((run["decisions"][0]["action"], run["closed"]), ("closed", 1))
        self.assertEqual(self.book()["positions"], [])
        self.assertIn("closed the long", self.book()["trades"][0]["note"])

    def test_mixed_signal_and_wrong_market_are_refused(self):
        run = self.run_pass(scan([studied("BTC-USD"), studied("NVDA")], [pick("BTC-USD", "STRONG BUY", "short"), pick("NVDA", "STRONG BUY", "long")]))
        self.assertIn("mixed signals", run["decisions"][0]["note"])
        self.assertEqual(run["decisions"][1]["action"], "failed")  # the crypto-only book refuses a stock
        self.assertIn("crypto only", run["decisions"][1]["note"])

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
