"""Candles in, future timestamps out.

A symbol with a slash (BTC/USDT) is a crypto pair read from a public exchange
through ccxt, in UTC. Anything else (TSLA, SPY, BTC-USD) is read from Yahoo's
free chart endpoint, the same source the Day Trader skills use. Stock bars are
kept in New York time so Kronos sees session hours, not UTC hours.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd

EXCHANGES = ["binance", "bybit", "okx", "kraken"]
NY = "America/New_York"
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)

# Yahoo interval name and the longest range it serves for that interval.
YAHOO_INTERVALS = {
    "5m": ("5m", "60d"),
    "15m": ("15m", "60d"),
    "30m": ("30m", "60d"),
    "1h": ("60m", "730d"),
    "1d": ("1d", "10y"),
}
STEPS = {"5m": timedelta(minutes=5), "15m": timedelta(minutes=15), "30m": timedelta(minutes=30),
         "1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
PRICE_COLS = ["open", "high", "low", "close", "volume", "amount"]


class DataError(RuntimeError):
    pass


def is_pair(symbol: str) -> bool:
    return "/" in symbol


def yahoo_symbol(symbol: str) -> str:
    """BTC/USDT -> BTC-USD for Yahoo and the Time Machine; stock tickers pass through."""
    if not is_pair(symbol):
        return symbol.upper()
    base, quote = symbol.upper().split("/", 1)
    return f"{base}-{'USD' if quote in ('USDT', 'USDC', 'USD') else quote}"


def step_of(timeframe: str) -> timedelta:
    if timeframe not in STEPS:
        raise DataError(f"unsupported timeframe {timeframe!r}; use one of {', '.join(STEPS)}")
    return STEPS[timeframe]


def fetch(symbol: str, timeframe: str, bars: int, exchange: str | None = None,
          now: datetime | None = None) -> tuple[pd.DataFrame, str]:
    """The last `bars` CLOSED candles as a DataFrame with a `timestamps` column, and the source name."""
    now = now or datetime.now(timezone.utc)
    if is_pair(symbol):
        df, src = _fetch_ccxt(symbol, timeframe, bars + 1, exchange)
    else:
        df, src = _fetch_yahoo(symbol, timeframe), "yahoo"
    tz = df.attrs.get("tz", "UTC")
    df = closed_only(df, timeframe, now, tz)
    df.attrs["tz"] = tz
    if len(df) < bars:
        raise DataError(f"{src} returned {len(df)} closed {timeframe} bars for {symbol}, need {bars}")
    out = df.iloc[-bars:].reset_index(drop=True)
    out.attrs["tz"] = tz
    return out, src


def closed_only(df: pd.DataFrame, timeframe: str, now: datetime, tz: str) -> pd.DataFrame:
    """Drop the candle that is still forming: its end is later than now. Timestamps are naive in `tz`."""
    ends = df["timestamps"].dt.tz_localize(tz) + step_of(timeframe)
    return df[ends <= pd.Timestamp(now)].reset_index(drop=True)


def _fetch_ccxt(symbol: str, timeframe: str, limit: int, only: str | None) -> tuple[pd.DataFrame, str]:
    import ccxt

    errors = []
    for name in [only] if only else EXCHANGES:
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True})
            rows = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:  # geo-blocks and missing pairs are common: try the next exchange
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if len(rows) < limit - 1:
            errors.append(f"{name}: only {len(rows)} bars returned")
            continue
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df["timestamps"] = pd.to_datetime(df["ts"], unit="ms")
        df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
        out = df.drop(columns="ts")[["timestamps"] + PRICE_COLS].reset_index(drop=True)
        out.attrs["tz"] = "UTC"
        return out, name
    raise DataError("could not fetch candles:\n  " + "\n  ".join(errors))


def _fetch_yahoo(symbol: str, timeframe: str) -> pd.DataFrame:
    if timeframe not in YAHOO_INTERVALS:
        raise DataError(f"Yahoo serves {', '.join(YAHOO_INTERVALS)} bars, not {timeframe}")
    interval, rng = YAHOO_INTERVALS[timeframe]
    sym = yahoo_symbol(symbol)
    last = None
    for host in ("query2", "query1"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={interval}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (kronos-lab)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return parse_yahoo(json.load(r), sym)
        except DataError:
            raise
        except Exception as e:
            last = e
    raise DataError(f"Yahoo did not answer for {sym}: {last}")


def parse_yahoo(doc: dict, sym: str) -> pd.DataFrame:
    """A Yahoo v8 chart JSON document -> candles. Crypto stays in UTC, stocks go to New York time."""
    try:
        res = doc["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
    except (KeyError, IndexError, TypeError):
        raise DataError(f"Yahoo returned no chart for {sym}: {str(doc)[:200]}")
    df = pd.DataFrame({"ts": ts, **{k: q.get(k) for k in ("open", "high", "low", "close", "volume")}})
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    crypto = res.get("meta", {}).get("instrumentType") == "CRYPTOCURRENCY"
    tz = "UTC" if crypto else NY
    df["timestamps"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(tz).dt.tz_localize(None)
    df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
    df = df.drop(columns="ts")[["timestamps"] + PRICE_COLS].drop_duplicates("timestamps").reset_index(drop=True)
    df.attrs["tz"] = tz
    return df


def future_timestamps(last: pd.Timestamp, timeframe: str, n: int, session: bool) -> pd.Series:
    """The next n bar start times. With session=True (stocks) bars stay inside 9:30 to 16:00 on
    weekdays and jump to the next morning; market holidays are not skipped."""
    step = step_of(timeframe)
    out, t = [], pd.Timestamp(last)
    while len(out) < n:
        t = t + step
        if session:
            t = _into_session(t, timeframe)
        out.append(t)
    return pd.Series(out)


def _into_session(t: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    if timeframe == "1d":
        while t.weekday() >= 5:
            t += timedelta(days=1)
        return t
    close = t.replace(hour=SESSION_CLOSE[0], minute=SESSION_CLOSE[1], second=0)
    if t.weekday() < 5 and t.time() >= t.replace(hour=SESSION_OPEN[0], minute=SESSION_OPEN[1]).time() and t < close:
        return t
    nxt = t if t.time() < t.replace(hour=SESSION_OPEN[0], minute=SESSION_OPEN[1]).time() else t + timedelta(days=1)
    nxt = nxt.replace(hour=SESSION_OPEN[0], minute=SESSION_OPEN[1], second=0)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt
