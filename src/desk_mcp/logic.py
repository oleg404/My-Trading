"""Pure rules the desk connector shares with the desk's Study page (study.js, app.js).

Kept free of I/O and of the MCP SDK so the unit tests run with plain Python.
If the desk's combined call or sizing rules change, change them here too.
"""

from __future__ import annotations

import re
import time
from datetime import date

WORD = {1: "LONG", -1: "SHORT", 0: "NEUTRAL"}
STEP_HOURS = {"5m": 1 / 12, "15m": 0.25, "30m": 0.5, "1h": 1, "1d": 24}
COIN_RE = re.compile(r"-(USD|USDT|USDC|BTC|ETH|EUR)$")
POSITION_PCT = 5.0  # the desk's default paper position and its cap: 5% of the portfolio's equity (dashboard/risk.py)


def is_coin(ticker: str) -> bool:
    return bool(COIN_RE.search(ticker.upper())) or "/" in ticker


def window_text(timeframe: str, horizon: int) -> str:
    hours = STEP_HOURS.get(timeframe, 1) * (horizon or 48)
    return f"{int(hours // 24)} days" if hours >= 48 and hours % 24 == 0 else f"{round(hours)} hours"


# ---- the two opinions ----------------------------------------------------------------

def agent_view(report: dict, today: date | None = None) -> dict:
    text = f"{report.get('rating') or ''} {report.get('action') or ''}".lower()
    direction = 1 if re.search(r"buy|overweight", text) else -1 if re.search(r"sell|underweight", text) else 0
    age = None
    if report.get("date"):
        age = ((today or date.today()) - date.fromisoformat(report["date"])).days
    return {"report": report, "dir": direction, "age": age, "stale": age is not None and age > 7}


def kronos_view(run: dict, now: float | None = None) -> dict:
    r = run["result"]
    direction = -1 if r["down"] > r["up"] else 1 if r["up"] > r["down"] else 0
    plan_dir = (1 if r["plan"]["side"] == "long" else -1) if r.get("plan") else 0
    age_h = ((now or time.time()) - run["created"]) / 3600
    window_h = STEP_HOURS.get(r.get("timeframe"), 1) * (r.get("horizon") or 48)
    cal, rv = r.get("calibration"), r.get("reversion") or {}
    return {"run": run, "r": r, "dir": direction, "lean": r.get("lean") if plan_dir == direction else None,
            "age_h": age_h, "stale": age_h > window_h / 2, "cal_bad": cal is not None and cal < 0.6,
            "reversion": bool(rv.get("flagged")), "reversion_note": rv.get("label") or ""}


def latest_report(reports: list[dict], ticker: str) -> dict | None:
    mine = [r for r in reports if r.get("ticker") == ticker]
    mine.sort(key=lambda r: f"{r.get('date') or ''}{r.get('generated') or ''}", reverse=True)
    return mine[0] if mine else None


def latest_scan(runs: list[dict], ticker: str) -> dict | None:
    done = [r for r in runs if r["params"]["ticker"] == ticker and r["status"] == "done" and r.get("result")]
    return max(done, key=lambda r: r["created"]) if done else None


# ---- combined call ---------------------------------------------------------------------

def combine(a: dict | None, k: dict | None) -> dict:
    """LONG or SHORT only when both point the same way, Kronos's race favours the target, its
    band passed the recent-days check and neither opinion is stale. Agreement with caveats is
    a LEAN; disagreement is NO TRADE; a Hold, an even fan, or a race that favours the stop is WAIT."""
    if not a and not k:
        return {"call": "NO CALL YET", "why": ["Neither the agents nor Kronos has run on this ticker."]}
    if not k:
        return {"call": "HALF A CALL", "why": [f"Only the agents have spoken ({WORD[a['dir']].lower()}). Run Kronos for the short-term view."]}
    if k.get("reversion"):
        note = k.get("reversion_note") or "Kronos's fan only drifted back toward the average of the candles it saw."
        more = ("Run the agents for the longer-term view." if not a else
                f"The agents say {WORD[a['dir']].lower()}" + (f" ({a['report'].get('rating')})" if a["report"].get("rating") else "") +
                "; there is no real second opinion until Kronos is re-run after the price settles.")
        return {"call": "WAIT", "why": [note, more]}
    if not a:
        return {"call": "HALF A CALL", "why": [f"Only Kronos has spoken ({WORD[k['dir']].lower()}). Run the agents for the longer-term view."]}
    if a["dir"] == 0:
        return {"call": "WAIT", "why": [f"The agents rate it {a['report'].get('rating') or 'Hold'}, so there is no side to back. Kronos leans {WORD[k['dir']].lower()}."]}
    if k["dir"] == 0:
        return {"call": "WAIT", "why": ["Kronos's futures split evenly between up and down."]}
    if a["dir"] != k["dir"]:
        return {"call": "NO TRADE", "why": [f"They disagree: the agents say {WORD[a['dir']].lower()} ({a['report'].get('rating')}), "
                                            f"Kronos's futures lean {WORD[k['dir']].lower()}."]}
    word, r = WORD[a["dir"]], k["r"]
    if k["lean"] is not None and k["lean"] <= -0.05:
        return {"call": "WAIT", "why": [f"Both lean {word.lower()}, but at a {r['plan'].get('stop_pct')}% stop and {r['plan'].get('target_pct')}% target "
                                        f"Kronos's futures hit the stop first more often ({r['stop_first']} against {r['target_first']})."]}
    caveats = []
    if k["cal_bad"]:
        caveats.append(f"Kronos failed its recent-days check: {round(100 * r['calibration'])}% of real closes stayed in its band, against about 80%.")
    if r.get("calibration") is None:
        caveats.append("Kronos was not checked against recent days.")
    if k["lean"] is None:
        caveats.append("Kronos's stop and target race was run for the other side.")
    elif k["lean"] < 0.05:
        caveats.append(f"Kronos's race is close to break-even (target first {r['target_first']}, stop first {r['stop_first']}).")
    if a["stale"]:
        caveats.append(f"The agent report is {a['age']} days old.")
    if k["stale"]:
        caveats.append(f"The Kronos scan is {round(k['age_h'])} hours old, over half its window.")
    if caveats:
        return {"call": f"LEAN {word}", "why": caveats}
    return {"call": word, "why": ["Both point the same way and Kronos's race favours the target."]}


# ---- portfolios and sizing ------------------------------------------------------------------

def takes(portfolio: dict, ticker: str) -> bool:
    """Whether a portfolio's asset rule lets it open this ticker (closing is always allowed)."""
    if portfolio.get("rule", "any") == "any" or (portfolio.get("held") or {}).get(ticker):
        return True
    return (portfolio["rule"] == "crypto") == is_coin(ticker)


def pick_portfolio(portfolios: list[dict], active_id: str, ticker: str, wanted: str = "") -> dict:
    """The portfolio named by `wanted` (name or id, any case), else the active one unless its
    rule refuses the ticker, else the first one that takes it."""
    if wanted:
        w = wanted.strip().lower()
        for p in portfolios:
            if p["id"] == w or p["name"].lower() == w:
                return p
        names = ", ".join(p["name"] for p in portfolios)
        raise ValueError(f"No portfolio called {wanted!r}. You have: {names}.")
    active = next((p for p in portfolios if p["id"] == active_id), None)
    if active and takes(active, ticker):
        return active
    fit = next((p for p in portfolios if takes(p, ticker)), None)
    if not fit:
        raise ValueError(f"None of your portfolios can hold {ticker}.")
    return fit


def size_quantity(equity: float, price: float, pct: float, ticker: str) -> float:
    """`pct` percent of the portfolio's equity at `price`: whole shares, coins to 4 decimals."""
    if price <= 0 or equity <= 0 or pct <= 0:
        return 0.0
    q = equity * pct / 100 / price
    return int(q * 10000) / 10000 if is_coin(ticker) else float(int(q))
