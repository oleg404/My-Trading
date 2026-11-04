# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///
"""MCP connector for the local TradingAgents desk (http://127.0.0.1:8787).

Lets Claude Desktop or Claude Code read your paper portfolios, the combined
agents + Kronos call and each ticker's history, start runs, and record paper
trades. It only talks to the desk's own API on this machine, so the desk's
validation (asset rules, cash and margin checks) applies to everything.
Nothing here can reach a broker: every trade is a paper entry.

Run: uv run --script server.py   (Claude starts it on demand over stdio)
"""

from __future__ import annotations

import functools
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import logic  # noqa: E402

DESK = os.environ.get("DESK_URL", "http://127.0.0.1:8787").rstrip("/")
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

mcp = MCPServer(
    "trading-desk",
    instructions=(
        "Tools for the user's local TradingAgents desk: paper portfolios, AI-agent reports, Kronos price-path "
        "scans and the combined call that merges them. Everything is paper trading; no order reaches a broker. "
        "Before record_paper_trade with confirm=true, always show the user the ticket from the confirm=false call "
        "and wait for an explicit yes in the chat. New positions default to 10% of the chosen portfolio's equity. "
        "This is research tooling, not financial advice: say so when asked for a recommendation."
    ),
)


# ---- desk API --------------------------------------------------------------------------

class DeskError(RuntimeError):
    pass


def _call(path: str, body: dict | None = None, timeout: float = 60):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Origin": DESK})
    req = urllib.request.Request(DESK + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error")
        except ValueError:
            msg = None
        raise DeskError(msg or f"The desk answered HTTP {e.code} for {path}.") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise DeskError(f"The desk is not running at {DESK}. Start it with scripts/dashboard.sh in the My-Trading folder.") from None


def _money(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _pct(v, signed=True) -> str:
    return "—" if v is None else (f"{v:+.1%}" if signed else f"{v:.1%}")


def _ticker(raw: str) -> str:
    t = (raw or "").strip().upper()
    if not t:
        raise DeskError("Give a ticker, such as NVDA or BTC-USD.")
    return t


def _guard(fn):
    """Desk errors come back as plain text the model can relay, not as tracebacks."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (DeskError, ValueError) as e:
            return f"Could not do that: {e}"
    return wrapped


# ---- portfolios ---------------------------------------------------------------------------

@mcp.tool(title="List portfolios", annotations=READ)
@_guard
def list_portfolios() -> str:
    """Every paper portfolio, valued at live prices: equity, return since it started, open P&L,
    cash, positions, trades, asset rule and what it is testing. Marks the active one."""
    ov = _call("/api/portfolios")
    rules = {"any": "stocks and crypto", "stocks": "stocks only", "crypto": "crypto only"}
    lines = []
    for p in ov["portfolios"]:
        mark = " (active)" if p["id"] == ov["active"] else ""
        lines.append(f"- {p['name']}{mark} [{rules.get(p['rule'], p['rule'])}]: equity {_money(p['equity'])}, return {_pct(p['return'])} "
                     f"since {_money(p['start'])}, open P&L {_money(p['openPnl'])}, cash {_money(p['cash'])}, "
                     f"{p['positions']} positions, {p['trades']} trades" + (f". Tests: {p['note']}" if p.get("note") else ""))
    return "\n".join(lines) or "No portfolios."


@mcp.tool(title="Show a portfolio", annotations=READ)
@_guard
def get_portfolio(portfolio: str = "") -> str:
    """Positions (with live price, P&L and exit plan) and the last 10 trades of one portfolio.
    `portfolio` is its name; empty means the active one."""
    state = _call("/api/state")
    pf = logic.pick_portfolio(state["portfolios"], state["book"]["id"], "", portfolio) if portfolio else \
        next(p for p in state["portfolios"] if p["id"] == state["book"]["id"])
    book = _call(f"/api/portfolios/{pf['id']}")
    tickers = [p["ticker"] for p in book["positions"]]
    quotes = _call("/api/quotes?tickers=" + urllib.parse.quote(",".join(tickers), safe=","))["quotes"] if tickers else {}
    out = [f"{book['name']}: cash {_money(book['cash'])} {book['currency']}, holds {book['rule']}."]
    for p in book["positions"]:
        last = (quotes.get(p["ticker"]) or {}).get("price")
        avg, qty = p.get("average_price") or 0, p["quantity"]
        pnl = (last - avg) * qty if last else None
        plan = ", ".join(x for x in [f"stop {p['stop']}" if p.get("stop") else "", f"target {p['target']}" if p.get("target") else "",
                                     f"review by {p['review_by']}" if p.get("review_by") else ""] if x) or "no exit plan"
        out.append(f"- {p['ticker']} {'SHORT' if qty < 0 else 'LONG'} {abs(qty):g} @ {avg:.6g}, last {f'{last:.6g}' if last else '—'}, "
                   f"P&L {_money(pnl)} · {plan}")
    if not book["positions"]:
        out.append("No open positions.")
    if book["trades"]:
        out.append("Last trades:")
        for t in book["trades"][:10]:
            out.append(f"- {date.fromtimestamp(t['time']).isoformat()} {t['side'].upper()} {t['quantity']:g} {t['ticker']} @ {t['price']:g}"
                       + (f" ({t['note']})" if t.get("note") else ""))
    return "\n".join(out)


@mcp.tool(title="Switch the active portfolio", annotations=WRITE)
@_guard
def switch_portfolio(portfolio: str) -> str:
    """Make another portfolio the active one on the desk (the one its Portfolio tab and sizing use)."""
    state = _call("/api/state")
    pf = logic.pick_portfolio(state["portfolios"], state["book"]["id"], "", portfolio)
    _call("/api/portfolios/active", {"id": pf["id"]})
    return f"{pf['name']} is now the active portfolio."


@mcp.tool(title="Create a portfolio", annotations=WRITE)
@_guard
def create_portfolio(name: str, starting_cash: float, holds: str = "any", note: str = "", make_active: bool = False) -> str:
    """A new paper portfolio, for example to test one model or to keep crypto apart.
    `holds` is "any", "stocks" or "crypto"; `note` says what it is testing."""
    _call("/api/portfolios", {"name": name, "cash": starting_cash, "rule": holds, "note": note, "activate": make_active})
    return f"Created {name} with {_money(starting_cash)}, holding {holds}" + (" and made it active." if make_active else ".")


# ---- tickers: call, history, runs ------------------------------------------------------------

@mcp.tool(title="Combined call for a ticker", annotations=READ)
@_guard
def get_prediction(ticker: str) -> str:
    """The desk's combined call for a ticker from its latest AI-agent report and latest Kronos
    scan: LONG/SHORT only when both agree and Kronos holds up, LEAN when they agree with caveats,
    WAIT or NO TRADE otherwise. Includes both inputs and Kronos's levels."""
    t = _ticker(ticker)
    state, kr = _call("/api/state"), _call("/api/kronos")
    rep, scan = logic.latest_report(state["reports"], t), logic.latest_scan(kr["runs"], t)
    a = logic.agent_view(rep) if rep else None
    k = logic.kronos_view(scan) if scan else None
    c = logic.combine(a, k)
    out = [f"{t}: {c['call']}", *[f"- {w}" for w in c["why"]]]
    if a:
        r = a["report"]
        out.append(f"AI agents: {r.get('rating')} on {r.get('date')} ({a['age']}d ago); action {r.get('action') or '—'}, "
                   f"entry {r.get('entry') or '—'}, stop {r.get('stop') or '—'}, target {r.get('target') or '—'}, horizon {r.get('horizon') or '—'}.")
    if k:
        r = k["r"]
        plan = r.get("plan") or {}
        band = "—" if r.get("calibration") is None else f"{r['calibration']:.0%}"
        out.append(f"Kronos: {r['down']} of {r['n']} futures end down over the next {logic.window_text(r.get('timeframe'), r.get('horizon'))}; "
                   f"race ({plan.get('side')}) target first {r['target_first']}, stop first {r['stop_first']}, neither {r['neither']}; "
                   f"band check {band}; scan says {r.get('action')}. "
                   f"Levels: entry {plan.get('entry'):g}, stop {plan.get('stop'):g}, target {plan.get('target'):g}.")
    if a and k:
        out.append("The agents look weeks to months ahead; Kronos looks about two days ahead on 1-hour candles.")
    return "\n".join(out)


@mcp.tool(title="History of a ticker", annotations=READ)
@_guard
def get_history(ticker: str) -> str:
    """Every agent call and Kronos scan on a ticker, newest first, with what happened after:
    agent calls scored against their benchmark, Kronos scans against real candles (stop or target first)."""
    t = _ticker(ticker)
    state, perf, kr = _call("/api/state"), _call("/api/performance"), _call("/api/kronos")
    outcomes = _call("/api/kronos/outcomes?ticker=" + urllib.parse.quote(t), timeout=120)
    rows = []
    for e in perf.get("entries", []):
        if e["ticker"].upper() != t:
            continue
        result = "pending" if e.get("pending") else f"return {e.get('return')}%, {e.get('alpha')}% vs benchmark"
        rows.append((e["date"], f"{e['date']} AI agents {e['rating']}: {result}"))
    for run in kr["runs"]:
        if run["params"]["ticker"] != t or not run.get("result"):
            continue
        r, o = run["result"], outcomes.get(run["id"]) or {}
        when = date.fromtimestamp(run["created"]).isoformat()
        what = {"target": f"target hit first after {o.get('bars')} candles", "stop": f"stop hit first after {o.get('bars')} candles",
                "expired": f"neither in {o.get('horizon')} candles, price {o.get('move_pct')}%",
                "open": f"open, {o.get('bars')}/{o.get('horizon')} candles, price {o.get('move_pct')}%"}.get(o.get("status"), o.get("status") or "not checked")
        rows.append((when, f"{when} Kronos {r['side']} ({r['down']}/{r['n']} down, race {r['target_first']}/{r['stop_first']}/{r['neither']}, "
                           f"scan said {r['action']}): {what}"))
    rows.sort(reverse=True)
    return "\n".join(f"- {line}" for _, line in rows) or f"No agent calls or Kronos scans on {t} yet."


@mcp.tool(title="Run the agents and Kronos", annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True))
@_guard
def run_analysis(ticker: str, agents: bool = True, kronos: bool = True, stop_pct: float = 1.0, target_pct: float = 2.0) -> str:
    """Start the AI agents (a few minutes, uses the desk's model plan) and/or a Kronos scan
    (1,000 futures on 1-hour candles plus a 5-day check, about 7 minutes on this Mac) on a ticker.
    Returns at once; use get_activity to follow them and get_prediction when they finish."""
    t = _ticker(ticker)
    done, errors = [], []
    if agents:
        try:
            _call("/api/runs", {"ticker": t, "date": date.today().isoformat(), "asset": "",
                                "analysts": ["market", "social", "news", "fundamentals"], "usePortfolio": False})
            done.append("the AI agents")
        except DeskError as e:
            errors.append(f"Agents: {e}")
    if kronos:
        try:
            _call("/api/kronos", {"ticker": t, "timeframe": "1h", "side": "auto", "lookback": 128, "horizon": 48, "paths": 1000,
                                  "stopPct": stop_pct, "targetPct": target_pct, "calibrateDays": 5})
            done.append("Kronos")
        except DeskError as e:
            errors.append(f"Kronos: {e}")
    head = f"Started {' and '.join(done)} on {t}." if done else "Nothing started."
    return " ".join([head, *errors])


@mcp.tool(title="What is running", annotations=READ)
@_guard
def get_activity() -> str:
    """Agent runs, backtests and the Kronos scan currently queued or running on the desk."""
    state = _call("/api/state")
    out = [f"- Agents on {j.get('ticker') or ', '.join(j.get('tickers', []))}: {j['status']}" for j in state["jobs"]
           if j["status"] in ("queued", "running")]
    k = state.get("kronos")
    if k:
        out.append(f"- Kronos on {k['ticker']} {k['timeframe']}: {k['progress']}% ({k['stage']})")
    return "\n".join(out) or "Nothing is running."


@mcp.tool(title="Daily position check", annotations=READ)
@_guard
def get_daily_check() -> str:
    """The latest daily check: every position in every portfolio against its stop, target and
    review date at the live price, and whether the indicator screen leans against it."""
    c = _call("/api/checks").get("latest")
    if not c:
        return "No daily check has run yet."
    lines = [f"Check of {c['date']} {c['time']}: {c['act']} to act on, {c['warn']} to watch."]
    for i in c["items"]:
        where = f" in {i['portfolio']}" if i.get("portfolio") else ""
        last = "—" if i.get("last") is None else f"{i['last']:.6g}"
        lines.append(f"- {i['level'].upper()} {i['ticker']}{where} ({i['side']} {i['quantity']:g}, last {last}): {'; '.join(i['notes'])}")
    return "\n".join(lines)


@mcp.tool(title="Watchlist screen", annotations=READ)
@_guard
def get_watchlist(limit: int = 15) -> str:
    """The desk's free indicator screen of your watchlist, most interesting first: bias, moves and signals."""
    rows = [r for r in _call("/api/watchlist", timeout=120).get("rows", []) if not r.get("error")]
    rows.sort(key=lambda r: r.get("interest") or 0, reverse=True)
    out = []
    for r in rows[:max(1, min(limit, 50))]:
        moves = ", ".join(f"{k} {r[k]:+.1f}%" for k in ("d1", "d5", "d20") if r.get(k) is not None)
        out.append(f"- {r['ticker']} interest {r.get('interest')}, bias {r.get('bias') or '—'}, last {r.get('price')}, {moves}: "
                   + ("; ".join(r.get("signals") or []) or "quiet"))
    return "\n".join(out) or "The watchlist is empty."


# ---- paper trades ----------------------------------------------------------------------------

@mcp.tool(title="Record a paper trade", annotations=WRITE)
@_guard
def record_paper_trade(ticker: str, side: str, portfolio: str = "", size_pct: float = logic.POSITION_PCT,
                       quantity: float | None = None, stop: float | None = None, target: float | None = None,
                       note: str = "", confirm: bool = False) -> str:
    """Record a paper trade at the live price. `side` is "buy" (open a long or cover a short) or
    "sell" (close a long or open a short). Sized at `size_pct` of the portfolio's equity unless
    `quantity` is given. `portfolio` is a name; empty picks the active one, or the first whose
    asset rule allows the ticker. With confirm=false (the default) nothing is recorded: it returns
    the ticket. Show it to the user and call again with confirm=true only after they say yes."""
    t, side = _ticker(ticker), (side or "").strip().lower()
    if side not in ("buy", "sell"):
        raise ValueError('side must be "buy" or "sell".')
    state = _call("/api/state")
    pf = logic.pick_portfolio(state["portfolios"], state["book"]["id"], t, portfolio)
    if not logic.takes(pf, t):
        raise ValueError(f"{pf['name']} holds {pf['rule']} only, so it cannot open {t}.")
    price = (_call("/api/quotes?tickers=" + urllib.parse.quote(t))["quotes"].get(t) or {}).get("price")
    if not price:
        raise ValueError(f"No live price for {t} right now.")
    equity = pf.get("equity") or pf["cash"]
    qty = float(quantity) if quantity else logic.size_quantity(equity, price, size_pct, t)
    if qty <= 0:
        raise ValueError(f"{size_pct}% of {pf['name']}'s {_money(equity)} equity buys less than one unit at {price:g}.")
    value = qty * price
    ticket = (f"{side.upper()} {qty:g} {t} at about {price:g} (≈ {_money(value)}, {value / equity:.0%} of {pf['name']}'s "
              f"{_money(equity)} equity) into {pf['name']}" + (f", stop {stop:g}" if stop else "") + (f", target {target:g}" if target else "") + ".")
    if not confirm:
        return ("NOT RECORDED YET. Ticket: " + ticket + " Show this to the user; record it with confirm=true only after they say yes. "
                "Paper only: no order goes to a broker.")
    book = _call("/api/portfolio/trades", {"ticker": t, "side": side, "quantity": qty, "price": price, "stop": stop or "",
                                           "target": target or "", "note": (note or "via Claude")[:200], "portfolio": pf["id"]})
    return f"Recorded in {book['name']}: {ticket} Cash now {_money(book['cash'])}."


if __name__ == "__main__":
    mcp.run()
