"""Offline tests for Claude's view on the Study page (tradingagents/dashboard/claude_review.py): a saved
review is marked out of date once its portfolio has traded the ticker after it was written. No model.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_claude_review.py -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="desk-claude-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import claude_review  # noqa: E402

T0 = 1_793_620_000.0  # the review's moment


def book(trades=(), positions=()):
    return {"id": "main", "name": "Main", "positions": list(positions), "trades": list(trades)}


def review(created=T0, status="done"):
    return {"id": "a" * 32, "ticker": "NVDA", "portfolio": "main", "status": status, "created": created}


class SinceTest(unittest.TestCase):
    def test_untouched_book_keeps_the_review_current(self):
        held = book(trades=[{"time": T0 - 100, "ticker": "NVDA", "side": "buy", "quantity": 10, "price": 204.64}],
                    positions=[{"ticker": "NVDA", "quantity": 10}])
        self.assertIsNone(claude_review.since(review(), held))

    def test_stop_out_after_the_review_marks_it_out_of_date(self):
        # The NVDA case on the desk: reviewed on Nov 4 while Main held 10 shares, stopped out on Nov 5.
        sold = book(trades=[{"time": T0 - 100, "ticker": "NVDA", "side": "buy", "quantity": 10, "price": 204.64},
                            {"time": T0 + 3600, "ticker": "NVDA", "side": "sell", "quantity": 10, "price": 192.2,
                             "note": "Auto SELL: stop 192.20 reached"}])
        s = claude_review.since(review(), sold)
        self.assertEqual((s["holds"], s["quantity"], s["portfolio_gone"]), (False, 0, False))
        self.assertEqual([t["side"] for t in s["trades"]], ["sell"])
        self.assertEqual(s["trades"][0]["note"], "Auto SELL: stop 192.20 reached")

    def test_other_tickers_do_not_count(self):
        other = book(trades=[{"time": T0 + 10, "ticker": "AMD", "side": "buy", "quantity": 5, "price": 100}])
        self.assertIsNone(claude_review.since(review(), other))

    def test_only_finished_reviews_are_checked(self):
        traded = book(trades=[{"time": T0 + 10, "ticker": "NVDA", "side": "sell", "quantity": 10, "price": 1}])
        self.assertIsNone(claude_review.since(review(status="failed"), traded))

    def test_deleted_portfolio_is_reported(self):
        s = claude_review.since({**review(), "portfolio": "nope"})
        self.assertTrue(s["portfolio_gone"])


if __name__ == "__main__":
    unittest.main()
