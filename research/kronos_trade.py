"""Kronos futures fan + stop/target race + calibration + Time Machine second opinion.

One `scan` does steps 5 to 8 of the use case and writes a JSON result the
TradingAgents desk draws in its Kronos tab:

    .venv/bin/python kronos_trade.py scan --symbol BTC-USD --out result.json

It never places an order. Progress lines go to stderr as `PROGRESS <0-100> <text>`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kronos_lab import analysis, data, sampler  # noqa: E402

SAMPLE_PATHS_KEPT = 60  # paths drawn on the chart; every path is kept in the .npz
HISTORY_BARS = 96


def progress(pct: float, text: str) -> None:
    print(f"PROGRESS {int(pct)} {text}", file=sys.stderr, flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="sample the fan, race it, calibrate it, ask the Time Machine")
    p.add_argument("--symbol", required=True, help="BTC-USD, TSLA, or a ccxt pair like BTC/USDT")
    p.add_argument("--timeframe", default="1h", choices=list(data.STEPS))
    p.add_argument("--lookback", type=int, default=128, help="candles fed to Kronos")
    p.add_argument("--horizon", type=int, default=48, help="candles ahead")
    p.add_argument("--paths", type=int, default=1000)
    p.add_argument("--model", default="small", choices=list(sampler.MODELS))
    p.add_argument("--side", default="auto", choices=["auto", "long", "short"],
                   help="auto: short when most paths end below the last close")
    p.add_argument("--stop-pct", type=float, default=1.0)
    p.add_argument("--target-pct", type=float, default=2.0)
    p.add_argument("--calibrate-days", type=int, default=5)
    p.add_argument("--calibrate-paths", type=int, default=100)
    p.add_argument("--no-timemachine", action="store_true")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--chunk", type=int, default=50)
    p.add_argument("--out", required=True, help="result JSON path; the paths go next to it as .npz")
    a = ap.parse_args(argv)
    if not 16 <= a.lookback <= 512 or not 1 <= a.horizon <= 256 or not 10 <= a.paths <= 5000:
        ap.error("need 16 <= lookback <= 512, 1 <= horizon <= 256, 10 <= paths <= 5000")
    if not 0 <= a.calibrate_days <= 30 or a.stop_pct <= 0 or a.target_pct <= 0:
        ap.error("calibrate-days 0 to 30, stop and target percentages above 0")
    return a


def iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def scan(a: argparse.Namespace) -> dict:
    t0 = time.time()
    crypto = data.is_crypto(a.symbol)
    bpd = data.bars_per_day(a.symbol, a.timeframe)
    progress(2, f"Fetching {a.timeframe} candles for {a.symbol}")
    need = a.lookback + a.calibrate_days * bpd
    df, source = data.fetch(a.symbol, a.timeframe, need)
    tz = df.attrs.get("tz", "UTC")
    hist = df.iloc[-a.lookback:].reset_index(drop=True)
    last_ts, last_close = hist["timestamps"].iloc[-1], float(hist["close"].iloc[-1])
    future = data.future_timestamps(last_ts, a.timeframe, a.horizon, session=not crypto)

    progress(5, f"Loading Kronos-{a.model}")
    predictor = sampler.load(a.model)
    calib_share = a.calibrate_days * a.calibrate_paths / max(a.paths + a.calibrate_days * a.calibrate_paths, 1)
    main_end = 8 + 80 * (1 - calib_share)

    progress(8, f"Sampling {a.paths} futures, {a.horizon} candles ahead")
    paths = _sample(predictor, hist, future, a.paths, a, 8, main_end, "futures")
    fan = analysis.fan(paths, last_close)
    fan["context"] = analysis.context(hist["close"], last_close)

    side = a.side if a.side != "auto" else ("short" if fan["down"] > fan["up"] else "long")
    fan["reversion"] = analysis.reversion(fan["context"], fan, side)
    stop, target = analysis.levels(last_close, side, a.stop_pct, a.target_pct)
    race = analysis.race(paths, last_close, stop, target, side)

    calib = calibrate(predictor, df, a, bpd, crypto, main_end)
    tm = None if a.no_timemachine else timemachine(a.symbol, side, last_close, stop, target, crypto)
    tm_view = tm if tm and "lean" in tm else None
    decision = analysis.combine(race, tm_view, calib["share"], fan["reversion"])

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    npz = out.with_suffix(".npz")
    np.savez_compressed(npz, paths=paths.astype(np.float32), columns=np.array(sampler.COLS),
                        future=np.array([iso(t) for t in future]))
    rng = np.random.default_rng(a.seed)
    keep = rng.choice(len(paths), size=min(SAMPLE_PATHS_KEPT, len(paths)), replace=False)
    tail = df.iloc[-HISTORY_BARS:]
    progress(100, "Done")
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": a.symbol, "timeframe": a.timeframe, "timezone": tz, "source": source,
        "model": f"Kronos-{a.model}", "device": str(predictor.device), "seed": a.seed,
        "lookback": a.lookback, "horizon": a.horizon, "crypto": crypto,
        "last": {"time": iso(last_ts), "close": last_close},
        "history": {"t": [iso(t) for t in tail["timestamps"]], "c": tail["close"].round(6).tolist()},
        "future": [iso(t) for t in future],
        "fan": fan,
        "sample_paths": paths[keep, :, 3].round(4).tolist(),
        "plan": {"side": side, "auto": a.side == "auto", "entry": last_close, "stop": round(stop, 6),
                 "target": round(target, 6), "stop_pct": a.stop_pct, "target_pct": a.target_pct},
        "race": race, "calibration": calib, "timemachine": tm, "decision": decision,
        "paths_file": str(npz), "elapsed": round(time.time() - t0, 1),
    }


def _sample(predictor, hist, future, n, a, p_from, p_to, label):
    done = {"n": 0}

    def chunked():
        out = []
        while done["n"] < n:
            b = min(a.chunk, n - done["n"])
            out.append(sampler.sample_paths(predictor, hist, future, n_paths=b, chunk=b,
                                            seed=a.seed + done["n"], progress=False))
            done["n"] += b
            progress(p_from + (p_to - p_from) * done["n"] / n, f"Sampled {done['n']}/{n} {label}")
        return np.concatenate(out, axis=0)

    return chunked()


def calibrate(predictor, df, a, bpd, crypto, p_from) -> dict:
    """Step 6: re-run Kronos as of each of the last N days and score the p10 to p90 band."""
    runs = []
    for d in range(1, a.calibrate_days + 1):
        cut = len(df) - d * bpd
        if cut < a.lookback:
            break
        ctx = df.iloc[cut - a.lookback:cut].reset_index(drop=True)
        real = df["close"].iloc[cut:cut + a.horizon].to_numpy()
        fut = data.future_timestamps(ctx["timestamps"].iloc[-1], a.timeframe, a.horizon, session=not crypto)
        span = (96 - p_from) / max(a.calibrate_days, 1)
        lo = p_from + span * (d - 1)
        progress(lo, f"Calibration day {d}/{a.calibrate_days}")
        paths = _sample(predictor, ctx, fut, a.calibrate_paths, a, lo, lo + span, f"paths (day -{d})")
        cov = analysis.coverage(paths, real)
        runs.append({"day": -d, "as_of": iso(ctx["timestamps"].iloc[-1]), **cov})
    steps = sum(r["steps"] for r in runs)
    share = round(sum(r["inside"] for r in runs) / steps, 4) if steps else None
    label, cls = analysis.calibration_label(share)
    return {"runs": runs, "steps": steps, "share": share, "label": label, "cls": cls, "target": 0.8}


def day_trader_dir() -> Path | None:
    env = os.environ.get("DAY_TRADER_DIR")
    if env:
        return Path(env)
    hits = sorted(glob.glob(str(Path.home() / ".claude/plugins/cache/day-trader-skill/day-trader-skill/*/")))
    return Path(hits[-1]) if hits else None


def timemachine(symbol, side, entry, stop, target, crypto) -> dict:
    """Step 8: the Day Trader Time Machine replays the rest of today's New York session 10,000 times
    from the ticker's own 5-minute bars. It only runs during 9:35 to 15:55 New York time."""
    root = day_trader_dir()
    script = root / "trader-timemachine" / "timemachine.py" if root else None
    if not script or not script.is_file():
        return {"skipped": "Day Trader plugin not found (install day-trader-skill)"}
    now = datetime.now(ZoneInfo("America/New_York"))
    minutes = now.hour * 60 + now.minute
    if (not crypto and now.weekday() >= 5) or not 9 * 60 + 35 <= minutes <= 15 * 60 + 55:
        return {"skipped": "The Time Machine replays the 9:35 AM to 3:55 PM New York session; run it then"}
    bar_close = now.replace(minute=now.minute - now.minute % 5)
    cmd = [sys.executable, str(script), "run", "--symbol", data.yahoo_symbol(symbol),
           "--side", "buy" if side == "long" else "sell", "--date", now.date().isoformat(),
           "--entry", f"{entry:.4f}", "--stop", f"{stop:.4f}", "--target", f"{target:.4f}",
           "--entry-time", bar_close.strftime("%H:%M"), "--setup", "kronos", "--json"]
    progress(97, "Asking the Time Machine")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        return {"skipped": "The Time Machine took longer than 3 minutes"}
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return {"skipped": "The Time Machine could not run this plan", "detail": (r.stderr or r.stdout).strip()[-300:]}
    mc = doc.get("mc") or {}
    if not mc:
        return {"skipped": "The Time Machine returned no replays", "detail": r.stderr.strip()[-300:]}
    v = analysis.verdict(mc.get("p_target", 0), mc.get("p_stop", 0), mc.get("r_at_target") or 1.0)
    return {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"),
            "replays": mc.get("replays"), "until": mc.get("until"), "entry_time": doc.get("plan", {}).get("entry_time"),
            "verdict": doc.get("verdict"), "pool_sessions": mc.get("pool_sessions"),
            "warnings": doc.get("warnings") or [], **v}


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        result = scan(a)
    except (data.DataError, ValueError, RuntimeError) as e:
        print(f"ERROR {e}", file=sys.stderr, flush=True)
        return 2
    tmp = Path(a.out).with_suffix(".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(a.out)
    r = result["race"]
    rv = result["fan"]["reversion"]
    print(f"{a.symbol}: {result['fan']['down']} of {result['fan']['n']} futures end down -> {r['side']}"
          f"{' (reversion: window pull)' if rv['flagged'] else ''} | "
          f"target first {r['target_first']}, stop first {r['stop_first']}, neither {r['neither']} | "
          f"calibration {result['calibration']['share']} | {result['decision']['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
