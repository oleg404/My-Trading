"""Offline tests for the stop streak (tradingagents/dashboard/stop_streak.py): three stop-outs in a row in a book
put every position it still holds to Claude, once per streak, without re-running the agents; and for what Claude
is then told and how a half it closes is recorded (position_review). Claude is mocked. No model, no network.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_stop_streak.py -v
"""

import time
import unittest
from unittest import mock

import test_workflows as tw  # noqa: E402  sets the desk's home first; imported whole so its tests do not run twice here

import checks  # noqa: E402
import claude_review  # noqa: E402
import lessons  # noqa: E402
import market_hours  # noqa: E402
import portfolios  # noqa: E402
import position_review  # noqa: E402
import stop_streak  # noqa: E402
import store  # noqa: E402

STOP = "Auto COVER: stop 1.00 reached"
EXTRA = {"stop_streak": {"stops_in_a_row": 3, "stopped": [{"ticker": "SOL-USD", "side": "short", "pct": -6.0}],
                         "same_side_as_stopped": True, "hours_since_last_stop": 0.1}}


class StopStreakTest(unittest.TestCase):
    setUp, tearDown = tw.WorkflowTest.setUp, tw.WorkflowTest.tearDown
    book, hold, closed = tw.WorkflowTest.book, tw.WorkflowTest.hold, tw.WorkflowTest.closed

    def stop_out(self, ticker, quantity):
        self.hold(ticker, "sell", quantity)
        self.closed(ticker, "buy", quantity, STOP)

    def three_stops(self):
        """ETH is held short throughout; SOL, BTC and SOL again are opened and stopped out."""
        self.hold("ETH-USD", "sell", 0.1)
        self.stop_out("SOL-USD", 1.0)
        self.stop_out("BTC-USD", 0.004)
        self.stop_out("SOL-USD", 1.0)

    def protect(self, **kw):
        with mock.patch.object(checks, "prefs", return_value={"autoHalf": kw.pop("half", True)}), \
                mock.patch.object(claude_review, "ready", return_value={"ready": kw.pop("ready", True), "reason": ""}), \
                mock.patch.object(position_review, "ask_now", return_value=kw.pop("free", True)) as ask:
            done = stop_streak.protect(mock.Mock(), **kw)
        # books of earlier tests share the desk's home: keep to this test's own
        mine = [c for c in ask.call_args_list if c.args[0] == self.pid]
        return [d for d in done if d["portfolio"] == self.book()["name"]], mine

    # ---- what a streak is ----

    def test_three_stops_in_a_row_make_a_streak(self):
        self.hold("ETH-USD", "sell", 0.1)
        self.stop_out("SOL-USD", 1.0)
        self.stop_out("BTC-USD", 0.004)
        self.assertIsNone(stop_streak.streak(self.book()))  # two
        self.stop_out("SOL-USD", 1.0)
        s = stop_streak.streak(self.book())
        self.assertEqual((s["n"], [x["ticker"] for x in s["stopped"]], s["stopped"][0]["side"]), (3, ["SOL-USD", "BTC-USD", "SOL-USD"], "short"))

    def test_any_other_close_breaks_the_row_but_the_desks_own_guard_halves_do_not(self):
        self.hold("ETH-USD", "sell", 0.12)
        self.stop_out("SOL-USD", 1.0)
        self.stop_out("BTC-USD", 0.004)
        self.closed("ETH-USD", "buy", 0.03, "Snapback guard: closed half. BTC-USD is 7% above its 72-hour low")
        self.closed("ETH-USD", "buy", 0.03, "Stop streak: Claude closed half. losing")
        self.stop_out("SOL-USD", 1.0)
        self.assertEqual(stop_streak.streak(self.book())["n"], 3)  # the two guard halves sit inside the row
        self.assertEqual(lessons.round_trips(self.book()["trades"])[2]["exitKind"], "guard")
        self.closed("ETH-USD", "buy", 0.03, "Review: Claude closed half. the agents turned neutral")
        self.assertIsNone(stop_streak.streak(self.book()))          # a review's half breaks it, like a target or a hand close

    def test_a_streak_goes_stale_after_a_day_and_slow_stops_are_not_one(self):
        self.three_stops()
        now = time.time()
        self.assertIsNotNone(stop_streak.streak(self.book(), now + 23 * 3600))
        self.assertIsNone(stop_streak.streak(self.book(), now + 25 * 3600))  # the latest stop is over a day old
        book = self.book()
        stops = [t for t in book["trades"] if t["note"] == STOP]  # newest first
        stops[2]["time"] -= 30 * 3600                              # the first of the three came 30 hours before the last
        for t in book["trades"]:
            if t["time"] < stops[1]["time"] and t is not stops[2]:
                t["time"] -= 31 * 3600                             # and everything before it stays before it
        self.assertIsNone(stop_streak.streak(book, now))

    # ---- what the desk does about it ----

    def test_every_position_left_is_put_to_claude_once_per_streak(self):
        self.three_stops()
        self.hold("BTC-USD", "sell", 0.004)  # opened after the streak formed: not one of "the rest"
        done, ask = self.protect()
        self.assertEqual([(d["ticker"], d["stops"]) for d in done], [("ETH-USD", 3)])
        pid, ticker, extra = ask[0].args[:3]
        self.assertEqual((pid, ticker, extra["stop_streak"]["stops_in_a_row"], extra["stop_streak"]["same_side_as_stopped"]), (self.pid, "ETH-USD", 3, True))
        self.assertEqual(self.book()["positions"][0]["streak_ask"], stop_streak.streak(self.book())["first"])
        done, ask = self.protect()
        self.assertEqual((done, ask), ([], []))  # asked already
        self.closed("BTC-USD", "buy", 0.004, STOP)  # a fourth stop is the same streak: nobody is asked twice
        self.assertEqual(self.protect()[0], [])

    def test_nothing_is_asked_without_a_streak_with_the_switch_off_or_without_claude(self):
        self.hold("ETH-USD", "sell", 0.1)
        self.assertEqual(self.protect()[0], [])               # no streak
        self.stop_out("SOL-USD", 1.0); self.stop_out("BTC-USD", 0.004); self.stop_out("SOL-USD", 1.0)
        self.assertEqual(self.protect(half=False)[0], [])     # the half-close switch is off
        self.assertEqual(self.protect(ready=False)[0], [])    # Claude is not available: try again next minute
        self.assertEqual(self.protect(free=False)[0], [])     # its scheduled review is looking at it right now
        self.assertNotIn("streak_ask", self.book()["positions"][0])
        self.assertEqual(len(self.protect()[0]), 1)

    def test_a_position_waits_for_its_market_to_open(self):
        self.three_stops()
        with mock.patch.object(market_hours, "is_open", return_value=False):
            done, ask = self.protect()
        self.assertEqual((done, ask), ([], []))
        self.assertEqual(len(self.protect()[0]), 1)  # open again while the streak is fresh: asked

    # ---- what Claude is told and what a half looks like ----

    def test_claude_gets_the_streak_and_a_half_is_recorded_as_the_streaks(self):
        self.hold("ETH-USD", "sell", 0.1)
        answer = {"action": "CLOSE HALF", "confidence": "medium", "reason": "Losing 2% with three shorts just stopped out."}
        with mock.patch.object(claude_review, "ready", return_value={"ready": True, "reason": ""}), \
                mock.patch.object(lessons, "brief", return_value={"market_regime": "strong short", "desk_record": []}), \
                mock.patch.object(store, "read_report", return_value={"rating": "Underweight"}), \
                mock.patch.object(claude_review, "ask", return_value=(answer, [])) as ask:
            done = position_review.judge(self.pid, "ETH-USD", {"report": None, "decision": "Underweight"}, mock.Mock(), EXTRA)
        prompt = ask.call_args.args[0]
        self.assertIn('"stops_in_a_row": 3', prompt)
        self.assertIn("is still open after 3 positions in this book hit their stops in a row", prompt)
        self.assertIn("`stop_streak` means this is not a scheduled review", ask.call_args.args[1])
        self.assertEqual(done["closed"], 0.05)
        p = self.book()["positions"][0]
        self.assertEqual((p["quantity"], p["review_claude"]["asked"], bool(p["trimmed"])), (-0.05, "stop streak", True))
        self.assertTrue(self.book()["trades"][0]["note"].startswith("Stop streak: Claude closed half. Losing 2%"))

    def test_asking_now_uses_the_report_the_position_has_and_runs_no_agents(self):
        self.hold("ETH-USD", "sell", 0.1)
        book = self.book()
        book["positions"][0].update(source="ETH-USD_20261122_000000", review_verdict="Underweight")
        portfolios._save(book)
        with mock.patch.object(position_review, "judge") as judge:
            self.assertTrue(position_review.ask_now(self.pid, "ETH-USD", EXTRA))
            for _ in range(100):
                if judge.called and not position_review._busy:
                    break
                time.sleep(0.01)
        self.assertEqual(judge.call_args.args[:3], (self.pid, "ETH-USD", {"report": "ETH-USD_20261122_000000", "decision": "Underweight"}))
        self.assertEqual(judge.call_args.args[4], EXTRA)


if __name__ == "__main__":
    unittest.main()
