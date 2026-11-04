"""Pure numpy on sampled paths: the fan, the stop/target race, calibration, the verdict.

paths has shape (n_paths, horizon, 6) with columns open, high, low, close,
volume, amount. Sampled candles are not always internally consistent, so a
bar's reach is taken as the max and min of its four prices.
"""

from __future__ import annotations

import numpy as np

QUANTILES = (10, 25, 50, 75, 90)
COIN_FLIP = 0.05  # same thresholds as trader-timemachine's verdict
STRONG = 0.15


def bar_reach(paths: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ohlc = paths[..., :4]
    return ohlc.max(axis=-1), ohlc.min(axis=-1)


def fan(paths: np.ndarray, last_close: float) -> dict:
    """Where the paths end relative to the last close, and percentile bands per step."""
    close = paths[..., 3]
    end = close[:, -1]
    bands = {f"p{q}": np.percentile(close, q, axis=0).round(6).tolist() for q in QUANTILES}
    end_pct = (end / last_close - 1) * 100
    return {
        "n": int(len(end)),
        "up": int((end > last_close).sum()),
        "down": int((end < last_close).sum()),
        "end_pct": {f"p{q}": round(float(np.percentile(end_pct, q)), 3) for q in QUANTILES},
        "bands": bands,
    }


def levels(entry: float, side: str, stop_pct: float, target_pct: float) -> tuple[float, float]:
    """Stop and target from percentages of the entry, on the right sides for a long or a short."""
    if stop_pct <= 0 or target_pct <= 0:
        raise ValueError("stop and target percentages must be positive")
    if side == "long":
        return entry * (1 - stop_pct / 100), entry * (1 + target_pct / 100)
    if side == "short":
        return entry * (1 + stop_pct / 100), entry * (1 - target_pct / 100)
    raise ValueError(f"side must be long or short, got {side!r}")


def race(paths: np.ndarray, entry: float, stop: float, target: float, side: str) -> dict:
    """Walk every path bar by bar: which of stop and target is touched first.
    A bar that touches both counts as the stop, the same conservative rule as the Time Machine."""
    long = side == "long"
    if long and not stop < entry < target:
        raise ValueError(f"a long needs stop < entry < target, got {stop}, {entry}, {target}")
    if not long and not target < entry < stop:
        raise ValueError(f"a short needs target < entry < stop, got {target}, {entry}, {stop}")
    hi, lo = bar_reach(paths)
    hit_t = hi >= target if long else lo <= target
    hit_s = lo <= stop if long else hi >= stop
    horizon = paths.shape[1]
    first_t = np.where(hit_t.any(axis=1), hit_t.argmax(axis=1), horizon)
    first_s = np.where(hit_s.any(axis=1), hit_s.argmax(axis=1), horizon)
    target_first = first_t < first_s
    stop_first = (first_s <= first_t) & (first_s < horizon)
    neither = ~target_first & ~stop_first
    n = len(paths)
    risk = abs(entry - stop)
    r_target = abs(target - entry) / risk
    end = paths[:, -1, 3]
    open_r = ((end - entry) if long else (entry - end)) / risk
    r = np.where(target_first, r_target, np.where(stop_first, -1.0, open_r))
    out = {
        "side": side, "entry": entry, "stop": stop, "target": target, "r_at_target": round(r_target, 3),
        "target_first": int(target_first.sum()), "stop_first": int(stop_first.sum()), "neither": int(neither.sum()),
        "n": n, "avg_r": round(float(r.mean()), 3),
        "median_bars_to_target": _median_bars(first_t[target_first]),
        "median_bars_to_stop": _median_bars(first_s[stop_first]),
    }
    out.update(verdict(out["target_first"] / n, out["stop_first"] / n, r_target))
    return out


def _median_bars(ix: np.ndarray):
    return None if ix.size == 0 else int(np.median(ix)) + 1


def lean(p_target: float, p_stop: float, r_target: float):
    """The target's share of the paths that hit a side, minus the break-even share at this R."""
    if p_target + p_stop <= 0:
        return None
    share, even = p_target / (p_target + p_stop), 1.0 / (1.0 + r_target)
    return share - even, share, even


def verdict(p_target: float, p_stop: float, r_target: float) -> dict:
    lv = lean(p_target, p_stop, r_target)
    if lv is None:
        return {"lean": None, "lean_label": "No path reached the stop or the target", "lean_class": "hold"}
    d, share, even = lv
    if abs(d) < COIN_FLIP:
        label, cls = "Close to break-even", "warn"
    elif abs(d) < STRONG:
        label, cls = ("Leans toward the target", "good") if d > 0 else ("Leans toward the stop", "bad")
    else:
        label, cls = ("Strongly toward the target", "good") if d > 0 else ("Strongly toward the stop", "bad")
    return {"lean": round(d, 4), "share_target": round(share, 4), "break_even": round(even, 4),
            "lean_label": label, "lean_class": cls}


def coverage(paths: np.ndarray, realized_close: np.ndarray, lo: int = 10, hi: int = 90) -> dict:
    """How often the real close landed inside the paths' p10 to p90 band, step by step.
    realized_close may be shorter than the horizon when the future has not happened yet."""
    k = min(len(realized_close), paths.shape[1])
    if k == 0:
        return {"steps": 0, "inside": 0, "share": None, "end_inside": None}
    close = paths[:, :k, 3]
    p_lo, p_hi = np.percentile(close, lo, axis=0), np.percentile(close, hi, axis=0)
    real = np.asarray(realized_close[:k], dtype=float)
    inside = (real >= p_lo) & (real <= p_hi)
    return {"steps": int(k), "inside": int(inside.sum()), "share": round(float(inside.mean()), 4),
            "end_inside": bool(inside[-1]), "below": int((real < p_lo).sum()), "above": int((real > p_hi).sum())}


def calibration_label(share) -> tuple[str, str]:
    """A p10 to p90 band should hold the real price about 80 % of the time."""
    if share is None:
        return "Not checked", "hold"
    if share < 0.6:
        return "Overconfident: the real price left the band too often", "bad"
    if share < 0.7:
        return "A bit narrow: treat the odds with care", "warn"
    if share <= 0.9:
        return "Well calibrated", "good"
    return "Band too wide: odds are vaguer than they look", "warn"


def combine(kronos: dict, timemachine: dict | None, calib_share) -> dict:
    """Step 8 of the use case: if the Time Machine disagrees with Kronos, skip the trade."""
    reasons = []
    k_lean = kronos.get("lean")
    if k_lean is None:
        return {"action": "SKIP", "cls": "warn", "reasons": ["No Kronos path reached the stop or the target."]}
    if abs(k_lean) < COIN_FLIP:
        reasons.append("Kronos is close to break-even on this stop and target.")
    elif k_lean < 0:
        reasons.append("Kronos leans toward the stop.")
    _, cal_cls = calibration_label(calib_share)
    if cal_cls == "bad":
        reasons.append("Kronos failed its calibration check on this ticker.")
    elif calib_share is None:
        reasons.append("Kronos was not checked against recent days.")
    if timemachine is None:
        reasons.append("No Time Machine second opinion for this plan.")
        if k_lean >= COIN_FLIP and cal_cls != "bad":
            return {"action": "PAPER ONLY", "cls": "warn", "reasons": reasons}
    else:
        tm_lean = timemachine.get("lean")
        if tm_lean is None or (tm_lean >= COIN_FLIP) != (k_lean >= COIN_FLIP):
            reasons.append("The Time Machine disagrees with Kronos.")
    if reasons:
        return {"action": "SKIP", "cls": "bad", "reasons": reasons}
    return {"action": "PAPER TRADE", "cls": "good",
            "reasons": ["Kronos and the Time Machine both lean toward the target, and the band passed calibration."]}
