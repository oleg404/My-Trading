"""Forecast upcoming candles for a crypto pair with the Kronos foundation model.

Pulls recent OHLCV candles from a public exchange endpoint via ccxt (no API
keys needed), samples several independent forecast paths with Kronos, and
prints a summary of where the paths end up relative to the last close.

Usage:
    .venv/bin/python kronos_forecast.py --symbol BTC/USDT --timeframe 1h
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ccxt
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "kronos"))

from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: E402

MODELS = {
    "mini": ("NeoQuasar/Kronos-mini", "NeoQuasar/Kronos-Tokenizer-2k", 2048),
    "small": ("NeoQuasar/Kronos-small", "NeoQuasar/Kronos-Tokenizer-base", 512),
    "base": ("NeoQuasar/Kronos-base", "NeoQuasar/Kronos-Tokenizer-base", 512),
}
EXCHANGES = ["binance", "bybit", "okx", "kraken"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--timeframe", default="1h", help="ccxt timeframe, e.g. 15m, 1h, 4h, 1d")
    p.add_argument("--lookback", type=int, default=400, help="history bars fed to the model")
    p.add_argument("--pred-len", type=int, default=24, help="future bars to forecast")
    p.add_argument("--samples", type=int, default=10, help="independent forecast paths")
    p.add_argument("--model", choices=MODELS, default="small")
    p.add_argument("--exchange", choices=EXCHANGES, help="force one exchange instead of trying in order")
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--top-p", type=float, default=0.9)
    p.add_argument("--plot", action="store_true", help="save a PNG chart to research/output/")
    args = p.parse_args()
    if args.lookback < 50 or args.pred_len < 1 or args.samples < 1:
        p.error("need lookback >= 50, pred-len >= 1, samples >= 1")
    return args


def fetch_candles(symbol: str, timeframe: str, limit: int, only: str | None) -> tuple[pd.DataFrame, str]:
    errors = []
    for name in [only] if only else EXCHANGES:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:  # geo-blocks and missing pairs are common; try the next one
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if len(rows) < limit:
            errors.append(f"{name}: only {len(rows)} bars returned")
            continue
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["timestamps"] = pd.to_datetime(df["ts"], unit="ms")
        df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
        return df.drop(columns="ts"), name
    sys.exit("Could not fetch candles:\n  " + "\n  ".join(errors))


def main() -> None:
    args = parse_args()
    model_id, tok_id, max_ctx = MODELS[args.model]
    lookback = min(args.lookback, max_ctx)

    # Drop the still-forming last candle so the model only sees closed bars.
    df, exchange = fetch_candles(args.symbol, args.timeframe, lookback + 1, args.exchange)
    df = df.iloc[:-1].reset_index(drop=True)

    step = df["timestamps"].diff().mode().iloc[0]
    x_ts = df["timestamps"]
    y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + step, periods=args.pred_len, freq=step))
    x_df = df[["open", "high", "low", "close", "volume", "amount"]]

    # Kronos never switches to eval mode itself, which leaves dropout active during
    # inference (noisier forecasts, and a hard crash on Apple's MPS backend).
    tokenizer = KronosTokenizer.from_pretrained(tok_id).eval()
    model = Kronos.from_pretrained(model_id).eval()
    predictor = KronosPredictor(model, tokenizer, max_context=max_ctx)
    print(f"Kronos-{args.model} on {predictor.device} | {exchange} {args.symbol} {args.timeframe} | "
          f"{lookback} bars in, {args.pred_len} out, {args.samples} paths")

    # One batch of identical inputs gives independent sampled paths in a single pass.
    paths = predictor.predict_batch(
        df_list=[x_df] * args.samples,
        x_timestamp_list=[x_ts] * args.samples,
        y_timestamp_list=[y_ts] * args.samples,
        pred_len=args.pred_len,
        T=args.temperature,
        top_p=args.top_p,
        sample_count=1,
        verbose=False,
    )

    last = float(df["close"].iloc[-1])
    end_closes = pd.Series([float(p["close"].iloc[-1]) for p in paths])
    pct = (end_closes / last - 1) * 100
    mean_path = sum(p[["open", "high", "low", "close"]] for p in paths) / len(paths)

    print(f"\nLast closed bar {x_ts.iloc[-1]} close {last:,.2f}")
    print(f"Horizon end     {y_ts.iloc[-1]}")
    print(f"Paths up        {int((pct > 0).sum())}/{len(pct)}")
    print(f"Median change   {pct.median():+.2f}%   (range {pct.min():+.2f}% to {pct.max():+.2f}%)")
    print(f"Mean-path high  {mean_path['high'].max():,.2f}   low {mean_path['low'].min():,.2f}")

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    stem = f"{args.symbol.replace('/', '')}_{args.timeframe}_{x_ts.iloc[-1]:%Y%m%d_%H%M}"
    mean_path.to_csv(out_dir / f"{stem}_mean.csv")
    print(f"\nMean path saved to {out_dir / (stem + '_mean.csv')}")

    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        hist = df.set_index("timestamps")["close"].iloc[-min(200, len(df)):]
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(hist.index, hist.values, color="black", lw=1.2, label="history")
        for p in paths:
            ax.plot(p.index, p["close"], color="tab:blue", alpha=0.25, lw=0.8)
        ax.plot(mean_path.index, mean_path["close"], color="tab:red", lw=1.8, label="mean forecast")
        ax.set_title(f"Kronos-{args.model} {args.symbol} {args.timeframe}")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        png = out_dir / f"{stem}.png"
        fig.savefig(png, dpi=120)
        print(f"Chart saved to {png}")


if __name__ == "__main__":
    main()
