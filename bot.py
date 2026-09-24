#!/usr/bin/env python3
"""
Momentum experiment bot (Coinbase Advanced, spot only).

Once per day (20:00-23:59 Europe/London window, first scheduled run wins):
  1. Verify the API key is scoped to the experiment portfolio with no Transfer permission.
  2. Compute 7-day returns for BTC-GBP, ETH-GBP, LINK-GBP from public market data.
  3. Read the experiment portfolio and cross-check its value against Coinbase's total.
  4. Apply safety rules (hard cap, 4-week duration, end threshold, pause threshold).
  5. Hold the coin with the highest positive 7-day return (GBP if all negative),
     switching only when the new target beats the current holding by >= 3pp.

Live trading requires BOTH config.json "dry_run": false AND the GitHub repository
variable LIVE_TRADING = "enabled". Otherwise the bot only logs what it would do.

Secrets are read from environment variables and are never logged or printed.
"""
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
TERMINAL_STATUSES = {"ended", "complete", "halted_cap", "halted_error"}
LOG_FIELDS = [
    "timestamp_utc", "run_id", "mode", "event", "asset", "product_id", "side",
    "base_size", "quote_gbp", "price_gbp", "fee_gbp", "order_id", "details",
]


class SkipToday(Exception):
    """Do nothing this run, alert, try again at the next check."""


class HaltExperiment(Exception):
    """Stop the experiment (live mode) until Dan intervenes."""

    def __init__(self, message, status="halted_error"):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- helpers

def D(x):
    return Decimal(str(x))


def floor_to(x, increment):
    increment = D(increment)
    return (D(x) / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


class Logger:
    def __init__(self, path, run_id, mode, now):
        self.path, self.run_id, self.mode, self.now = path, run_id, mode, now
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(LOG_FIELDS)

    def log(self, event, **fields):
        row = {k: "" for k in LOG_FIELDS}
        row.update(timestamp_utc=iso(self.now), run_id=self.run_id, mode=self.mode, event=event)
        for k, v in fields.items():
            if k not in row:
                raise KeyError(f"unknown log field {k}")
            row[k] = str(v)
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow(row)
        print(f"[{self.mode}] {event} " + " ".join(f"{k}={v}" for k, v in fields.items() if v != ""))


class Notifier:
    """ntfy push notifications. A failed alert never fails the run."""

    def __init__(self, topic):
        self.topic = (topic or "").strip()
        self.sent = []  # kept for tests

    def send(self, title, message, priority="default"):
        self.sent.append((title, message, priority))
        if not self.topic:
            print(f"[alert not sent - NTFY_TOPIC unset] {title}: {message}")
            return
        try:
            import requests
            requests.post(
                f"https://ntfy.sh/{self.topic}",
                data=message.encode("utf-8"),
                headers={"Title": title.encode("ascii", "replace").decode(), "Priority": priority},
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[alert failed: {type(e).__name__}] {title}")


# --------------------------------------------------------------------------- Coinbase

class CoinbaseGateway:
    """Thin wrapper around the official coinbase-advanced-py SDK, returning plain dicts."""

    def __init__(self, key_name, private_key):
        from coinbase.rest import RESTClient
        self.c = RESTClient(api_key=key_name, api_secret=private_key, timeout=20)

    def key_permissions(self):
        return self.c.get_api_key_permissions().to_dict()

    def portfolio_breakdown(self, portfolio_uuid):
        return self.c.get_portfolio_breakdown(portfolio_uuid, currency="GBP").to_dict()["breakdown"]

    def product(self, product_id):
        return self.c.get_public_product(product_id).to_dict()

    def price(self, product_id):
        return D(self.product(product_id)["price"])

    def close_at(self, product_id, t):
        """Close of the hourly candle containing time t."""
        ts = int(t.timestamp())
        data = self.c.get_public_candles(product_id, str(ts - 3 * 3600), str(ts + 3600), "ONE_HOUR").to_dict()
        eligible = [c for c in data.get("candles", []) if int(c["start"]) <= ts]
        if not eligible:
            raise SkipToday(f"no candle data for {product_id} at {iso(t)}")
        return D(max(eligible, key=lambda c: int(c["start"]))["close"])

    # Orders are routed to the key's own portfolio; key scope is verified before trading.
    def market_sell(self, product_id, base_size):
        return self.c.market_order_sell(
            client_order_id=str(uuid.uuid4()), product_id=product_id, base_size=str(base_size)
        ).to_dict()

    def market_buy(self, product_id, quote_size):
        return self.c.market_order_buy(
            client_order_id=str(uuid.uuid4()), product_id=product_id, quote_size=str(quote_size)
        ).to_dict()

    def get_order(self, order_id):
        return self.c.get_order(order_id).to_dict()["order"]


# --------------------------------------------------------------------------- strategy

def seven_day_returns(gw, cfg, now):
    out = {}
    for asset in cfg["universe"]:
        pid = f"{asset}-{cfg['quote_currency']}"
        p_now = gw.price(pid)
        p_then = gw.close_at(pid, now - timedelta(days=cfg["lookback_days"]))
        if p_then <= 0 or p_now <= 0:
            raise SkipToday(f"bad price data for {pid}")
        out[asset] = {"price": p_now, "price_then": p_then, "return_pct": (p_now / p_then - 1) * 100}
    return out


def choose_target(returns, current, margin_pp):
    """Returns (target, reason). GBP counts as a 0% return."""
    leader = max(returns, key=lambda a: returns[a]["return_pct"])  # ties: first in universe order
    target = leader if returns[leader]["return_pct"] > 0 else "GBP"
    ret = lambda a: D(0) if a == "GBP" else returns[a]["return_pct"]  # noqa: E731
    if target == current:
        return current, f"hold {current}: already the target"
    gap = ret(target) - ret(current)
    if gap >= D(margin_pp):
        return target, f"switch {current}->{target}: gap {gap:.2f}pp >= {margin_pp}pp"
    return current, f"hold {current}: {target} leads by only {gap:.2f}pp (< {margin_pp}pp)"


def read_portfolio(gw, cfg, prices):
    bd = gw.portfolio_breakdown(cfg["portfolio_uuid"])
    reported = D(bd["portfolio_balances"]["total_balance"]["value"])
    quote, dust = cfg["quote_currency"], D(cfg["dust_gbp"])
    positions = {}
    for p in bd.get("spot_positions", []) or []:
        asset = p["asset"]
        qty = D(p.get("total_balance_crypto") or 0)
        avail = D(p.get("available_to_trade_crypto", qty) or 0)
        fiat = D(p.get("total_balance_fiat") or 0)
        positions[asset] = {"qty": qty, "available": avail, "fiat": fiat}
    for asset, pos in positions.items():
        if asset not in cfg["universe"] and asset != quote and pos["fiat"] > dust:
            raise HaltExperiment(f"unexpected asset {asset} (GBP {pos['fiat']:.2f}) in experiment portfolio")
    values = {quote: positions.get(quote, {}).get("fiat", D(0))}
    for a in cfg["universe"]:
        values[a] = positions.get(a, {}).get("qty", D(0)) * prices[a]
    computed = sum(values.values())
    if reported <= 0:
        raise SkipToday("Coinbase reports a zero portfolio value")
    mismatch = abs(computed - reported) / reported * 100
    if mismatch > D(cfg["max_value_mismatch_pct"]):
        raise SkipToday(f"value mismatch {mismatch:.1f}%: computed GBP {computed:.2f} vs reported GBP {reported:.2f}")
    holding = max(values, key=lambda a: values[a])
    return {"reported": reported, "computed": computed, "values": values,
            "positions": positions, "holding": holding}


# --------------------------------------------------------------------------- execution (live only)

def wait_for_fill(gw, order_id, timeout_s, sleep=time.sleep):
    deadline = time.monotonic() + timeout_s
    while True:
        o = gw.get_order(order_id)
        status = o.get("status")
        if status == "FILLED":
            return o
        if status in ("CANCELLED", "EXPIRED", "FAILED"):
            raise HaltExperiment(f"order {order_id} ended with status {status}")
        if time.monotonic() > deadline:
            raise HaltExperiment(f"order {order_id} not filled within {timeout_s}s (status {status})")
        sleep(2)


def _place(gw, ctx, fn, product_id, size, log, side):
    ctx["orders_placed"] = True
    resp = fn(product_id, size)
    if not resp.get("success"):
        raise HaltExperiment(f"{side} {product_id} rejected: {json.dumps(resp.get('error_response', resp))[:300]}")
    order_id = resp["success_response"]["order_id"]
    log.log("ORDER", product_id=product_id, side=side, order_id=order_id,
            **({"base_size": size} if side == "SELL" else {"quote_gbp": size}))
    return order_id


def sell_all(gw, cfg, pf, asset, log, ctx, sleep):
    pid = f"{asset}-{cfg['quote_currency']}"
    prod = gw.product(pid)
    size = floor_to(pf["positions"][asset]["available"], prod["base_increment"])
    if size * D(prod["price"]) < D(prod.get("quote_min_size") or 0) or size <= 0:
        log.log("NOTE", asset=asset, details=f"{size} {asset} below minimum order; left as dust")
        return
    oid = _place(gw, ctx, gw.market_sell, pid, size, log, "SELL")
    o = wait_for_fill(gw, oid, cfg["order_fill_timeout_s"], sleep)
    common = dict(asset=asset, product_id=pid, side="SELL", base_size=o["filled_size"],
                  quote_gbp=o["filled_value"], price_gbp=o["average_filled_price"],
                  fee_gbp=o["total_fees"], order_id=oid)
    log.log("FILL", **common)
    log.log("DISPOSAL", **common, details="crypto disposal - potential CGT event")


def buy_with_gbp(gw, cfg, asset, gbp_available, log, ctx, sleep):
    pid = f"{asset}-{cfg['quote_currency']}"
    prod = gw.product(pid)
    quote = floor_to(D(gbp_available) / (1 + D(cfg["fee_buffer_pct"]) / 100), prod["quote_increment"])
    if quote > D(cfg["hard_cap_gbp"]):
        raise HaltExperiment(f"refusing buy of GBP {quote}: exceeds hard cap")
    if quote < D(prod.get("quote_min_size") or 0) or quote <= 0:
        raise HaltExperiment(f"GBP {quote} is below the minimum order for {pid}")
    oid = _place(gw, ctx, gw.market_buy, pid, quote, log, "BUY")
    o = wait_for_fill(gw, oid, cfg["order_fill_timeout_s"], sleep)
    log.log("FILL", asset=asset, product_id=pid, side="BUY", base_size=o["filled_size"],
            quote_gbp=o["filled_value"], price_gbp=o["average_filled_price"],
            fee_gbp=o["total_fees"], order_id=oid)


def liquidate(gw, cfg, pf, prices, log, ctx, sleep):
    for asset in cfg["universe"]:
        if pf["values"][asset] > D(cfg["dust_gbp"]):
            sell_all(gw, cfg, pf, asset, log, ctx, sleep)


def switch(gw, cfg, pf, prices, src, dst, log, ctx, sleep):
    if src != "GBP":
        sell_all(gw, cfg, pf, src, log, ctx, sleep)
    if dst != "GBP":
        fresh = read_portfolio(gw, cfg, prices)
        buy_with_gbp(gw, cfg, dst, fresh["values"][cfg["quote_currency"]], log, ctx, sleep)


# --------------------------------------------------------------------------- daily check

def _fmt_returns(returns):
    return " ".join(f"{a} {r['return_pct']:+.2f}%" for a, r in returns.items())


def daily_check(gw, cfg, state, now, today, log, notify, live, ctx, sleep):
    tag = "LIVE" if live else "DRY RUN"

    perms = gw.key_permissions()
    if perms.get("can_transfer"):
        raise HaltExperiment("API key has Transfer permission - it must be View + Trade only")
    if perms.get("portfolio_uuid") != cfg["portfolio_uuid"]:
        raise HaltExperiment("API key is not scoped to the Momentum experiment portfolio")
    if live and not perms.get("can_trade"):
        raise SkipToday("API key lacks Trade permission")

    returns = seven_day_returns(gw, cfg, now)
    prices = {a: r["price"] for a, r in returns.items()}
    for a, r in returns.items():
        log.log("SIGNAL", asset=a, product_id=f"{a}-GBP", price_gbp=f"{r['price']}",
                details=f"7d_return_pct={r['return_pct']:.4f} price_7d_ago={r['price_then']}")

    pf = read_portfolio(gw, cfg, prices)
    value = pf["reported"]
    bench = ""
    if state.get("benchmark"):
        b = state["benchmark"]
        bench_val = D(b["start_value"]) * prices["BTC"] / D(b["btc_price"])
        bench = f" btc_hold_benchmark_gbp={bench_val:.2f}"
    log.log("PORTFOLIO", asset=pf["holding"], quote_gbp=f"{value:.2f}",
            details=f"computed_gbp={pf['computed']:.2f} holding={pf['holding']}{bench}")

    if value > D(cfg["hard_cap_gbp"]):
        raise HaltExperiment(f"portfolio value GBP {value:.2f} exceeds hard cap GBP {cfg['hard_cap_gbp']}", "halted_cap")

    summary = f"{tag} | GBP {value:.2f} | {_fmt_returns(returns)} | holding {pf['holding']}"

    # 4-week duration (clock starts at the first live check)
    if live:
        if not state.get("activation_utc"):
            state["activation_utc"] = iso(now)
            state["benchmark"] = {"btc_price": str(prices["BTC"]), "start_value": str(value)}
            log.log("STATE", details="experiment activated (live); benchmark recorded")
        elif now >= parse_iso(state["activation_utc"]) + timedelta(days=cfg["duration_days"]):
            state["status"] = "complete"
            log.log("STATE", details="four weeks complete - checks stopped, position kept")
            notify("Momentum: 4 weeks complete",
                   summary + " | Checks stopped, position kept. What next?", "high")
            return

    # Pause handling
    if state.get("status") == "paused":
        until = parse_iso(state["paused_until_utc"])
        if now < until:
            left = (until - now).days + 1
            log.log("DECISION", details=f"paused until {state['paused_until_utc']}")
            notify("Momentum: paused", summary + f" | Paused, about {left} day(s) left.")
            return
        if not state.get("pause_acknowledged"):
            log.log("DECISION", details="pause period over - awaiting Dan's approval")
            notify("Momentum: approval needed",
                   summary + " | 7-day pause over. Trading stays off until you approve.", "high")
            return
        state["status"] = "active"
        state["paused_until_utc"] = None
        log.log("STATE", details="resumed after pause with Dan's approval")

    # End threshold
    if value <= D(cfg["end_threshold_gbp"]):
        if live:
            liquidate(gw, cfg, pf, prices, log, ctx, sleep)
            state["status"] = "ended"
        log.log("DECISION", details=f"END threshold hit (<= GBP {cfg['end_threshold_gbp']})"
                + ("" if live else " - would liquidate and stop"))
        notify("Momentum: EXPERIMENT ENDED" if live else "Momentum (dry): end would trigger",
               summary + " | End threshold reached. Liquidated to GBP, stopped permanently."
               if live else summary + " | Would liquidate and stop.", "urgent")
        return

    # Pause threshold
    pause_armed = not (cfg.get("pause_triggers_once", True) and state.get("pause_used"))
    if value <= D(cfg["pause_threshold_gbp"]) and pause_armed:
        if live:
            liquidate(gw, cfg, pf, prices, log, ctx, sleep)
            state.update(status="paused", pause_acknowledged=False, pause_used=True,
                         paused_until_utc=iso(now + timedelta(days=cfg["pause_days"])))
        log.log("DECISION", details=f"PAUSE threshold hit (<= GBP {cfg['pause_threshold_gbp']})"
                + ("" if live else " - would liquidate and pause"))
        notify("Momentum: PAUSED" if live else "Momentum (dry): pause would trigger",
               summary + (" | Liquidated to GBP. Paused 7 days, then needs your approval."
                          if live else " | Would liquidate and pause 7 days."), "urgent")
        return

    # Strategy decision
    holding = pf["holding"]
    target, reason = choose_target(returns, holding, cfg["switch_margin_pp"])
    if target != holding and state.get("last_switch_london_date") == today:
        target, reason = holding, reason + " - blocked: already switched today"
    log.log("DECISION", asset=target, details=reason)

    if target != holding:
        if live:
            switch(gw, cfg, pf, prices, holding, target, log, ctx, sleep)
            state["last_switch_london_date"] = today
        else:
            log.log("NOTE", details=f"dry run - would switch {holding} -> {target}")
    state["last_holding"] = target if live else holding
    notify(f"Momentum: {tag.lower()} check", summary + f" | {reason}")


def run(gw, cfg, state, now, log_path, notifier, live_var="", force=False,
        run_id="local", sleep=time.sleep):
    live_requested = cfg.get("dry_run") is False
    live = live_requested and live_var.strip().lower() == "enabled"
    log = Logger(log_path, run_id, "LIVE" if live else "DRY", now)
    london = now.astimezone(LONDON)
    today = london.date().isoformat()
    state["last_run_utc"] = iso(now)

    if state.get("status") in TERMINAL_STATUSES:
        print(f"Experiment status is {state['status']}: no action.")
        if force:
            notifier.send("Momentum: stopped", f"Status {state['status']}: {state.get('halt_reason') or ''}")
        return 0

    if not force:
        w0, w1 = cfg["check_window_london"]
        if not (w0 <= london.hour < w1):
            print(f"Outside check window ({london:%H:%M} London). Nothing to do.")
            return 0
        if state.get("last_check_london_date") == today:
            print("Already checked today. Nothing to do.")
            return 0

    if live_requested and not live:
        log.log("NOTE", details="config dry_run=false but LIVE_TRADING variable not 'enabled' - running dry")
    if live_var.strip().lower() == "enabled" and not live_requested:
        log.log("NOTE", details="LIVE_TRADING enabled but config dry_run=true - running dry")

    ctx = {"orders_placed": False}
    retry_later = False
    try:
        daily_check(gw, cfg, state, now, today, log, notifier.send, live, ctx, sleep)
        code = 0
    except SkipToday as e:
        log.log("ERROR", details=f"skipped today: {e}")
        notifier.send("Momentum: check skipped", f"No trades. {e}", "high")
        retry_later = True  # a later scheduled run in today's window may retry
        code = 1
    except Exception as e:  # noqa: BLE001  (includes HaltExperiment)
        msg = f"{type(e).__name__}: {e}"
        status = e.status if isinstance(e, HaltExperiment) else "halted_error"
        if live and (isinstance(e, HaltExperiment) or ctx["orders_placed"]):
            state["status"] = status
            state["halt_reason"] = msg
            log.log("HALT", details=f"{status}: {msg}")
            notifier.send("Momentum: HALTED", f"Experiment stopped ({status}). {msg} "
                          "Nothing more will happen until you review.", "urgent")
        else:
            log.log("ERROR", details=msg)
            retry_later = True
            notifier.send("Momentum: error", f"{'Dry run - ' if not live else ''}No trades. {msg}", "high")
        code = 1

    if not force and not retry_later:
        state["last_check_london_date"] = today
        if not live:
            state["dry_run_checks"] = state.get("dry_run_checks", 0) + 1
    return code


def main():
    cfg = load_json(os.path.join(HERE, "config.json"))
    state_path = os.path.join(HERE, "state.json")
    state = load_json(state_path)
    notifier = Notifier(os.environ.get("NTFY_TOPIC"))
    key_name = os.environ.get("COINBASE_API_KEY_NAME", "")
    private_key = os.environ.get("COINBASE_API_PRIVATE_KEY", "")
    if not key_name or not private_key:
        print("Missing COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY secrets.")
        notifier.send("Momentum: setup error", "API key secrets are missing; nothing ran.", "high")
        return 1
    gw = CoinbaseGateway(key_name, private_key)
    code = run(gw, cfg, state, datetime.now(timezone.utc), os.path.join(HERE, "log.csv"), notifier,
               live_var=os.environ.get("LIVE_TRADING", ""),
               force=os.environ.get("FORCE_RUN", "").lower() == "true",
               run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    save_json(state_path, state)
    return code


if __name__ == "__main__":
    sys.exit(main())
