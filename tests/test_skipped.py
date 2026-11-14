"""Offline tests for what the desk's skipped picks did afterwards (tradingagents/dashboard/skipped.py): which
skipped picks are followed, the paper plan each gets, how daily bars settle it, and what the record says about
the score bar. No model, no network: the daily bars are made up.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_skipped.py -v
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOME = tempfile.mkdtemp(prefix="desk-skipped-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import skipped  # noqa: E402
import store  # noqa: E402

DAY0 = date(2026, 10, 1)
AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp()   # the pass ran on bar 19


def bars(after=(), n=20, price=100.0, spread=1.0):
    """`n` quiet days around `price` (a range of 2 x spread, so the ATR is 2% of 100), then one bar per (high, low, close) in `after`."""
    quiet = [(price + spread, price - spread, price)] * n
    return [{"date": (DAY0 + timedelta(days=i)).isoformat(), "high": h, "low": lo, "close": c} for i, (h, lo, c) in enumerate(quiet + list(after))]


def decision(ticker="BTC-USD", reason="score", score=45, side="long", action="skipped", **more):
    return {"ticker": ticker, "action": action, "reason": reason, "score": score, "deskSide": side, **more}


def run(decisions, started=AT, bar=50, rid="r1"):
    return {"id": rid, "started": started, "bar": bar, "decisions": decisions}


def rec(group="near", pct=None, outcome=None, market="crypto", at=AT, **more):
    return {"id": f"x{time.time_ns()}", "ticker": "BTC-USD", "market": market, "side": "long", "score": 45, "bar": 50, "reason": "score",
            "group": group, "workflow": "w", "at": at, "quote": None, "entry": 100.0, "stopPct": 2.0, "targetPct": 4.0,
            "outcome": outcome or (None if pct is None else "target" if pct > 0 else "stop"), "pct": pct, "settled": None if pct is None else "2026-10-22", **more}


class CollectTest(unittest.TestCase):
    def test_skipped_picks_are_grouped_by_why(self):
        w = {"name": "Crypto every 4h", "runs": [run([
            decision("BTC-USD", "score", 45), decision("ETH-USD", "score", 30), decision("SOL-USD", "review", 20, "short"),
            decision("XRP-USD", "regime", 70), decision("ADA-USD", "invested", 60), decision("NVDA", "cooldown", 55),
            decision("DOT-USD", "held", 80), decision("LTC-USD", "side", 0, None), decision("BNB-USD", None, 90, action="opened")])]}
        new = skipped.collect([w], [])
        self.assertEqual({r["ticker"]: r["group"] for r in new},
                         {"BTC-USD": "near", "ETH-USD": "far", "SOL-USD": "review", "XRP-USD": "rule", "ADA-USD": "room", "NVDA": "rule"})
        nvda = next(r for r in new if r["ticker"] == "NVDA")
        self.assertEqual((nvda["market"], nvda["workflow"], nvda["bar"], nvda["outcome"]), ("stocks", "Crypto every 4h", 50, None))

    def test_a_ticker_and_side_is_followed_once_per_window(self):
        later, week = AT + 2 * 86400, AT + 8 * 86400
        w = {"name": "w", "runs": [run([decision()], rid="a"), run([decision(), decision(side="short")], later, rid="b"), run([decision()], week, rid="c")]}
        new = skipped.collect([w], [])
        self.assertEqual([(r["id"], r["side"]) for r in new], [("a:BTC-USD", "long"), ("b:BTC-USD", "short"), ("c:BTC-USD", "long")])
        self.assertEqual(skipped.collect([w], new), [])   # nothing twice


class SettleTest(unittest.TestCase):
    def follow(self, after, side="long", quote=None, today="2026-11-30"):
        r = skipped.collect([{"name": "w", "runs": [run([decision(side=side, quote=quote)])]}], [])[0]
        skipped.settle(r, bars(after), today)
        return r

    def test_the_plan_is_one_atr_for_the_stop_and_two_for_the_target(self):
        r = self.follow([(100.5, 99.5, 100.0)], today="2026-10-22")
        self.assertEqual((r["entry"], r["stopPct"], r["targetPct"], r["outcome"]), (100.0, 2.0, 4.0, None))   # still inside its window

    def test_a_long_reaches_its_target_or_its_stop(self):
        win = self.follow([(101.0, 99.0, 100.5), (104.5, 100.0, 104.0)])
        self.assertEqual((win["outcome"], win["pct"], win["settled"]), ("target", 4.0, "2026-10-22"))
        loss = self.follow([(101.0, 97.9, 98.5)])
        self.assertEqual((loss["outcome"], loss["pct"]), ("stop", -2.0))
        both = self.follow([(105.0, 97.0, 100.0)])
        self.assertEqual(both["outcome"], "stop")   # one bar reaches both: the stop counts

    def test_a_short_wins_when_the_price_falls(self):
        win = self.follow([(100.5, 95.5, 96.0)], side="short")
        self.assertEqual((win["outcome"], win["pct"]), ("target", 4.0))
        loss = self.follow([(102.5, 99.5, 102.0)], side="short")
        self.assertEqual((loss["outcome"], loss["pct"]), ("stop", -2.0))

    def test_neither_reached_closes_at_the_last_close_of_the_window(self):
        drift = [(101.0, 99.0, 100.0 + i * 0.2) for i in range(1, 12)]   # eleven quiet days, seven of them in the window
        r = self.follow(drift)
        self.assertEqual((r["outcome"], r["pct"], r["settled"]), ("timeout", 1.4, "2026-10-27"))
        self.assertIsNone(self.follow(drift[:3], today="2026-10-23")["outcome"])   # the window has not run out: pending

    def test_the_price_the_pass_saw_is_the_entry(self):
        r = self.follow([(104.5, 100.5, 104.0)], quote=101.0, today="2026-10-22")
        self.assertEqual((r["entry"], r["outcome"]), (101.0, None))   # the target is 105.04 from 101, not 104 from the close

    def test_an_old_pick_waits_for_its_days_bar_to_finish(self):
        r = skipped.collect([{"name": "w", "runs": [run([decision()])]}], [])[0]
        self.assertFalse(skipped.settle(r, bars(), "2026-10-20"))   # no price from the pass, and bar 19 is the last one
        self.assertIsNone(r["entry"])
        self.assertFalse(skipped.settle(r, bars(n=10), "2026-10-20"))   # no bar for the day yet

    def test_stock_days_are_new_york_days(self):
        late = datetime(2026, 10, 21, 2, tzinfo=timezone.utc).timestamp()   # 10 pm on the 20th in New York
        self.assertEqual((skipped.entry_day(late, "stocks"), skipped.entry_day(late, "crypto")), ("2026-10-20", "2026-10-21"))


class ReportTest(unittest.TestCase):
    def test_too_few_picks_is_no_verdict(self):
        s = skipped.summary([rec(pct=4.0), rec(pct=-2.0), rec()], "crypto", AT)
        near = s["groups"][0]
        self.assertEqual((near["n"], near["pending"], near["wins"], near["avgPct"]), (2, 1, 1, 1.0))
        self.assertEqual(s["verdicts"], ["Too few picks just under the bar have played out to judge it: 2 of the 8 needed."])

    def test_winners_under_the_bar_say_the_bar_may_be_too_high(self):
        s = skipped.summary([rec(pct=4.0)] * 6 + [rec(pct=-2.0)] * 2, "crypto", AT + 30 * 86400)
        self.assertIn("6 of 8 would have won, +2.5% on average", s["verdicts"][0])
        self.assertIn("may be keeping winners out", s["verdicts"][0])
        self.assertEqual(len(s["verdicts"]), 1)   # a month of record: no caution

    def test_losers_under_the_bar_say_it_is_doing_its_job(self):
        s = skipped.summary([rec(pct=4.0)] * 2 + [rec(pct=-2.0)] * 6, "crypto", AT + 30 * 86400)
        self.assertIn("The bar is doing its job.", s["verdicts"][0])

    def test_the_picks_that_cleared_the_bar_are_the_yardstick(self):
        near = [rec(pct=4.0)] * 4 + [rec(pct=-2.0)] * 4                       # +1.0% on average
        room = [rec("room", pct=4.0)] * 7 + [rec("room", pct=-2.0)]           # +3.25%
        s = skipped.summary(near + room, "crypto", AT + 30 * 86400)
        self.assertIn("less than the ones that cleared it (+3.2%)", s["verdicts"][0])
        self.assertIn("Room in the book, not the bar", s["verdicts"][1])

    def test_claudes_no_is_judged_too_and_a_young_record_is_flagged(self):
        s = skipped.summary([rec("review", pct=4.0)] * 6 + [rec("review", pct=-2.0)] * 2, "crypto", AT + 5 * 86400)
        self.assertIn("Its no kept winners out.", s["verdicts"][1])
        self.assertIn("All of this comes from the last 6 days", s["verdicts"][2])
        s = skipped.summary([rec("review", pct=-2.0)] * 8, "crypto", AT + 30 * 86400)
        self.assertIn("Its no was right.", s["verdicts"][1])

    def test_markets_are_kept_apart(self):
        s = skipped.summary([rec(pct=4.0, market="stocks")] * 3, "crypto", AT)
        self.assertEqual((s["all"]["n"], s["days"]), (0, 0))


class RefreshTest(unittest.TestCase):
    def test_refresh_adds_settles_and_saves(self):
        folder = store.DASH / "workflows"
        folder.mkdir(parents=True, exist_ok=True)
        store.write_json(folder / "w.json", {"name": "Crypto every 4h", "runs": [run([decision("BTC-USD"), decision("ETH-USD", "review", 20, "short")],
                                                                              started=time.time() - 5 * 86400)]})
        today = date.today()
        made = [{"date": (today - timedelta(days=40 - i)).isoformat(), "high": 101.0, "low": 99.0, "close": 100.0} for i in range(40)]
        made.append({"date": today.isoformat(), "high": 105.0, "low": 100.0, "close": 104.5})   # up 4.5% today
        with mock.patch.object(store, "daily_bars", return_value=made) as fetch:
            out = skipped.refresh()
            self.assertEqual(out, {"new": 2, "settled": 2, "total": 2})
            self.assertEqual(skipped.refresh(), {"new": 0, "settled": 0, "total": 2})   # nothing pending: nothing fetched again
            self.assertEqual(fetch.call_count, 2)
        by = {r["ticker"]: r for r in skipped.load()}
        self.assertEqual((by["BTC-USD"]["outcome"], by["ETH-USD"]["outcome"]), ("target", "stop"))
        rep = skipped.report()
        self.assertEqual((rep["markets"][0]["all"]["n"], rep["markets"][0]["all"]["wins"], len(rep["recent"])), (2, 1, 2))


if __name__ == "__main__":
    unittest.main()
