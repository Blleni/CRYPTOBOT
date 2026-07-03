"""
Hyperliquid Whale Mirror
========================
Watches a whale wallet on Hyperliquid in real time and mirrors its perp
trades, scaled to your capital: whale risks 5% of their account → you
risk 5% of yours.

Run it:
    python mirror.py

Everything is controlled by config.json. Default mode is "paper":
simulated fills, no real money, no account needed — just watch.

How it works (top to bottom):
  1. subscribe to the whale's fills over Hyperliquid's websocket (pushed
     to us the moment they trade — usually well under 1 second)
  2. figure out what the fill means: opening long/short, closing, flipping
  3. size our copy: (whale trade notional / whale account value) × our equity
  4. execute: paper = pretend fill at the whale's price; live = market order
  5. record the trade, update the live dashboard, append to logs/trades.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ────────────────────────────────────────────────────────────────────
# config
# ────────────────────────────────────────────────────────────────────
CFG = json.loads(Path("config.json").read_text())
WHALE = CFG["whale_address"].lower()
MODE = CFG.get("mode", "paper")
API_URL = (constants.TESTNET_API_URL if CFG.get("network") == "testnet"
           else constants.MAINNET_API_URL)
RISK = CFG.get("risk", {})
MAX_FRACTION = float(RISK.get("max_position_fraction", 0.25))
MIN_BALANCE = float(RISK.get("min_viable_balance_usd", 100))
MAX_AGE = float(RISK.get("max_signal_age_seconds", 3))

STATE_FILE = Path("state/portfolio.json")
AUDIT_FILE = Path("logs/trades.jsonl")
for p in (STATE_FILE.parent, AUDIT_FILE.parent):
    p.mkdir(parents=True, exist_ok=True)

LOCK = threading.Lock()
console = Console()


def audit(event: str, **fields) -> None:
    """Append one timestamped line to the audit log."""
    rec = {"ts": time.time(),
           "iso": datetime.now(timezone.utc).isoformat(),
           "event": event, **fields}
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


# ────────────────────────────────────────────────────────────────────
# portfolio (paper accounting; in live mode it shadows your real trades
# so the dashboard and stats work the same way)
# ────────────────────────────────────────────────────────────────────
class Portfolio:
    def __init__(self, capital: float):
        self.starting_capital = capital
        self.cash = capital
        # coin → {size(+long/−short), entry_px, margin, entry_time, whale_hash}
        self.positions: dict[str, dict] = {}
        self.closed: list[dict] = []
        self.load()

    # equity = cash + margin locked in positions + unrealized pnl
    def equity(self, marks: dict[str, float]) -> float:
        total = self.cash
        for coin, p in self.positions.items():
            total += p["margin"] + self.unrealized(coin, p, marks)
        return total

    @staticmethod
    def unrealized(coin: str, p: dict, marks: dict[str, float]) -> float:
        mark = marks.get(coin, p["entry_px"])
        return p["size"] * (mark - p["entry_px"])  # sign handles shorts

    def open(self, coin: str, direction: int, notional: float, px: float,
             whale_hash: str) -> dict:
        size = direction * notional / px
        p = self.positions.get(coin)
        if p and (p["size"] > 0) == (size > 0):        # add to same side
            total_margin = p["margin"] + notional
            new_size = p["size"] + size
            p["entry_px"] = ((abs(p["size"]) * p["entry_px"] +
                              abs(size) * px) / abs(new_size))
            p["size"], p["margin"] = new_size, total_margin
        else:
            p = {"size": size, "entry_px": px, "margin": notional,
                 "entry_time": time.time(), "whale_hash": whale_hash}
            self.positions[coin] = p
        self.cash -= notional
        self.save()
        return p

    def close(self, coin: str, fraction: float, px: float,
              whale_hash: str) -> dict | None:
        p = self.positions.get(coin)
        if not p:
            return None
        fraction = max(0.0, min(1.0, fraction))
        close_size = p["size"] * fraction
        margin_back = p["margin"] * fraction
        pnl = close_size * (px - p["entry_px"])
        trade = {
            "coin": coin,
            "side": "LONG" if p["size"] > 0 else "SHORT",
            "size": abs(close_size),
            "entry_px": p["entry_px"], "exit_px": px,
            "entry_time": p["entry_time"], "exit_time": time.time(),
            "cost": margin_back, "pnl": pnl,
            "pnl_pct": pnl / margin_back * 100 if margin_back else 0.0,
            "win": pnl > 0,
            "whale_entry": p["whale_hash"], "whale_exit": whale_hash,
        }
        self.closed.append(trade)
        self.cash += margin_back + pnl
        p["size"] -= close_size
        p["margin"] -= margin_back
        if abs(p["size"]) * px < 1.0:                  # dust → fully closed
            del self.positions[coin]
        self.save()
        return trade

    def stats(self, marks: dict[str, float]) -> dict:
        wins = [t for t in self.closed if t["win"]]
        losses = [t for t in self.closed if not t["win"]]
        eq = self.equity(marks)
        return {
            "trades": len(self.closed), "wins": len(wins),
            "losses": len(losses),
            "win_rate": 100 * len(wins) / len(self.closed) if self.closed else 0,
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
            "realized": sum(t["pnl"] for t in self.closed),
            "unrealized": sum(self.unrealized(c, p, marks)
                              for c, p in self.positions.items()),
            "cash": self.cash, "equity": eq,
            "pnl_pct": 100 * (eq - self.starting_capital) / self.starting_capital,
        }

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps({
            "starting_capital": self.starting_capital, "cash": self.cash,
            "positions": self.positions, "closed": self.closed}, indent=2))

    def load(self) -> None:
        if STATE_FILE.exists():
            try:
                s = json.loads(STATE_FILE.read_text())
                self.starting_capital = s["starting_capital"]
                self.cash = s["cash"]
                self.positions = s["positions"]
                self.closed = s["closed"]
                console.print(f"[dim]restored: {len(self.positions)} open, "
                              f"{len(self.closed)} closed[/dim]")
            except Exception:
                console.print("[yellow]could not read old state; "
                              "starting fresh[/yellow]")


# ────────────────────────────────────────────────────────────────────
# live execution (only used when mode == "live")
# ────────────────────────────────────────────────────────────────────
class LiveTrader:
    def __init__(self, info: Info):
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        key = os.environ.get(CFG.get("private_key_env", "HL_PRIVATE_KEY"))
        if not key:
            sys.exit("mode=live but the private key environment variable "
                     "is not set. See README step 6.")
        self.exchange = Exchange(Account.from_key(key), API_URL,
                                 account_address=CFG.get("my_address") or None)
        # size rounding rules per coin
        self.sz_decimals = {a["name"]: a["szDecimals"]
                            for a in info.meta()["universe"]}

    def round_size(self, coin: str, sz: float) -> float:
        return round(sz, self.sz_decimals.get(coin, 4))

    def open(self, coin: str, is_buy: bool, sz: float) -> bool:
        sz = self.round_size(coin, sz)
        if sz <= 0:
            return False
        r = self.exchange.market_open(coin, is_buy, sz, None, 0.01)
        return _order_ok(r)

    def close(self, coin: str, sz: float | None) -> bool:
        if sz is not None:
            sz = self.round_size(coin, sz)
        r = self.exchange.market_close(coin, sz)
        return _order_ok(r)


def _order_ok(resp) -> bool:
    try:
        statuses = resp["response"]["data"]["statuses"]
        return not any("error" in s for s in statuses)
    except Exception:
        return False


# ────────────────────────────────────────────────────────────────────
# the bot
# ────────────────────────────────────────────────────────────────────
class Bot:
    def __init__(self):
        console.print(f"[cyan]connecting to Hyperliquid "
                      f"({CFG.get('network', 'mainnet')})…[/cyan]")
        self.info = Info(API_URL)
        self.portfolio = Portfolio(float(CFG["my_capital_usd"]))
        self.live = LiveTrader(self.info) if MODE == "live" else None
        self.marks: dict[str, float] = {}
        self.whale_value: float = 0.0
        self.paused = False
        self.last_event = "waiting for whale activity…"
        self.refresh_whale_value()

    # ── whale account value (denominator for proportional sizing) ───
    def refresh_whale_value(self) -> None:
        try:
            state = self.info.user_state(WHALE)
            self.whale_value = float(state["marginSummary"]["accountValue"])
        except Exception as exc:
            console.print(f"[yellow]could not read whale account: {exc}[/yellow]")

    # ── handle one batch of whale fills ──────────────────────────────
    def on_message(self, msg: dict) -> None:
        try:
            data = msg.get("data", {})
            if data.get("isSnapshot"):        # history sent on connect — skip
                return
            fills = data.get("fills", [])
            # one order can arrive as many small fills; merge per coin+dir
            merged: dict[tuple, dict] = {}
            for f in fills:
                if "/" in f["coin"] or f["coin"].startswith("@"):
                    continue                   # spot fill — perps only
                key = (f["coin"], f["dir"])
                m = merged.setdefault(key, {"sz": 0.0, "notional": 0.0,
                                            "f": f})
                m["sz"] += float(f["sz"])
                m["notional"] += float(f["sz"]) * float(f["px"])
            for (coin, direction), m in merged.items():
                self.handle_trade(coin, direction, m["sz"],
                                  m["notional"] / m["sz"], m["f"])
        except Exception as exc:
            audit("error", where="on_message", error=str(exc))
            self.last_event = f"error: {exc}"

    def handle_trade(self, coin: str, dir_str: str, sz: float, px: float,
                     fill: dict) -> None:
        age = time.time() - fill["time"] / 1000
        if age > MAX_AGE:
            self._skip(coin, dir_str, f"stale ({age:.1f}s old)")
            return

        with LOCK:
            equity = self.portfolio.equity(self.marks)

            # pause new entries when balance is too low; exits still allowed
            if equity < MIN_BALANCE:
                self.paused = True
                audit("alert", kind="below_min_balance", equity=equity)
            else:
                self.paused = False

            opening = "Open" in dir_str or dir_str in ("Buy", "Sell")
            flip = ">" in dir_str            # e.g. "Long > Short"

            if flip:
                self._do_close(coin, 1.0, px, fill)     # get flat first
                # remainder of the whale's fill opened the opposite side
                start = abs(float(fill["startPosition"]))
                open_sz = max(sz - start, 0.0)
                if open_sz > 0:
                    direction = 1 if "> Long" in dir_str else -1
                    self._do_open(coin, direction, open_sz * px, px,
                                  fill, equity)
            elif opening:
                if self.paused:
                    self._skip(coin, dir_str,
                               f"PAUSED — equity ${equity:.2f} below "
                               f"${MIN_BALANCE:.0f} minimum")
                    return
                direction = 1 if "Long" in dir_str or dir_str == "Buy" else -1
                self._do_open(coin, direction, sz * px, px, fill, equity)
            else:                              # closing
                start = abs(float(fill["startPosition"]))
                fraction = min(sz / start, 1.0) if start else 1.0
                self._do_close(coin, fraction, px, fill)

    # ── sizing: mirror the whale's fraction, never more ─────────────
    def _do_open(self, coin: str, direction: int, whale_notional: float,
                 px: float, fill: dict, equity: float) -> None:
        if self.whale_value <= 0:
            self.refresh_whale_value()
        if self.whale_value <= 0:
            self._skip(coin, "open", "whale account value unknown")
            return

        fraction = whale_notional / self.whale_value
        fraction = min(fraction, MAX_FRACTION)          # hard cap
        notional = fraction * equity
        notional = min(notional, self.portfolio.cash)   # never exceed balance
        if notional < 10:                               # HL $10 order minimum
            self._skip(coin, "open", f"size ${notional:.2f} below $10 minimum")
            return

        if self.live:
            ok = self.live.open(coin, direction > 0, notional / px)
            if not ok:
                self._skip(coin, "open", "live order rejected")
                return

        self.portfolio.open(coin, direction, notional, px, fill["hash"])
        side = "LONG" if direction > 0 else "SHORT"
        latency = time.time() - fill["time"] / 1000
        self.last_event = (f"OPEN {side} {coin} ${notional:,.2f} @ {px:,.6g} "
                           f"({fraction*100:.2f}% | {latency:.2f}s after whale)")
        audit("entry", coin=coin, side=side, notional=round(notional, 2),
              px=px, fraction=round(fraction, 5), whale_tx=fill["hash"],
              signal_to_fill_s=round(latency, 3))

    def _do_close(self, coin: str, fraction: float, px: float,
                  fill: dict) -> None:
        pos = self.portfolio.positions.get(coin)
        if not pos:
            return                            # we never copied this position
        if self.live:
            sz = abs(pos["size"]) * fraction
            ok = self.live.close(coin, None if fraction >= 0.999 else sz)
            if not ok:
                self._skip(coin, "close", "live order rejected")
                return
        trade = self.portfolio.close(coin, fraction, px, fill["hash"])
        if trade:
            latency = time.time() - fill["time"] / 1000
            self.last_event = (f"CLOSE {trade['side']} {coin} "
                               f"P&L ${trade['pnl']:+,.2f} "
                               f"({trade['pnl_pct']:+.2f}%)")
            audit("exit", coin=coin, pnl=round(trade["pnl"], 2),
                  pnl_pct=round(trade["pnl_pct"], 3), win=trade["win"],
                  whale_tx=fill["hash"], signal_to_fill_s=round(latency, 3))

    def _skip(self, coin: str, action: str, reason: str) -> None:
        self.last_event = f"{action} {coin} skipped: {reason}"
        audit("skip", coin=coin, action=action, reason=reason)

    # ── background refresh: prices + whale value ─────────────────────
    def refresh_loop(self) -> None:
        while True:
            try:
                mids = self.info.all_mids()
                with LOCK:
                    for coin in list(self.portfolio.positions):
                        if coin in mids:
                            self.marks[coin] = float(mids[coin])
                self.refresh_whale_value()
            except Exception:
                pass
            time.sleep(20)

    # ── dashboard ────────────────────────────────────────────────────
    def render(self):
        with LOCK:
            s = self.portfolio.stats(self.marks)
            head = Text()
            head.append(f" {MODE.upper()} ",
                        style="yellow" if MODE == "paper" else "bold red")
            head.append(f" whale {WHALE[:8]}…{WHALE[-4:]} "
                        f"(acct ${self.whale_value:,.0f})  ")
            head.append(f"my equity ${s['equity']:,.2f}  ")
            total = s["realized"] + s["unrealized"]
            head.append(f"${total:+,.2f} ({s['pnl_pct']:+.2f}%)",
                        style="green" if total >= 0 else "red")
            if self.paused:
                head.append("  ⏸ PAUSED — balance below minimum",
                            style="bold red")
            head.append(f"\n last: {self.last_event}", style="dim")

            pos_t = Table(title=f"Open Positions "
                                f"({len(self.portfolio.positions)})",
                          expand=True)
            for c in ("Coin", "Side", "Size", "Entry", "Mark", "Margin",
                      "Unrealized"):
                pos_t.add_column(c, justify="right")
            for coin, p in self.portfolio.positions.items():
                mark = self.marks.get(coin, p["entry_px"])
                u = self.portfolio.unrealized(coin, p, self.marks)
                upct = u / p["margin"] * 100 if p["margin"] else 0
                pos_t.add_row(
                    coin, "LONG" if p["size"] > 0 else "SHORT",
                    f"{abs(p['size']):,.6g}", f"{p['entry_px']:,.6g}",
                    f"{mark:,.6g}", f"${p['margin']:,.2f}",
                    Text(f"${u:+,.2f} ({upct:+.2f}%)",
                         style="green" if u >= 0 else "red"))
            if not self.portfolio.positions:
                pos_t.add_row(*["—"] * 7)

            tr_t = Table(title="Recent Closed Trades (last 15)", expand=True)
            for c in ("Coin", "Side", "Entry", "Exit", "P&L", "Closed", "W/L"):
                tr_t.add_column(c, justify="right")
            for t in reversed(self.portfolio.closed[-15:]):
                tr_t.add_row(
                    t["coin"], t["side"], f"{t['entry_px']:,.6g}",
                    f"{t['exit_px']:,.6g}",
                    Text(f"${t['pnl']:+,.2f} ({t['pnl_pct']:+.2f}%)",
                         style="green" if t["win"] else "red"),
                    datetime.fromtimestamp(t["exit_time"])
                    .strftime("%m-%d %H:%M:%S"),
                    Text("W", style="green") if t["win"]
                    else Text("L", style="red"))
            if not self.portfolio.closed:
                tr_t.add_row(*["—"] * 7)

            st = Text()
            st.append(f"trades {s['trades']}  wins {s['wins']}  "
                      f"losses {s['losses']}  win rate {s['win_rate']:.1f}%\n")
            st.append(f"avg win ${s['avg_win']:+,.2f}   "
                      f"avg loss ${s['avg_loss']:+,.2f}   ")
            st.append(f"realized ${s['realized']:+,.2f}   "
                      f"unrealized ${s['unrealized']:+,.2f}   "
                      f"cash ${s['cash']:,.2f}")

            return Group(Panel(head, title="Hyperliquid Whale Mirror",
                               border_style="cyan"),
                         pos_t, tr_t,
                         Panel(st, title="Cumulative Stats",
                               border_style="magenta"))

    def run(self) -> None:
        audit("startup", mode=MODE, whale=WHALE,
              capital=CFG["my_capital_usd"])
        self.info.subscribe({"type": "userFills", "user": WHALE},
                            self.on_message)
        threading.Thread(target=self.refresh_loop, daemon=True).start()
        console.print("[green]watching whale — leave this window open. "
                      "Ctrl+C to stop.[/green]")
        with Live(self.render(), console=console, refresh_per_second=2) as lv:
            try:
                while True:
                    time.sleep(0.5)
                    lv.update(self.render())
            except KeyboardInterrupt:
                pass
        audit("shutdown")
        console.print("stopped. Your history is saved — just run "
                      "`python mirror.py` again to continue.")


if __name__ == "__main__":
    if MODE == "live":
        console.print("[bold red]⚠ LIVE MODE — this places real orders "
                      "with real money on your Hyperliquid account.[/bold red]")
        if input("Type 'I understand the risks' to continue: ").strip() \
                != "I understand the risks":
            sys.exit("aborted — set \"mode\": \"paper\" in config.json "
                     "to run safely.")
    Bot().run()
