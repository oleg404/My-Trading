"""Offline tests for the desk's exit-plan rules (tradingagents/dashboard/portfolios.py and checks.py): every
position gets a stop, a target and a review date, and a review that has come due re-runs the agents and renews
the plan from their report. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_checks.py -v
"""

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-checks-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import checks  # noqa: E402
import portfolios  # noqa: E402
import position_review  # noqa: E402
import store  # noqa: E402

TODAY = date.today().isoformat()
IN_THREE = (date.today() + timedelta(days=portfolios.REVIEW_DAYS)).isoformat()


def levels(atr_pct):
    return mock.patch.object(store, "levels", return_value={"atrPct": atr_pct})


def quotes(**prices):
    return mock.patch.object(store, "quotes", lambda tickers: {t: {"price": prices.get(t), "previous": None} for t in tickers})


class PlanTest(unittest.TestCase):
    def setUp(self):
        store.DASH.mkdir(parents=True, exist_ok=True)
        portfolios.DIR.mkdir(parents=True, exist_ok=True)
        for f in list(portfolios.DIR.glob("*.json")) + [portfolios.REGISTRY]:
            f.unlink(missing_ok=True)
        portfolios._migrate(force_new=True)
        portfolios.set_cash({"cash": 100000})

    def test_a_long_with_no_plan_gets_an_atr_stop_below_and_a_target_above(self):
        with levels(2.0), quotes():
            data = portfolios.record_trade({"ticker": "ZEC-USD", "side": "buy", "quantity": 2, "price": 1000})
        p = data["positions"][0]
        self.assertEqual((p["stop"], p["target"], p["review_by"]), (980.0, 1040.0, IN_THREE))
        self.assertIn("2% stop, 4% target", p["levels"])

    def test_a_shorts_stop_sits_above_and_its_target_below(self):
        with levels(None), quotes():
            p = portfolios.record_trade({"ticker": "LTC-USD", "side": "sell", "quantity": 1, "price": 50})["positions"][0]
        self.assertEqual((p["stop"], p["target"]), (50.5, 49.0))  # 1% / 2% when the ATR is unknown

    def test_the_trades_own_levels_win_and_only_the_gap_is_filled(self):
        with levels(2.0), quotes():
            p = portfolios.record_trade({"ticker": "AAPL", "side": "buy", "quantity": 1, "price": 100, "stop": 97, "reviewBy": "2030-01-01"})["positions"][0]
        self.assertEqual((p["stop"], p["target"], p["review_by"]), (97.0, 104.0, "2030-01-01"))
        self.assertIn("(target only)", p["levels"])

    def test_clearing_a_field_in_the_plan_form_refills_it_at_the_live_price(self):
        with levels(2.0), quotes(AAPL=100.0):
            portfolios.record_trade({"ticker": "AAPL", "side": "buy", "quantity": 1, "price": 100, "stop": 97, "target": 110})
        with levels(2.0), quotes(AAPL=200.0):
            p = portfolios.set_plan({"ticker": "AAPL", "stop": "", "target": "", "reviewBy": ""})["positions"][0]
        self.assertEqual((p["stop"], p["target"], p["review_by"]), (196.0, 208.0, IN_THREE))

    def test_ensure_plans_backfills_old_positions_at_the_live_price(self):
        data = portfolios.book()
        data["positions"] = [{"ticker": "DOGE-USD", "quantity": 1000, "average_price": 0.07}, {"ticker": "XLM-USD", "quantity": -10, "average_price": 0.15, "stop": 0.16, "target": 0.14, "review_by": "2030-01-01"}]
        portfolios._save(data)
        with levels(5.0), quotes(**{"DOGE-USD": 0.08}):
            filled = portfolios.ensure_plans()
        self.assertEqual([(f["ticker"], f["filled"]) for f in filled], [("DOGE-USD", ["stop", "target", "review_by"])])
        doge = portfolios.book()["positions"][0]
        self.assertEqual((doge["stop"], doge["target"], doge["review_by"]), (0.076, 0.088, IN_THREE))
        with levels(5.0), quotes():
            self.assertEqual(portfolios.ensure_plans(), [])  # nothing left to fill


class FakeDesk:
    def __init__(self):
        self.jobs, self.queued = {}, []

    def enqueue(self, job):
        job_id = f"job{len(self.jobs) + 1:02d}"
        job.update(id=job_id, status="queued")
        self.jobs[job_id] = job
        self.queued.append(job)
        return job_id


class AutoReviewTest(unittest.TestCase):
    def setUp(self):
        PlanTest.setUp(self)
        self.desk = FakeDesk()
        checks.configure(enqueue=self.desk.enqueue, jobs=self.desk.jobs)
        checks.save_prefs({"checkTime": "09:35", "notify": False, "autoReanalyze": True, "autoExit": True})
        self.asked = mock.patch.object(position_review, "start").start()  # Claude's hold-or-halve call: never a real one here
        self.addCleanup(mock.patch.stopall)
        patches = [mock.patch.object(store, "settings", return_value={"ready": True}), mock.patch.object(checks, "notify")]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        with levels(2.0), quotes():
            portfolios.record_trade({"ticker": "LTC-USD", "side": "sell", "quantity": 2, "price": 52, "stop": 54.6, "target": 47.41, "reviewBy": TODAY})
            portfolios.record_trade({"ticker": "AXS-USD", "side": "sell", "quantity": 100, "price": 0.81, "stop": 0.85, "target": 0.73, "reviewBy": "2030-01-01"})

    def position(self, ticker="LTC-USD"):
        return next(p for p in portfolios.book()["positions"] if p["ticker"] == ticker)

    def test_a_due_review_queues_the_agents_once_with_the_book(self):
        with quotes():
            out = checks.auto_review()
        self.assertEqual([(o["ticker"], o["action"]) for o in out], [("LTC-USD", "queued")])
        job = self.desk.queued[0]
        self.assertEqual((job["ticker"], job["usePortfolio"], job["portfolio"], job["review"]), ("LTC-USD", True, "main", True))
        self.assertNotIn("fundamentals", job["analysts"])
        self.assertEqual(self.position()["review_run"], {"job": "job01", "date": TODAY})
        with quotes():
            self.assertEqual(checks.auto_review(), [])  # still running: not queued again
        self.assertEqual(len(self.desk.queued), 1)

    def test_a_finished_review_renews_the_plan_from_the_report(self):
        with quotes():
            checks.auto_review()
        self.desk.jobs["job01"].update(status="done", report="LTC-USD_20261109_120000", decision="SELL")
        with quotes(**{"LTC-USD": 52.0}), mock.patch.object(store, "read_report", return_value={"stop": 55.0, "target": "$45 by year end"}):
            out = checks.auto_review()
        self.assertEqual((out[0]["action"], out[0]["verdict"]), ("renewed", "SELL"))
        p = self.position()
        self.assertEqual((p["stop"], p["target"], p["review_by"], p["source"]), (55.0, 45.0, IN_THREE, "LTC-USD_20261109_120000"))
        self.assertEqual((p["review_verdict"], p["review_date"], p["levels"]), ("SELL", TODAY, "the agents' plan"))
        self.assertNotIn("review_run", p)
        self.assertEqual(self.asked.call_args.args[:2], ("main", "LTC-USD"))  # then Claude is asked: hold, or close half
        self.assertEqual(self.asked.call_args.args[2]["report"], "LTC-USD_20261109_120000")

    def test_the_half_close_switch_off_asks_nobody(self):
        checks.save_prefs({"checkTime": "09:35", "notify": False, "autoReanalyze": True, "autoExit": True, "autoHalf": False})
        with quotes():
            checks.auto_review()
        self.desk.jobs["job01"].update(status="done", report="LTC-USD_20261109_120000", decision="SELL")
        with quotes(**{"LTC-USD": 52.0}), mock.patch.object(store, "read_report", return_value={}):
            checks.auto_review()
        self.asked.assert_not_called()

    def test_report_levels_on_the_wrong_side_keep_the_old_ones(self):
        with quotes():
            checks.auto_review()
        self.desk.jobs["job01"].update(status="done", report="LTC-USD_20261109_120000", decision="BUY")
        with quotes(**{"LTC-USD": 52.0}), mock.patch.object(store, "read_report", return_value={"stop": 48.0, "target": "60"}):  # a long's plan on a short
            checks.auto_review()
        p = self.position()
        self.assertEqual((p["stop"], p["target"], p["review_by"], p["review_verdict"]), (54.6, 47.41, IN_THREE, "BUY"))

    def test_a_failed_run_waits_for_tomorrow(self):
        with quotes():
            checks.auto_review()
        self.desk.jobs["job01"]["status"] = "failed"
        with quotes():
            self.assertEqual(checks.auto_review(), [])
        self.assertEqual(len(self.desk.queued), 1)
        data = portfolios.book()
        self.position()  # the run marker stays until tomorrow
        data["positions"][0]["review_run"]["date"] = "2000-01-01"
        portfolios._save(data)
        with quotes():
            out = checks.auto_review()
        self.assertEqual([o["action"] for o in out], ["queued"])
        self.assertEqual(len(self.desk.queued), 2)

    def test_an_old_job_without_a_kind_does_not_stop_reviews(self):
        self.desk.jobs["legacy"] = {"id": "legacy", "status": "done"}  # a saved job from before jobs carried a kind
        with quotes():
            out = checks.auto_review()
        self.assertEqual([(o["ticker"], o["action"]) for o in out], [("LTC-USD", "queued")])

    def test_the_switch_off_leaves_reviews_to_the_user(self):
        checks.save_prefs({"checkTime": "09:35", "notify": False, "autoReanalyze": False, "autoExit": True})
        with quotes():
            self.assertEqual(checks.auto_review(), [])
        self.assertEqual(self.desk.queued, [])

    def test_the_daily_check_names_the_running_review(self):
        with quotes():
            checks.auto_review()
        with quotes(**{"LTC-USD": 52.0, "AXS-USD": 0.81}), mock.patch.object(checks, "_indicators", return_value=None):
            result = checks.run_check()
        notes = {i["ticker"]: i["notes"] for i in result["items"]}
        self.assertTrue(any(n.startswith("REVIEW RUNNING") for n in notes["LTC-USD"]))
        self.assertEqual(notes["AXS-USD"], ["on plan"])


if __name__ == "__main__":
    unittest.main()
