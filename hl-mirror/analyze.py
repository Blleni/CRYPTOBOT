"""
Hyperliquid Wallet Analyzer
===========================
Pulls a wallet's public record from Hyperliquid's API and prints a
copy-trading report card: PnL history, drawdown, win rate, trade
frequency, coins, leverage, and consistency.

Usage:
    python analyze.py 0x888e000c78b8f1aada5b3c99f880794907b76d77

No account or key needed — this is all public on-chain data.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"


def post(payload: dict):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def max_drawdown(curve: list[float]) -> float:
    """Worst peak-to-trough drop, as % of the peak."""
    peak, worst = float("-inf"), 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v - peak) / peak)
    return worst * 100


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python analyze.py <wallet_address>")
    addr = sys.argv[1].lower()
    print(f"\n═══ Hyperliquid wallet analysis: {addr} ═══\n")

    # ── current account state ────────────────────────────────────────
    state = post({"type": "clearinghouseState", "user": addr})
    acct_value = float(state["marginSummary"]["accountValue"])
    total_ntl = float(state["marginSummary"]["totalNtlPos"])
    print(f"Account value:        {fmt_usd(acct_value)}")
    if acct_value > 0:
        print(f"Open notional:        {fmt_usd(total_ntl)} "
              f"(≈{total_ntl / acct_value:.1f}x effective leverage)")
    positions = state.get("assetPositions", [])
    if positions:
        print(f"\nOpen positions ({len(positions)}):")
        for ap in positions:
            p = ap["position"]
            szi = float(p["szi"])
            side = "LONG" if szi > 0 else "SHORT"
            lev = p.get("leverage", {}).get("value", "?")
            upnl = float(p.get("unrealizedPnl", 0))
            print(f"  {p['coin']:>8} {side:<5} size {abs(szi):,.4g} "
                  f"@ {p.get('entryPx', '?')} | {lev}x | "
                  f"uPnL {fmt_usd(upnl)}")
    else:
        print("Open positions:       none")

    # ── equity / pnl history (consistency + drawdown) ────────────────
    print("\n── History ──")
    portfolio = dict(post({"type": "portfolio", "user": addr}))
    for window in ("allTime", "month", "week"):
        w = portfolio.get(window) or portfolio.get("perp" + window.capitalize())
        if not w:
            continue
        pnl_hist = [(t, float(v)) for t, v in w.get("pnlHistory", [])]
        if not pnl_hist:
            continue
        pnl_now = pnl_hist[-1][1]
        label = {"allTime": "All-time PnL", "month": "30-day PnL",
                 "week": "7-day PnL"}[window]
        print(f"{label:<21} {fmt_usd(pnl_now)}")
        if window == "allTime":
            first_ts = pnl_hist[0][0] / 1000
            age_days = (datetime.now(timezone.utc).timestamp() - first_ts) / 86400
            acct_hist = [float(v) for _, v in w.get("accountValueHistory", [])]
            dd = max_drawdown(acct_hist) if acct_hist else 0.0
            print(f"{'Track record:':<21} {age_days:,.0f} days")
            print(f"{'Max drawdown:':<21} {dd:.1f}% (equity peak-to-trough; "
                  f"deposits/withdrawals can distort this)")
            # pnl per month = last cumulative value in that month
            #                 minus last cumulative value of the prior month
            by_month, last_of_month = {}, {}
            for ts, v in pnl_hist:
                key = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m")
                last_of_month[key] = v
            keys = sorted(last_of_month)
            prev_v = 0.0
            for k in keys:
                by_month[k] = last_of_month[k] - prev_v
                prev_v = last_of_month[k]
            green = sum(1 for v in by_month.values() if v > 0)
            print(f"{'Profitable months:':<21} {green}/{len(by_month)}")
            recent = list(by_month.items())[-6:]
            print("  last 6 months: " + "  ".join(
                f"{k[-2:]}/{k[2:4]} {v:+,.0f}" for k, v in recent))

    # ── recent fills: win rate, frequency, coins ─────────────────────
    print("\n── Recent trading (last ≤2000 fills) ──")
    fills = post({"type": "userFills", "user": addr})
    if not fills:
        print("No recent fills found.")
        return
    fills = [f for f in fills if "/" not in f["coin"]]  # perps only
    closes = [f for f in fills if "Close" in f.get("dir", "")
              or ">" in f.get("dir", "")]
    wins = [f for f in closes if float(f.get("closedPnl", 0)) > 0]
    losses = [f for f in closes if float(f.get("closedPnl", 0)) < 0]
    span_days = max((fills[0]["time"] - fills[-1]["time"]) / 86_400_000, 0.01)
    coins = defaultdict(float)
    for f in fills:
        coins[f["coin"]] += float(f["sz"]) * float(f["px"])
    top = sorted(coins.items(), key=lambda kv: -kv[1])[:6]

    win_pnl = sum(float(f["closedPnl"]) for f in wins)
    loss_pnl = sum(float(f["closedPnl"]) for f in losses)
    print(f"Fills analyzed:       {len(fills)} over {span_days:,.1f} days "
          f"({len(fills) / span_days:,.1f} fills/day)")
    if closes:
        wr = 100 * len(wins) / len(closes)
        print(f"Closing fills:        {len(closes)}  |  win rate {wr:.1f}% "
              f"(by fill, not round-trip)")
        if wins:
            print(f"Avg winning fill:     {fmt_usd(win_pnl / len(wins))}")
        if losses:
            print(f"Avg losing fill:      {fmt_usd(loss_pnl / len(losses))}")
        if loss_pnl != 0:
            pf = abs(win_pnl / loss_pnl)
            print(f"Profit factor:        {pf:.2f} "
                  f"(gross wins ÷ gross losses; >1.3 is solid)")
    print("Most-traded coins:    " + ", ".join(
        f"{c} ({v / sum(coins.values()) * 100:.0f}%)" for c, v in top))

    # ── checklist verdict hints ──────────────────────────────────────
    print("\n── Things to judge for yourself ──")
    print("• Equity curve shape: open the chart on hyperdash.info/trader/"
          f"{addr} — staircase up = skill, one spike = luck")
    print("• Fills/day much above ~20 is hard to copy (your fills lag)")
    print("• Concentration in illiquid coins = your slippage will be worse")
    print("• If effective leverage is high, expect deep drawdowns")
    print("• PnL history counts deposits out, but check for wallet "
          "switches — some traders rotate to fresh wallets after losses\n")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.exit(f"network error talking to Hyperliquid API: {e}")
    except KeyError as e:
        sys.exit(f"unexpected API response (missing {e}) — the address may "
                 "have no Hyperliquid history, or the API changed.")
