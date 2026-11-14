"""Offline tests for the daily scan's pipeline (tradingagents/dashboard/daily_scan.py): Kronos runs only
on tickers the agents gave a direction, and the scan says so. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_daily_scan.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-scan-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402
import daily_scan  # noqa: E402
import store  # noqa: E402


class FakeKronos:
    """Stands in for the kronos module: every scan finishes at once with a short-leaning fan."""

    def __init__(self):
        self.DIR = Path(tempfile.mkdtemp(prefix="kronos-"))
        self.started = []

    def overview(self):
        return {"running": None}

    def start(self, body):
        kid = f"{len(self.started):032x}"
        self.started.append(body["ticker"])
        store.write_json(self.DIR / f"{kid}.meta.json", {"status": "done"})
        return kid

    def _summary(self, meta):
        return {"result": {"side": "short", "up": 60, "down": 240, "n": 300, "calibration": 0.6}}


def doc(tickers, agents=True, kronos=True):
    return {"id": "d" * 32, "market": "crypto", "status": "running", "stage": "Starting", "progress": 0, "created": 0,
            "config": {"limit": len(tickers), "agents": agents, "kronos": kronos, "depth": "quick", "paths": 300, "calibrateDays": 2},
            "notes": [], "tickers": [{"ticker": t, "source": "coin market", "side": "short", "score": 4, "why": ["breakdown"], "price": 1.0,
                                      "agents": {}, "kronos": {}, "claude": {}} for t in tickers],
            "skipped": [], "picks": None, "model": "sonnet", "workflow": None}


class DailyScanTest(unittest.TestCase):
    def setUp(self):
        self.kronos = FakeKronos()
        self.jobs, self.decisions = {}, {}

        def enqueue(job):
            jid = f"{len(self.jobs):032x}"
            self.jobs[jid] = {"id": jid, "status": "done", "decision": self.decisions[job["ticker"]], "report": f"{job['ticker']}_20261108_120000"}
            return jid

        daily_scan.configure(enqueue=enqueue, jobs=self.jobs, cancel_job=lambda j: None)
        self.patches = [
            mock.patch.object(daily_scan, "kronos", self.kronos),
            mock.patch.object(daily_scan, "race_levels", return_value={"stopPct": 2.0, "targetPct": 4.0, "atrPct": 2.0}),
            mock.patch.object(daily_scan, "_desk_call", return_value=None),
            mock.patch.object(daily_scan, "_portfolio_for", return_value="main"),
            mock.patch.object(daily_scan, "_rank", side_effect=lambda d: {"summary": "ranked", "picks": [], "models": []}),
            mock.patch.object(daily_scan, "WAIT_STEP", 0.001),
            mock.patch.object(claude_review, "overview", return_value={"running": None}),
            mock.patch.object(claude_review, "start", side_effect=lambda body: "r" * 32),
            mock.patch.object(claude_review, "get", return_value={"status": "done", "review": {"verdict": "PASS", "side": "none"}}),
        ]
        for p in self.patches:
            p.start()
        daily_scan._running.update(id="d" * 32, cancel=False)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_kronos_skips_tickers_the_agents_gave_no_direction(self):
        self.decisions = {"AAA-USD": "Underweight", "BBB-USD": "Hold", "CCC-USD": "Overweight"}
        d = doc(["AAA-USD", "BBB-USD", "CCC-USD"])
        daily_scan._run(d)
        self.assertEqual(d["status"], "done")
        self.assertEqual(self.kronos.started, ["AAA-USD", "CCC-USD"])
        by = {t["ticker"]: t for t in d["tickers"]}
        self.assertEqual(by["BBB-USD"]["kronos"]["status"], "skipped")
        self.assertIn("Hold", by["BBB-USD"]["kronos"]["why"])
        self.assertEqual(by["AAA-USD"]["kronos"]["status"], "done")
        self.assertEqual(by["AAA-USD"]["kronos"]["result"]["down"], 240)
        self.assertTrue(any("BBB-USD" in n and "no direction" in n for n in d["notes"]))
        self.assertEqual(d["progress"], 100)

    def test_kronos_runs_on_everything_when_the_agents_are_off_or_failed(self):
        self.decisions = {"AAA-USD": "Hold", "BBB-USD": "Hold"}
        d = doc(["AAA-USD", "BBB-USD"], agents=False)
        daily_scan._run(d)
        self.assertEqual(self.kronos.started, ["AAA-USD", "BBB-USD"])
        self.kronos.started.clear()
        self.decisions = {"AAA-USD": None}  # the agents' job failed: no recommendation, so Kronos still runs
        d = doc(["AAA-USD"])
        daily_scan._run(d)
        self.assertEqual(self.kronos.started, ["AAA-USD"])
        self.assertEqual(d["notes"], [])


class CandidatesTest(unittest.TestCase):
    """What a workflow leaves out of its scan: the candidates it names are skipped with its reason, and keep the
    screen's side and whether they outranked a studied ticker."""

    def setUp(self):
        coin = lambda t, score, side: {"ticker": t, "name": t, "score": score, "side": side, "cap": 5e9, "why": ["x"], "price": 1.0}
        rows = [coin("AAA-USD", 9, "long"), coin("BBB-USD", 8, "short"), coin("CCC-USD", 7, "long"), coin("DDD-USD", 6, "short"), coin("EEE-USD", 5, "long")]
        for p in (mock.patch.object(daily_scan.watchlist, "screen", return_value={"rows": []}),
                  mock.patch.object(daily_scan.watchlist, "coin_opportunities", return_value={"rows": rows})):
            p.start(); self.addCleanup(p.stop)

    def test_named_tickers_are_left_out_and_the_scan_moves_down_the_screens(self):
        skip = {"AAA-USD": "held long in Coins; its review looks after it", "EEE-USD": "studied at 11:11 and passed over (score); it rests for 24 hours"}
        picked, skipped = daily_scan.candidates("crypto", 2, False, skip)
        self.assertEqual([c["ticker"] for c in picked], ["BBB-USD", "CCC-USD"])
        self.assertEqual(skipped, [{"ticker": "AAA-USD", "reason": skip["AAA-USD"], "side": "long", "slot": True},   # it outranked a studied ticker
                                   {"ticker": "EEE-USD", "reason": skip["EEE-USD"], "side": "long", "slot": False}])  # it would not have been studied
        self.assertEqual([c["ticker"] for c in daily_scan.candidates("crypto", 2, False)[0]], ["AAA-USD", "BBB-USD"])

    def test_a_scan_with_nothing_new_says_so(self):
        everything = {t: "held" for t in ("AAA-USD", "BBB-USD", "CCC-USD", "DDD-USD", "EEE-USD")}
        with mock.patch.object(daily_scan.claude_review, "ready", return_value={"ready": True, "reason": ""}), \
                mock.patch.object(store, "settings", return_value={"ready": True}), self.assertRaises(daily_scan.NothingToStudy):
            daily_scan.start({"market": "crypto", "limit": 2, "fresh": False, "skip": everything})


class StockPoolTest(unittest.TestCase):
    """A stock scan's pool: the watchlist's stocks plus the market's busiest movers, deep enough that a workflow
    resting what it passed over still finds new tickers."""

    def test_the_pool_is_the_watchlist_plus_thirty_movers_with_a_side(self):
        stock = lambda t, score, side: {"ticker": t, "crypto": False, "source": "market movers", "side": side, "score": score,
                                        "why": ["x"], "price": 10.0}
        movers = [stock(f"M{i:02d}", 30 - i, "long") for i in range(26)] + [stock("FLAT", 40, None), stock("NVDA", 50, "short")]
        watched = {"rows": [{"ticker": "NVDA", "crypto": False, "error": None, "signals": ["volume 2.0× avg"], "bias": "long", "interest": 3, "price": 190.0},
                            {"ticker": "SOL-USD", "crypto": True, "error": None, "signals": ["x"], "bias": "long", "interest": 9, "price": 150.0}]}
        with mock.patch.object(daily_scan.watchlist, "screen", return_value=watched), \
                mock.patch.object(daily_scan.backtests, "movers", return_value=movers) as asked, \
                mock.patch.object(daily_scan, "_chart_setups"):
            picked, skipped = daily_scan.candidates("stocks", 10, False, {"M00": "held long in Stocks Only; its review looks after it"})
            pool = daily_scan.candidates("stocks", 20, False)[0] + daily_scan.candidates("stocks", 20, False, {f"M{i:02d}": "resting" for i in range(19)})[0]
        asked.assert_called_with(30)
        self.assertEqual([c["ticker"] for c in picked], [f"M{i:02d}" for i in range(1, 11)])   # busiest first, the held one left out
        self.assertEqual(skipped, [{"ticker": "M00", "reason": "held long in Stocks Only; its review looks after it", "side": "long", "slot": True}])
        names = {c["ticker"] for c in pool}
        self.assertEqual(len(names), 27)                 # 26 movers and the watchlist's stock: more than one pass deep
        self.assertNotIn("FLAT", names)                  # a mover with no side is not a candidate
        self.assertNotIn("SOL-USD", names)               # a coin is not a stock
        self.assertEqual(next(c for c in pool if c["ticker"] == "NVDA")["source"], "your watchlist")  # the watchlist's row wins

    def test_movers_are_the_busiest_names_off_the_watchlist_that_show_a_signal(self):
        backtests = daily_scan.backtests
        quote = lambda t, d1, vol: {"ticker": t, "d1": d1, "volRatio": vol}
        screens = {"day_losers": [quote("DOWN", -8.0, 2.0), quote("NVDA", -9.0, 3.0)], "day_gainers": [quote("UP", 12.0, 3.0), quote("QUIET", 6.0, 1.0)],
                   "most_actives": [quote("BUSY", 1.0, 5.0)]}
        row = lambda t: {"ticker": t, "crypto": False, "error": None, "signals": [] if t == "QUIET" else ["volume 2.0× avg"], "bias": "long",
                         "interest": 2, "price": 10.0}
        backtests._movers.clear()
        self.addCleanup(backtests._movers.clear)
        with mock.patch.object(backtests.watchlist, "screen_stocks", side_effect=lambda name, cap, vol: {"rows": screens[name]}), \
                mock.patch.object(backtests.watchlist, "load", return_value=["NVDA"]), \
                mock.patch.object(backtests.watchlist, "_row", side_effect=row) as screened:
            self.assertEqual([m["ticker"] for m in backtests.movers(3)], ["UP", "DOWN"])   # QUIET took a slot and showed no signal
            self.assertEqual([c.args[0] for c in screened.call_args_list], ["UP", "DOWN", "QUIET"])
            self.assertEqual([m["ticker"] for m in backtests.movers(30)], ["UP", "DOWN", "BUSY"])
            backtests.movers(30)
            self.assertEqual(screened.call_count, 7)     # the second ask of the same depth came from the cache


if __name__ == "__main__":
    unittest.main()
