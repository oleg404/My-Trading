"""Offline tests for the desk's Bybit demo link (tradingagents/dashboard/bybit_demo.py): which market a ticker maps
to, how a paper trade becomes a demo order, what is skipped or refused, and when a trade is queued at all. No
network and no keys: Bybit is a made-up exchange that fills every market order 0.1% worse than its price.

Run (from the tradingagents folder, so its package wins over the repo folder of the same name):
    cd tradingagents && .venv/bin/python ../tests/test_bybit_demo.py -v
"""

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

HOME = tempfile.mkdtemp(prefix="desk-bybit-")
os.environ["TRADINGAGENTS_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tradingagents" / "dashboard"))

import bybit_demo  # noqa: E402
import portfolios  # noqa: E402
import store  # noqa: E402

MARKETS = [("BTCUSDT", "0.001", "0.001"), ("SOLUSDT", "0.1", "0.1"), ("1000BONKUSDT", "100", "100")]
PRICES = {"BTCUSDT": 60000.0, "SOLUSDT": 200.0, "1000BONKUSDT": 0.02}
REAL_SEND = bybit_demo._send
ok = lambda result: {"retCode": 0, "retMsg": "OK", "result": result}


class Exchange:
    """Stands in for bybit_demo._send. Keeps the demo account's positions and every call it was sent."""

    def __init__(self):
        self.calls, self.orders, self.fills, self.positions = [], [], {}, {}
        self.refuse = self.bad_key = self.down = self.slow_fills = False

    def __call__(self, method, url, headers, data):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        if self.down:
            raise OSError("no route to host")
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if parts.path == "/v5/market/instruments-info":
            return ok({"nextPageCursor": "", "list": [
                {"symbol": s, "quoteCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual",
                 "lotSizeFilter": {"qtyStep": step, "minOrderQty": least, "minNotionalValue": "5"}} for s, step, least in MARKETS]
                + [{"symbol": "ETHUSDC", "quoteCoin": "USDC", "status": "Trading", "contractType": "LinearPerpetual", "lotSizeFilter": {}}]})
        if self.bad_key:
            return {"retCode": 10003, "retMsg": "API key is invalid."}
        if parts.path == "/v5/account/wallet-balance":
            return ok({"list": [{"totalEquity": "50000", "totalWalletBalance": "49990", "totalPerpUPL": "10"}]})
        if parts.path == "/v5/position/list":
            wanted = [s for s in self.positions if query.get("symbol") in (None, s)]
            return ok({"nextPageCursor": "", "list": [
                {"symbol": s, "side": "Buy" if self.positions[s] > 0 else "Sell" if self.positions[s] < 0 else "", "size": str(abs(self.positions[s])),
                 "avgPrice": str(PRICES[s]), "unrealisedPnl": "1.5", "curRealisedPnl": "-0.2", "liqPrice": ""} for s in wanted]})
        if parts.path == "/v5/order/create":
            order = json.loads(data)
            if self.refuse:
                return {"retCode": 110007, "retMsg": "ab not enough for new order"}
            self.orders.append(order)
            qty, buy = Decimal(order["qty"]), order["side"] == "Buy"
            self.positions[order["symbol"]] = self.positions.get(order["symbol"], Decimal(0)) + (qty if buy else -qty)
            price = PRICES[order["symbol"]] * (1.001 if buy else 0.999)
            self.fills[order["orderLinkId"]] = [{"execType": "Trade", "execQty": order["qty"], "execPrice": str(price),
                                                 "execFee": str(float(qty) * price * 0.00055)}]
            return ok({"orderId": order["orderLinkId"]})
        if parts.path == "/v5/execution/list":
            return ok({"list": [] if self.slow_fills else self.fills.get(query["orderId"], [])})
        if parts.path == "/v5/account/transaction-log":
            return ok({"nextPageCursor": "", "list": [{"funding": "-0.5"}, {"funding": "0.2"}]})
        raise AssertionError(f"unexpected call: {method} {url}")


def trade(before, after, ticker="BTC-USD", price=60000.0, **more):
    return {"portfolio": "x", "ticker": ticker, "before": before, "after": after, "price": price, "note": "test", **more}


class DeskCase(unittest.TestCase):
    keys = ("demo-key", "demo-secret")

    def setUp(self):
        self.bybit = Exchange()
        for target, new in ((bybit_demo, {"_send": self.bybit, "_keys": lambda: self.keys, "_start": lambda: None, "FILL_PAUSE": 0}),):
            for name, value in new.items():
                patch = mock.patch.object(target, name, value)
                patch.start()
                self.addCleanup(patch.stop)
        bybit_demo._cache.clear()
        while not bybit_demo._queue.empty():
            bybit_demo._queue.get()
        for f in (bybit_demo.LOG, bybit_demo.PREFS):
            f.unlink(missing_ok=True)

    def copy(self, *args, **kwargs):
        return bybit_demo.handle(trade(*args, **kwargs))

    def signed(self):
        return [c for c in self.bybit.calls if "X-BAPI-SIGN" in c["headers"]]


class MarketTest(DeskCase):
    def test_a_ticker_maps_to_its_usdt_perpetual(self):
        self.assertEqual(bybit_demo.market_for("BTC-USD")["symbol"], "BTCUSDT")
        self.assertEqual(bybit_demo.market_for("sol-usdt")["symbol"], "SOLUSDT")

    def test_a_cheap_coin_maps_to_its_lot_of_a_thousand(self):
        market = bybit_demo.market_for("BONK-USD")
        self.assertEqual((market["symbol"], market["lot"], market["step"]), ("1000BONKUSDT", 1000, "100"))

    def test_no_market_for_an_unlisted_coin_a_stock_or_another_quote(self):
        for ticker in ("HTX-USD", "NVDA", "BTC-EUR", "ETH-USDC"):
            self.assertIsNone(bybit_demo.market_for(ticker), ticker)

    def test_the_market_list_is_read_once(self):
        bybit_demo.market_for("BTC-USD")
        bybit_demo.market_for("SOL-USD")
        self.assertEqual(len(self.bybit.calls), 1)

    def test_quantities_round_down_to_the_step_and_print_plainly(self):
        self.assertEqual(bybit_demo._text(bybit_demo._floor(Decimal("0.0129"), "0.001")), "0.012")
        self.assertEqual(bybit_demo._text(bybit_demo._floor(Decimal("1234.567"), "100")), "1200")
        self.assertEqual(bybit_demo._text(bybit_demo._floor(Decimal("0.3"), "0.1")), "0.3")


class SigningTest(DeskCase):
    def test_signed_calls_go_only_to_the_demo_address(self):
        self.copy(0, -0.0125)
        bybit_demo.overview()
        self.assertTrue(self.signed())
        for call in self.signed():
            self.assertTrue(call["url"].startswith("https://api-demo.bybit.com/"), call["url"])

    def test_the_public_market_call_carries_no_key(self):
        self.copy(0, -0.0125)
        public = [c for c in self.bybit.calls if not c["url"].startswith("https://api-demo.bybit.com/")]
        self.assertEqual([urlsplit(c["url"]).netloc for c in public], ["api.bybit.com"])
        self.assertNotIn("X-BAPI-API-KEY", public[0]["headers"])

    def test_the_signature_covers_time_key_window_and_payload(self):
        self.copy(0, -0.0125)
        for call in self.signed():
            payload = call["data"].decode() if call["method"] == "POST" else urlsplit(call["url"]).query
            h = call["headers"]
            plain = h["X-BAPI-TIMESTAMP"] + "demo-key" + h["X-BAPI-RECV-WINDOW"] + payload
            self.assertEqual(h["X-BAPI-SIGN"], hmac.new(b"demo-secret", plain.encode(), hashlib.sha256).hexdigest())

    def test_a_bare_401_reads_as_a_key_bybit_did_not_accept(self):
        def answer(code, body):
            return mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", code, "no", {}, io.BytesIO(body)))
        with answer(401, b""):
            self.assertIn("created in Demo Trading", REAL_SEND("GET", "https://api-demo.bybit.com/x", {}, None)["retMsg"])
        with answer(400, b'{"retCode": 10001, "retMsg": "params error"}'):
            self.assertEqual(REAL_SEND("GET", "https://api-demo.bybit.com/x", {}, None)["retCode"], 10001)
        with answer(502, b"<html>bad gateway</html>"), self.assertRaises(OSError):
            REAL_SEND("GET", "https://api-demo.bybit.com/x", {}, None)

    def test_no_keys_means_no_signed_call(self):
        self.keys = ("", "")
        rows = self.copy(0, -0.0125)
        self.assertEqual(rows[0]["status"], "rejected")
        self.assertFalse(self.signed())


class CopyTest(DeskCase):
    def test_a_new_short_sends_a_market_sell_of_the_same_size(self):
        rows = self.copy(0, -0.0125)
        order = self.bybit.orders[0]
        self.assertEqual({k: order[k] for k in ("category", "symbol", "side", "orderType", "qty", "reduceOnly", "positionIdx")},
                         {"category": "linear", "symbol": "BTCUSDT", "side": "Sell", "orderType": "Market", "qty": "0.012", "reduceOnly": False, "positionIdx": 0})
        self.assertEqual((rows[0]["status"], rows[0]["action"], rows[0]["side"], rows[0]["demoQty"]), ("filled", "open", "sell", 0.012))

    def test_the_row_says_how_far_the_fill_sat_from_the_paper_price(self):
        sell = self.copy(0, -0.0125)[0]
        buy = self.copy(0, 2.0, ticker="SOL-USD", price=200.0)[0]
        self.assertAlmostEqual(sell["demoPrice"], 59940.0)
        self.assertAlmostEqual(sell["gapPct"], 0.1)  # sold 0.1% lower than paper: worse
        self.assertAlmostEqual(buy["gapPct"], 0.1)  # bought 0.1% higher than paper: worse
        self.assertAlmostEqual(sell["fee"], 0.012 * 59940.0 * 0.00055, places=5)

    def test_a_cheap_coin_is_sent_in_lots_and_logged_in_coins(self):
        row = self.copy(0, -1234567.0, ticker="BONK-USD", price=0.00002)[0]
        self.assertEqual((self.bybit.orders[0]["symbol"], self.bybit.orders[0]["qty"]), ("1000BONKUSDT", "1200"))
        self.assertEqual(row["demoQty"], 1200000.0)
        self.assertAlmostEqual(row["demoPrice"], 0.02 * 0.999 / 1000)

    def test_adding_to_a_position_sends_only_what_was_added(self):
        self.copy(0, -0.0125)
        row = self.copy(-0.0125, -0.0375)[0]
        self.assertEqual((row["action"], self.bybit.orders[1]["side"], self.bybit.orders[1]["qty"], self.bybit.orders[1]["reduceOnly"]),
                         ("add", "Sell", "0.025", False))

    def test_a_close_of_a_position_the_demo_never_had_is_skipped(self):
        row = self.copy(-0.5, 0)[0]
        self.assertEqual((row["status"], row["action"]), ("skipped", "close"))
        self.assertIn("opened before the link", row["why"])
        self.assertEqual(self.bybit.orders, [])

    def test_a_close_is_skipped_when_the_demo_holds_the_other_side(self):
        self.bybit.positions["BTCUSDT"] = Decimal("0.012")
        self.assertEqual(self.copy(-0.0125, 0)[0]["status"], "skipped")
        self.assertEqual(self.bybit.orders, [])

    def test_a_full_close_closes_all_the_demo_holds(self):
        self.bybit.positions["BTCUSDT"] = Decimal("-0.012")
        row = self.copy(-0.0125, 0)[0]
        order = self.bybit.orders[0]
        self.assertEqual((order["side"], order["qty"], order["reduceOnly"], row["action"]), ("Buy", "0.012", True, "close"))
        self.assertEqual(self.bybit.positions["BTCUSDT"], 0)

    def test_a_half_close_closes_half_of_what_the_demo_holds(self):
        self.bybit.positions["BTCUSDT"] = Decimal("-0.012")
        row = self.copy(-0.0125, -0.00625)[0]
        self.assertEqual((self.bybit.orders[0]["qty"], self.bybit.orders[0]["reduceOnly"], row["action"]), ("0.006", True, "reduce"))
        self.assertAlmostEqual(row["paperQty"], 0.00625)

    def test_a_reduction_below_the_smallest_order_is_skipped(self):
        self.bybit.positions["BTCUSDT"] = Decimal("-0.001")
        row = self.copy(-0.0125, -0.00625)[0]
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(self.bybit.orders, [])

    def test_a_flip_closes_the_demo_position_then_opens_the_other_side(self):
        self.bybit.positions["BTCUSDT"] = Decimal("-0.012")
        rows = self.copy(-0.0125, 0.0125)
        self.assertEqual([(r["action"], r["status"]) for r in rows], [("close", "filled"), ("open", "filled")])
        self.assertEqual([(o["side"], o["qty"], o["reduceOnly"]) for o in self.bybit.orders], [("Buy", "0.012", True), ("Buy", "0.012", False)])
        self.assertEqual(self.bybit.positions["BTCUSDT"], Decimal("0.012"))

    def test_a_coin_bybit_does_not_trade_is_skipped_without_touching_the_account(self):
        row = self.copy(0, -100.0, ticker="HTX-USD", price=2.0)[0]
        self.assertEqual(row["status"], "skipped")
        self.assertIn("no USDT perpetual for HTX", row["why"])
        self.assertFalse(self.signed())

    def test_an_order_below_the_smallest_size_or_value_is_skipped(self):
        self.assertEqual(self.copy(0, -0.0004)[0]["status"], "skipped")  # under 0.001 BTC
        self.assertEqual(self.copy(0, -0.1, ticker="SOL-USD", price=20.0)[0]["status"], "skipped")  # 2 USDT, under 5
        self.assertEqual(self.bybit.orders, [])

    def test_a_refused_order_is_logged_with_bybits_reason(self):
        self.bybit.refuse = True
        row = self.copy(0, -0.0125)[0]
        self.assertEqual(row["status"], "rejected")
        self.assertIn("ab not enough for new order", row["why"])

    def test_an_unreachable_exchange_is_logged_not_raised(self):
        bybit_demo.market_for("BTC-USD")
        self.bybit.down = True
        self.assertEqual(self.copy(0, -0.0125)[0]["status"], "error")
        bybit_demo._cache.clear()
        row = self.copy(0, -0.0125)[0]
        self.assertEqual(row["status"], "error")
        self.assertIn("market list", row["why"])

    def test_an_order_whose_fill_is_not_listed_yet_is_marked_sent(self):
        self.bybit.slow_fills = True
        row = self.copy(0, -0.0125)[0]
        self.assertEqual((row["status"], row["demoQty"], len(self.bybit.orders)), ("sent", 0.012, 1))
        self.assertNotIn("gapPct", row)

    def test_a_sent_order_gets_its_fill_on_a_later_look(self):
        self.bybit.slow_fills = True
        self.copy(0, -0.0125)
        self.assertEqual(bybit_demo.settle(), 0)
        self.bybit.slow_fills = False
        self.assertEqual(bybit_demo.settle(), 1)
        row = store.read_json(bybit_demo.LOG, [])[0]
        self.assertEqual((row["status"], row["demoQty"]), ("filled", 0.012))
        self.assertAlmostEqual(row["demoPrice"], 59940.0)
        self.assertAlmostEqual(row["gapPct"], 0.1)
        self.assertNotIn("why", row)
        self.assertEqual(bybit_demo.settle(), 0)

    def test_every_row_lands_in_the_log_newest_first(self):
        self.copy(0, -0.0125)
        self.copy(0, -100.0, ticker="HTX-USD", price=2.0)
        self.assertEqual([r["ticker"] for r in store.read_json(bybit_demo.LOG, [])], ["HTX-USD", "BTC-USD"])

    def test_a_failure_is_announced_and_a_skip_is_not(self):
        with mock.patch.object(bybit_demo, "notify") as said:
            self.copy(0, -100.0, ticker="HTX-USD", price=2.0)
            said.assert_not_called()
            self.bybit.refuse = True
            self.copy(0, -0.0125)
            said.assert_called_once()
            self.assertIn("BTC-USD", said.call_args[0][0])


class BookCase(DeskCase):
    """A coin book of 100,000 to trade in, and the link to it."""

    def setUp(self):
        super().setUp()
        quotes = mock.patch.object(store, "quotes", side_effect=lambda t: {x: {"price": None} for x in t})
        quotes.start()
        self.addCleanup(quotes.stop)
        self.pid = self.new_book("crypto")

    def paper(self, side="sell", ticker="BTC-USD", quantity=0.0125, price=60000.0, portfolio=None):
        return portfolios.record_trade({"portfolio": portfolio or self.pid, "ticker": ticker, "side": side, "quantity": quantity,
                                        "price": price, "stop": price * 1.05, "target": price * 0.9, "note": "from a test"})

    def new_book(self, rule, cash=100000):
        return portfolios.create({"name": f"{rule} {uuid.uuid4().hex[:12]}", "rule": rule, "cash": cash})["active"]

    def link(self):
        bybit_demo.save_prefs({"portfolio": self.pid})
        bybit_demo.save_prefs({"enabled": True})


class LinkTest(BookCase):

    def test_nothing_is_copied_until_the_link_is_switched_on(self):
        self.paper()
        self.assertTrue(bybit_demo._queue.empty())
        self.assertEqual(self.bybit.calls, [])

    def test_a_trade_in_the_linked_book_is_queued_with_the_position_before_and_after(self):
        self.link()
        self.paper()
        self.paper(side="buy", quantity=0.005)
        first, second = bybit_demo._queue.get_nowait(), bybit_demo._queue.get_nowait()
        self.assertEqual((first["ticker"], first["before"], first["after"], first["price"]), ("BTC-USD", 0.0, -0.0125, 60000.0))
        self.assertAlmostEqual(second["before"], -0.0125)
        self.assertAlmostEqual(second["after"], -0.0075)
        self.assertEqual(bybit_demo.handle(first)[0]["status"], "filled")

    def test_another_book_and_a_stock_are_not_queued(self):
        self.link()
        other = self.new_book("any")
        self.paper(portfolio=other)
        bybit_demo.save_prefs({"portfolio": other})
        self.paper(ticker="NVDA", quantity=5, price=150.0, portfolio=other)
        self.assertTrue(bybit_demo._queue.empty())

    def test_switching_off_stops_the_copies(self):
        self.link()
        bybit_demo.save_prefs({"enabled": False})
        self.paper()
        self.assertTrue(bybit_demo._queue.empty())

    def test_it_cannot_be_switched_on_without_keys(self):
        self.keys = ("", "")
        bybit_demo.save_prefs({"portfolio": self.pid})
        with self.assertRaisesRegex(ValueError, "BYBIT_DEMO_API_KEY"):
            bybit_demo.save_prefs({"enabled": True})
        self.assertFalse(bybit_demo.prefs()["enabled"])

    def test_it_cannot_be_switched_on_with_a_key_bybit_refuses(self):
        self.bybit.bad_key = True
        bybit_demo.save_prefs({"portfolio": self.pid})
        with self.assertRaisesRegex(ValueError, "refused the demo key"):
            bybit_demo.save_prefs({"enabled": True})
        self.assertFalse(bybit_demo.prefs()["enabled"])

    def test_it_cannot_be_switched_on_before_a_book_is_chosen(self):
        with self.assertRaisesRegex(ValueError, "Choose the portfolio"):
            bybit_demo.save_prefs({"enabled": True})

    def test_a_stocks_only_book_cannot_be_linked(self):
        stocks = self.new_book("stocks")
        with self.assertRaisesRegex(ValueError, "stocks only"):
            bybit_demo.save_prefs({"portfolio": stocks})

    def test_copy_open_queues_only_what_the_demo_account_lacks(self):
        self.paper()  # opened while the link was off
        self.paper(ticker="SOL-USD", quantity=2.0, price=200.0)
        self.paper(ticker="HTX-USD", quantity=100.0, price=2.0)  # no Bybit market
        self.link()
        self.bybit.positions["SOLUSDT"] = Decimal("-2")  # already there
        self.assertEqual(bybit_demo.copy_open(), 1)
        event = bybit_demo._queue.get_nowait()
        self.assertEqual((event["ticker"], event["before"], event["after"], event["price"]), ("BTC-USD", 0.0, -0.0125, 60000.0))
        self.assertEqual(bybit_demo.handle(event)[0]["status"], "filled")
        self.assertEqual(bybit_demo.copy_open(), 0)  # a second press sends nothing more

    def test_copy_open_needs_the_link_on(self):
        self.paper()
        with self.assertRaisesRegex(ValueError, "Switch the link on"):
            bybit_demo.save_prefs({"copyOpen": True})
        self.assertTrue(bybit_demo._queue.empty())

    def test_a_copy_that_blows_up_leaves_the_paper_trade_standing(self):
        self.link()
        with mock.patch.object(bybit_demo, "on_trade", side_effect=RuntimeError("boom")), mock.patch("builtins.print"):
            data = self.paper()
        self.assertEqual([p["ticker"] for p in data["positions"]], ["BTC-USD"])


class OverviewTest(BookCase):
    def test_totals_add_up_the_fills_fees_and_the_gap_to_paper(self):
        self.copy(0, -0.0125)
        self.copy(0, 2.0, ticker="SOL-USD", price=200.0)
        self.copy(0, -100.0, ticker="HTX-USD", price=2.0)
        self.bybit.refuse = True
        self.copy(0, -0.0125)
        t = bybit_demo.totals(store.read_json(bybit_demo.LOG, []))
        self.assertEqual((t["orders"], t["filled"], t["skipped"], t["failed"]), (4, 2, 1, 1))
        self.assertAlmostEqual(t["gapPct"], 0.1)
        self.assertAlmostEqual(t["gapCost"], 0.001 * (0.012 * 60000 + 2.0 * 200), places=3)
        self.assertAlmostEqual(t["fees"], 0.00055 * (0.012 * 59940 + 2.0 * 200.2), places=3)

    def test_the_paper_book_is_laid_beside_the_demo_account(self):
        self.link()
        self.paper()
        self.paper(ticker="SOL-USD", quantity=2.0, price=200.0)
        self.paper(ticker="HTX-USD", quantity=100.0, price=2.0)
        bybit_demo.handle(bybit_demo._queue.get_nowait())  # only the BTC short reaches the demo account
        self.bybit.positions["1000BONKUSDT"] = Decimal("-500")
        view = bybit_demo.overview()
        self.assertEqual({r["ticker"] or r["symbol"]: r["state"] for r in view["compare"]},
                         {"BTC-USD": "size differs", "SOL-USD": "paper only", "HTX-USD": "no Bybit market", "1000BONKUSDT": "demo only"})
        self.assertEqual((view["account"]["equity"], view["account"]["positions"], view["funding"]), (50000.0, 2, -0.3))
        self.assertTrue(view["enabled"])

    def test_a_copy_within_rounding_of_the_paper_size_counts_as_copied(self):
        self.link()
        self.paper(quantity=0.012)
        bybit_demo.handle(bybit_demo._queue.get_nowait())
        self.assertEqual(bybit_demo.overview()["compare"][0]["state"], "copied")

    def test_without_keys_the_overview_asks_bybit_nothing(self):
        self.keys = ("", "")
        view = bybit_demo.overview()
        self.assertEqual((view["configured"], view["enabled"], view["account"]), (False, False, None))
        self.assertEqual(self.bybit.calls, [])

    def test_a_refused_key_is_reported_in_the_overview(self):
        self.bybit.bad_key = True
        self.assertIn("API key is invalid", bybit_demo.overview()["error"])

    def test_a_deleted_book_switches_the_link_off_in_the_overview(self):
        self.link()
        self.new_book("crypto")
        portfolios.delete(self.pid)
        view = bybit_demo.overview()
        self.assertEqual((view["enabled"], view["portfolio"]), (False, None))

    def test_the_overview_never_carries_the_key_or_the_secret(self):
        self.link()
        text = json.dumps(bybit_demo.overview())
        self.assertNotIn("demo-key", text)
        self.assertNotIn("demo-secret", text)


if __name__ == "__main__":
    unittest.main()
