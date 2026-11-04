"""Offline tests for the fan, race, calibration and verdict math. No model, no network.

Run: research/.venv/bin/python -m unittest discover -s research/tests -v
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kronos_lab import analysis, data  # noqa: E402


def path_from_closes(closes, wick=0.0):
    """One path whose bars open at the previous close and reach `wick` beyond each side."""
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    hi, lo = np.maximum(o, c) + wick, np.minimum(o, c) - wick
    return np.stack([o, hi, lo, c, np.zeros_like(c), np.zeros_like(c)], axis=1)


class RaceTest(unittest.TestCase):
    def setUp(self):
        # entry 100, long: stop 99, target 102
        self.paths = np.stack([
            path_from_closes([100.5, 101, 102.5, 103]),   # target first at bar 3
            path_from_closes([99.5, 98.5, 99, 103]),      # stop first at bar 2
            path_from_closes([100.2, 100.4, 100.1, 100.3]),  # neither
            path_from_closes([100, 100, 100, 100]),        # neither, flat
        ])

    def test_long_counts(self):
        r = analysis.race(self.paths, 100, 99, 102, "long")
        self.assertEqual((r["target_first"], r["stop_first"], r["neither"]), (1, 1, 2))
        self.assertEqual(r["r_at_target"], 2.0)
        self.assertEqual(r["median_bars_to_target"], 3)
        self.assertEqual(r["median_bars_to_stop"], 2)

    def test_same_bar_counts_as_stop(self):
        both = path_from_closes([100, 100.5], wick=2.5)[None]  # bar 2 spans 98 to 103
        r = analysis.race(both, 100, 99, 102, "long")
        self.assertEqual((r["target_first"], r["stop_first"]), (0, 1))

    def test_short_mirrors_long(self):
        mirrored = self.paths.copy()
        mirrored[..., :4] = 200 - mirrored[..., :4]
        mirrored[..., [1, 2]] = mirrored[..., [2, 1]]  # high and low swap under the mirror
        r = analysis.race(mirrored, 100, 101, 98, "short")
        self.assertEqual((r["target_first"], r["stop_first"], r["neither"]), (1, 1, 2))

    def test_rejects_levels_on_wrong_side(self):
        with self.assertRaises(ValueError):
            analysis.race(self.paths, 100, 101, 102, "long")
        with self.assertRaises(ValueError):
            analysis.race(self.paths, 100, 99, 98, "short")

    def test_levels_from_percent(self):
        self.assertEqual(analysis.levels(100, "long", 1, 2), (99, 102))
        self.assertEqual(analysis.levels(100, "short", 1, 2), (101, 98))


class FanTest(unittest.TestCase):
    def test_up_down_and_bands(self):
        paths = np.stack([path_from_closes([100, 100 + k]) for k in range(-5, 5)])
        f = analysis.fan(paths, 100)
        self.assertEqual((f["up"], f["down"], f["n"]), (4, 5, 10))
        self.assertEqual(len(f["bands"]["p50"]), 2)
        self.assertLess(f["bands"]["p10"][1], f["bands"]["p90"][1])


class CoverageTest(unittest.TestCase):
    def test_inside_and_partial_horizon(self):
        rng = np.random.default_rng(1)
        closes = 100 + rng.normal(0, 1, size=(500, 4))
        paths = np.stack([path_from_closes(c) for c in closes])
        cov = analysis.coverage(paths, np.array([100.0, 100.0, 150.0]))  # third step far outside
        self.assertEqual((cov["steps"], cov["inside"], cov["above"]), (3, 2, 1))
        self.assertFalse(cov["end_inside"])

    def test_labels(self):
        self.assertEqual(analysis.calibration_label(0.8)[1], "good")
        self.assertEqual(analysis.calibration_label(0.5)[1], "bad")
        self.assertEqual(analysis.calibration_label(None)[1], "hold")


class VerdictTest(unittest.TestCase):
    def test_break_even_depends_on_r(self):
        # at 2R, a 40 % target share beats the 33 % break-even
        v = analysis.verdict(0.2, 0.3, 2.0)
        self.assertAlmostEqual(v["break_even"], 1 / 3, places=4)
        self.assertEqual(v["lean_class"], "good")

    def test_combine_skips_on_disagreement(self):
        k = analysis.verdict(0.4, 0.2, 2.0)
        tm = analysis.verdict(0.1, 0.4, 2.0)
        self.assertEqual(analysis.combine(k, tm, 0.8)["action"], "SKIP")
        self.assertEqual(analysis.combine(k, k, 0.8)["action"], "PAPER TRADE")
        self.assertEqual(analysis.combine(k, None, 0.8)["action"], "PAPER ONLY")
        self.assertEqual(analysis.combine(k, k, 0.4)["action"], "SKIP")


class DataTest(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(data.yahoo_symbol("BTC/USDT"), "BTC-USD")
        self.assertTrue(data.is_crypto("ETH-USD"))
        self.assertFalse(data.is_crypto("TSLA"))
        self.assertEqual(data.bars_per_day("BTC-USD", "1h"), 24)
        self.assertEqual(data.bars_per_day("TSLA", "5m"), 78)

    def test_stock_future_stays_in_session(self):
        fut = data.future_timestamps(pd.Timestamp("2026-10-30 15:30"), "1h", 3, session=True)  # a Friday
        self.assertEqual([str(t) for t in fut],
                         ["2026-11-02 09:30:00", "2026-11-02 10:30:00", "2026-11-02 11:30:00"])

    def test_crypto_future_is_continuous(self):
        fut = data.future_timestamps(pd.Timestamp("2026-10-31 23:00"), "1h", 2, session=False)
        self.assertEqual([str(t) for t in fut], ["2026-11-01 00:00:00", "2026-11-01 01:00:00"])

    def test_parse_yahoo_crypto_stays_utc(self):
        doc = {"chart": {"result": [{"meta": {"instrumentType": "CRYPTOCURRENCY"}, "timestamp": [1762214400],
                                      "indicators": {"quote": [{"open": [1.0], "high": [2.0], "low": [0.5],
                                                                "close": [1.5], "volume": [10]}]}}]}}
        df = data.parse_yahoo(doc, "BTC-USD")
        self.assertEqual(str(df["timestamps"].iloc[0]), "2025-11-04 00:00:00")
        self.assertEqual(df.attrs["tz"], "UTC")


if __name__ == "__main__":
    unittest.main()
