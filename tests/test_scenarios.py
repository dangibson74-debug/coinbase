"""Mock-scenario tests for bot.py. No network, no API keys: Coinbase is replaced by FakeGateway.

Run: python -m unittest discover -s tests -v
"""
import copy
import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402

D = Decimal
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = bot.load_json(os.path.join(HERE, "config.json"))
PF = CFG["portfolio_uuid"]
FEE = D("0.006")
# 20:30 London (BST) on a Thursday: inside the check window
NOW = datetime(2026, 10, 1, 19, 30, tzinfo=timezone.utc)
TODAY = "2026-10-01"


def fresh_state():
    return {
        "activation_utc": None, "benchmark": None, "dry_run_checks": 0, "halt_reason": None,
        "last_check_london_date": None, "last_holding": None, "last_run_utc": None,
        "last_switch_london_date": None, "pause_acknowledged": False, "pause_used": False,
        "paused_until_utc": None, "status": "active",
    }


def live_cfg():
    cfg = copy.deepcopy(CFG)
    cfg["dry_run"] = False
    return cfg


class FakeGateway:
    """In-memory stand-in for CoinbaseGateway. Market orders fill instantly with a 0.6% fee."""

    def __init__(self, returns, holdings=None, gbp="50", prices=None):
        self.prices = {a: D(p) for a, p in (prices or {"BTC": "80000", "ETH": "3000", "LINK": "15"}).items()}
        self.prices_then = {a: self.prices[a] / (1 + D(str(r)) / 100) for a, r in returns.items()}
        self.qty = {a: D(0) for a in self.prices}
        for asset, gbp_value in (holdings or {}).items():
            self.qty[asset] = D(gbp_value) / self.prices[asset]
        self.cash = D(gbp)
        self.extra_assets = {}
        self.perms = {"can_view": True, "can_trade": True, "can_transfer": False, "portfolio_uuid": PF}
        self.reported_factor = D(1)
        self.reject_orders = False
        self.orders = []
        self._fills = {}

    # --- read side
    def key_permissions(self):
        return dict(self.perms)

    def total(self):
        return self.cash + sum(self.qty[a] * self.prices[a] for a in self.qty)

    def portfolio_breakdown(self, portfolio_uuid):
        assert portfolio_uuid == PF
        spot = [{"asset": "GBP", "total_balance_crypto": str(self.cash),
                 "available_to_trade_crypto": str(self.cash), "total_balance_fiat": str(self.cash)}]
        for a, q in self.qty.items():
            if q > 0:
                spot.append({"asset": a, "total_balance_crypto": str(q), "available_to_trade_crypto": str(q),
                             "total_balance_fiat": str(q * self.prices[a])})
        extra = D(0)
        for a, v in self.extra_assets.items():
            spot.append({"asset": a, "total_balance_crypto": "1", "available_to_trade_crypto": "1",
                         "total_balance_fiat": v})
            extra += D(v)
        reported = (self.total() + extra) * self.reported_factor
        return {"portfolio_balances": {"total_balance": {"value": str(reported), "currency": "GBP"}},
                "spot_positions": spot}

    def product(self, product_id):
        asset = product_id.split("-")[0]
        return {"product_id": product_id, "price": str(self.prices[asset]), "base_increment": "0.00000001",
                "quote_increment": "0.01", "quote_min_size": "1"}

    def price(self, product_id):
        return self.prices[product_id.split("-")[0]]

    def close_at(self, product_id, t):
        return self.prices_then[product_id.split("-")[0]]

    # --- trade side
    def _order(self, side, product_id, size):
        self.orders.append((side, product_id, D(size)))
        if self.reject_orders:
            return {"success": False, "error_response": {"error": "INSUFFICIENT_FUND"}}
        asset = product_id.split("-")[0]
        price = self.prices[asset]
        if side == "SELL":
            base = D(size)
            value = base * price
            self.qty[asset] -= base
            self.cash += value * (1 - FEE)
        else:
            value = D(size)
            base = value * (1 - FEE) / price
            self.cash -= value
            self.qty[asset] += base
        oid = f"order-{len(self.orders)}"
        self._fills[oid] = {"order_id": oid, "status": "FILLED", "filled_size": str(base),
                            "filled_value": str(value), "average_filled_price": str(price),
                            "total_fees": str(value * FEE)}
        return {"success": True, "success_response": {"order_id": oid}}

    def market_sell(self, product_id, base_size):
        return self._order("SELL", product_id, base_size)

    def market_buy(self, product_id, quote_size):
        return self._order("BUY", product_id, quote_size)

    def get_order(self, order_id):
        return self._fills[order_id]


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "log.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, gw, cfg=None, state=None, now=NOW, live=False, force=False):
        cfg = cfg or (live_cfg() if live else copy.deepcopy(CFG))
        state = state if state is not None else fresh_state()
        notifier = bot.Notifier("")
        code = bot.run(gw, cfg, state, now, self.log_path, notifier,
                       live_var="enabled" if live else "", force=force, sleep=lambda s: None)
        return code, state, notifier

    def events(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, newline="", encoding="utf-8") as f:
            return [r["event"] for r in csv.DictReader(f)]


class ChooseTargetTests(unittest.TestCase):
    def r(self, **kw):
        return {a: {"return_pct": D(str(v))} for a, v in kw.items()}

    def test_01_switches_to_leader_when_gap_at_least_3pp(self):
        target, _ = bot.choose_target(self.r(BTC=6, ETH=2, LINK=1), "ETH", "3")
        self.assertEqual(target, "BTC")

    def test_02_holds_when_gap_below_3pp(self):
        target, reason = bot.choose_target(self.r(BTC=4, ETH=2, LINK=1), "ETH", "3")
        self.assertEqual(target, "ETH")
        self.assertIn("only", reason)

    def test_03_moves_to_gbp_when_all_negative_and_holding_is_down_3pp(self):
        target, _ = bot.choose_target(self.r(BTC=-1, ETH=-5, LINK=-2), "ETH", "3")
        self.assertEqual(target, "GBP")

    def test_04_keeps_coin_when_all_negative_but_within_margin(self):
        target, _ = bot.choose_target(self.r(BTC=-1, ETH=-2, LINK=-4), "ETH", "3")
        self.assertEqual(target, "ETH")

    def test_05_gbp_counts_as_zero_when_buying_in(self):
        self.assertEqual(bot.choose_target(self.r(BTC=2.9, ETH=1, LINK=0), "GBP", "3")[0], "GBP")
        self.assertEqual(bot.choose_target(self.r(BTC=3.0, ETH=1, LINK=0), "GBP", "3")[0], "BTC")


class RunTests(Scenario):
    def test_06_outside_check_window_does_nothing(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        code, state, _ = self.go(gw, now=NOW - timedelta(hours=2), live=True)
        self.assertEqual(code, 0)
        self.assertEqual(gw.orders, [])
        self.assertIsNone(state["last_check_london_date"])

    def test_07_only_one_check_per_day(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        state = fresh_state()
        state["last_check_london_date"] = TODAY
        self.go(gw, state=state, live=True)
        self.assertEqual(gw.orders, [])
        self.assertEqual(self.events(), [])

    def test_08_dry_run_by_default_places_no_orders(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        code, state, notifier = self.go(gw)
        self.assertEqual(code, 0)
        self.assertEqual(gw.orders, [])
        self.assertEqual(state["dry_run_checks"], 1)
        self.assertEqual(state["last_check_london_date"], TODAY)
        self.assertIsNone(state["activation_utc"])
        self.assertIn("dry", notifier.sent[-1][0])

    def test_09_config_live_without_variable_stays_dry(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        state = fresh_state()
        bot.run(gw, live_cfg(), state, NOW, self.log_path, bot.Notifier(""), live_var="", sleep=lambda s: None)
        self.assertEqual(gw.orders, [])
        self.assertEqual(state["dry_run_checks"], 1)

    def test_10_variable_without_config_stays_dry(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        state = fresh_state()
        bot.run(gw, copy.deepcopy(CFG), state, NOW, self.log_path, bot.Notifier(""),
                live_var="enabled", sleep=lambda s: None)
        self.assertEqual(gw.orders, [])

    def test_11_live_buys_leader_from_gbp_and_activates(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1}, gbp="50")
        code, state, _ = self.go(gw, live=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(gw.orders), 1)
        side, pid, quote = gw.orders[0]
        self.assertEqual((side, pid), ("BUY", "BTC-GBP"))
        self.assertLess(quote, D("50"))  # fee buffer kept back
        self.assertEqual(state["activation_utc"], "2026-10-01T19:30:00Z")
        self.assertEqual(state["last_switch_london_date"], TODAY)
        self.assertEqual(state["last_holding"], "BTC")
        self.assertIn("FILL", self.events())

    def test_12_live_switch_sells_then_buys_and_logs_disposal(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1}, holdings={"ETH": "50"}, gbp="0")
        self.go(gw, live=True)
        self.assertEqual([(s, p) for s, p, _ in gw.orders], [("SELL", "ETH-GBP"), ("BUY", "BTC-GBP")])
        self.assertIn("DISPOSAL", self.events())

    def test_13_max_one_switch_per_day(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1}, holdings={"ETH": "50"}, gbp="0")
        state = fresh_state()
        state["last_switch_london_date"] = TODAY
        self.go(gw, state=state, live=True, force=True)
        self.assertEqual(gw.orders, [])

    def test_14_transfer_permission_halts(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        gw.perms["can_transfer"] = True
        code, state, _ = self.go(gw, live=True)
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "halted_error")
        self.assertIn("Transfer", state["halt_reason"])
        self.assertEqual(gw.orders, [])

    def test_15_key_for_wrong_portfolio_halts(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        gw.perms["portfolio_uuid"] = "some-other-portfolio"
        _, state, _ = self.go(gw, live=True)
        self.assertEqual(state["status"], "halted_error")
        self.assertEqual(gw.orders, [])

    def test_16_hard_cap_halts(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1}, gbp="75")
        _, state, _ = self.go(gw, live=True)
        self.assertEqual(state["status"], "halted_cap")
        self.assertEqual(gw.orders, [])

    def test_17_value_mismatch_skips_and_allows_retry(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        gw.reported_factor = D("1.2")
        code, state, _ = self.go(gw, live=True)
        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "active")
        self.assertIsNone(state["last_check_london_date"])
        self.assertEqual(gw.orders, [])

    def test_18_unexpected_asset_halts(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        gw.extra_assets = {"DOGE": "5"}
        _, state, _ = self.go(gw, live=True)
        self.assertEqual(state["status"], "halted_error")
        self.assertIn("DOGE", state["halt_reason"])

    def test_19_rejected_order_halts(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        gw.reject_orders = True
        _, state, _ = self.go(gw, live=True)
        self.assertEqual(state["status"], "halted_error")
        self.assertIn("rejected", state["halt_reason"])

    def test_20_pause_liquidates_then_waits_for_approval_and_triggers_once(self):
        gw = FakeGateway({"BTC": -2, "ETH": 1, "LINK": 1}, holdings={"BTC": "20"}, gbp="0")
        state = fresh_state()
        state["activation_utc"] = "2026-09-25T19:30:00Z"
        self.go(gw, state=state, live=True)
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["pause_used"])
        self.assertEqual([(s, p) for s, p, _ in gw.orders], [("SELL", "BTC-GBP")])

        # Seven days later, not yet approved: no trades
        n = len(gw.orders)
        later = NOW + timedelta(days=8)
        _, state, notifier = self.go(gw, state=state, now=later, live=True)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(len(gw.orders), n)
        self.assertIn("approval", notifier.sent[-1][0])

        # Approved: resumes, and the pause does not trigger a second time
        state["pause_acknowledged"] = True
        gw.prices_then = {a: p / D("1.08") for a, p in gw.prices.items()}  # all +8%
        _, state, _ = self.go(gw, state=state, now=later + timedelta(days=1), live=True)
        self.assertEqual(state["status"], "active")
        self.assertEqual(gw.orders[-1][0:2], ("BUY", "BTC-GBP"))

    def test_21_end_threshold_liquidates_and_stops(self):
        gw = FakeGateway({"BTC": 5, "ETH": 1, "LINK": 1}, holdings={"BTC": "15"}, gbp="0")
        state = fresh_state()
        state["activation_utc"] = "2026-09-25T19:30:00Z"
        self.go(gw, state=state, live=True)
        self.assertEqual(state["status"], "ended")
        self.assertEqual([(s, p) for s, p, _ in gw.orders], [("SELL", "BTC-GBP")])

    def test_22_four_weeks_complete_keeps_position(self):
        gw = FakeGateway({"BTC": 1, "ETH": 9, "LINK": 1}, holdings={"BTC": "50"}, gbp="0")
        state = fresh_state()
        state["activation_utc"] = bot.iso(NOW - timedelta(days=28))
        state["benchmark"] = {"btc_price": "80000", "start_value": "50"}
        self.go(gw, state=state, live=True)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(gw.orders, [])

    def test_23_terminal_status_does_nothing(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        for status in sorted(bot.TERMINAL_STATUSES):
            state = fresh_state()
            state["status"] = status
            code, _, _ = self.go(gw, state=state, live=True)
            self.assertEqual(code, 0)
        self.assertEqual(gw.orders, [])

    def test_24_manual_run_does_not_use_up_daily_check(self):
        gw = FakeGateway({"BTC": 8, "ETH": 1, "LINK": 1})
        _, state, _ = self.go(gw, now=NOW - timedelta(hours=6), force=True)
        self.assertIsNone(state["last_check_london_date"])
        self.assertIn("DECISION", self.events())


if __name__ == "__main__":
    unittest.main()
