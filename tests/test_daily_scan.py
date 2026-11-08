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


if __name__ == "__main__":
    unittest.main()
