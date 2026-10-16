"""Daily self-review: numbers first, then rule-based observations, then a slot for Claude's notes."""
from __future__ import annotations

import pandas as pd

from . import config, data, strategy
from .portfolio import Portfolio


REVIEW_HEADING = "## Claude's review"
REVIEW_PLACEHOLDER = ("_To be filled by Claude after reading the numbers above: what went right, what went wrong, "
                      "whether any rule should change, and what to watch tomorrow. Rule changes are logged in CHANGELOG.md, "
                      "never applied silently._")


def _fmt(x: float) -> str:
    return f"{x:,.0f}"


def _existing_review(path) -> str | None:
    """Return the written review from an existing journal file, or None if absent or still the placeholder.
    A same-date re-run (holiday, late Yahoo bar, scheduled run after a manual one) must not erase it."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    marker = "\n" + REVIEW_HEADING + "\n"
    if marker not in text:
        return None
    body = text.split(marker, 1)[1].strip()
    if not body or body == REVIEW_PLACEHOLDER:
        return None
    return body


def performance(pf: Portfolio, closes: pd.DataFrame) -> dict:
    curve = pf.equity_curve()
    eq = curve["equity"].iloc[-1] if len(curve) else pf.starting_capital
    days = len(curve)
    total_pnl = eq - pf.starting_capital
    peak = curve["equity"].cummax() if len(curve) else pd.Series([eq])
    dd = ((curve["equity"] - peak) / peak).min() if len(curve) else 0.0
    day_pnl = curve["equity"].iloc[-1] - curve["equity"].iloc[-2] if len(curve) > 1 else 0.0

    trades = pf.trades()
    closed = trades[trades["side"] == "SELL"].copy()
    closed["realised_pnl"] = pd.to_numeric(closed["realised_pnl"], errors="coerce")
    wins = (closed["realised_pnl"] > 0).sum()
    n_closed = len(closed)

    return dict(
        equity=eq, days=days, total_pnl=total_pnl, day_pnl=day_pnl,
        avg_per_day=total_pnl / days if days else 0.0,
        max_drawdown=dd, n_trades=len(trades), n_closed=n_closed,
        win_rate=(wins / n_closed) if n_closed else None,
        avg_win=closed.loc[closed["realised_pnl"] > 0, "realised_pnl"].mean() if wins else None,
        avg_loss=closed.loc[closed["realised_pnl"] <= 0, "realised_pnl"].mean() if n_closed - wins else None,
        costs_paid=pf.costs_paid,
        cash=pf.cash, invested=pf.market_value(closes.ffill().iloc[-1].dropna().to_dict()),
    )


def observations(perf: dict, pf: Portfolio, fills: list[dict], proposals=(), closes=None) -> list[str]:
    obs = []
    if closes is not None:
        stale = [s for s in pf.positions if s not in closes.columns or pd.isna(closes[s].iloc[-1])]
        if stale:
            obs.append(f"Held with NO close today (valued at last known close, no automatic exit): {', '.join(stale)}. "
                       f"Check for suspension, delisting or a symbol change.")
    neg = [o['symbol'] for o in proposals if o.get('side') == 'BUY' and f"{config.LOOKBACK_MOMENTUM}d return -" in o.get('reason', '')]
    if neg:
        obs.append(f"{len(neg)} proposed buy(s) have a NEGATIVE {config.LOOKBACK_MOMENTUM}d return ({', '.join(neg)}). "
                   f"The rule ranks only trend-passing names and does not require positive momentum.")
    if perf["days"] < 20:
        obs.append(f"Only {perf['days']} trading day(s) recorded. Nothing here is statistically meaningful yet; "
                   f"the first honest read is at ~60 days.")
    gap = config.TARGET_GROSS_PER_DAY - perf["avg_per_day"]
    if perf["days"] >= 1:
        obs.append(f"Average P&L per trading day is Rs {perf['avg_per_day']:,.0f} against a pre-tax target of "
                   f"Rs {config.TARGET_GROSS_PER_DAY:,.0f} (gap Rs {gap:,.0f}/day).")
    if perf["total_pnl"] != 0 and perf["costs_paid"] > 0.25 * abs(perf["total_pnl"]):
        obs.append(f"Charges paid (Rs {perf['costs_paid']:,.0f}) are more than 25% of absolute P&L. "
                   f"Turnover is eating the edge.")
    if perf["max_drawdown"] < -0.05:
        obs.append(f"Max drawdown {perf['max_drawdown']*100:.1f}%. Check whether stops are doing their job.")
    if perf["win_rate"] is not None and perf["n_closed"] >= 10 and perf["win_rate"] < 0.4:
        obs.append(f"Win rate {perf['win_rate']*100:.0f}% over {perf['n_closed']} closed trades. Momentum systems "
                   f"usually win <50% of the time; what matters is avg win vs avg loss.")
    if len(pf.positions) == 0:
        obs.append("Portfolio is fully in cash. Either no names pass the trend filter (defensive, fine) or the "
                   "entry rule is too strict.")
    if fills:
        obs.append(f"{len(fills)} fill(s) today: " + "; ".join(f"{f['side']} {f['qty']} {f['symbol']} @ {f['price']}" for f in fills))
    required_annual = config.TARGET_GROSS_PER_DAY * 250 / pf.starting_capital
    obs.append(f"Reality check: Rs {config.TARGET_GROSS_PER_DAY:,.0f}/day pre-tax on Rs {pf.starting_capital:,.0f} "
               f"capital needs ~{required_annual*100:.0f}% per year. Nifty's long-run average is ~12-14%.")
    return obs


def write_daily_review(pf: Portfolio, prices: pd.DataFrame, fills: list[dict], proposals: list[dict], dropped=None,
                       extra_sections: list[str] | None = None):
    closes = data.pivot(prices, "close")
    day = closes.index[-1].strftime("%Y-%m-%d")
    perf = performance(pf, closes)
    tbl = strategy.indicators(closes)
    last = closes.ffill().iloc[-1]

    lines = [f"# Daily review - {day}", ""]
    lines += ["## Scorecard", "",
              "| Metric | Value |", "|---|---|",
              f"| Equity | Rs {_fmt(perf['equity'])} |",
              f"| Day P&L | Rs {_fmt(perf['day_pnl'])} |",
              f"| Total P&L since start | Rs {_fmt(perf['total_pnl'])} |",
              f"| Trading days recorded | {perf['days']} |",
              f"| Avg P&L per day | Rs {_fmt(perf['avg_per_day'])} (target pre-tax Rs {_fmt(config.TARGET_GROSS_PER_DAY)}, net Rs {_fmt(config.TARGET_NET_PER_DAY)}) |",
              f"| Max drawdown | {perf['max_drawdown']*100:.2f}% |",
              f"| Charges paid to date | Rs {_fmt(perf['costs_paid'])} |",
              f"| Closed trades / win rate | {perf['n_closed']} / {('%.0f%%' % (perf['win_rate']*100)) if perf['win_rate'] is not None else 'n/a'} |",
              f"| Cash / invested | Rs {_fmt(perf['cash'])} / Rs {_fmt(perf['invested'])} |",
              ""]

    lines += ["## Open positions", ""]
    if pf.positions:
        lines += ["| Symbol | Qty | Avg cost | Close | Unrealised | Stop | Entry |", "|---|---|---|---|---|---|---|"]
        for s, p in pf.positions.items():
            c = float(last.get(s, float("nan")))
            unreal = (c - p.avg_cost) * p.qty
            lines.append(f"| {s} | {p.qty} | {p.avg_cost:,.2f} | {c:,.2f} | Rs {_fmt(unreal)} ({(c/p.avg_cost-1)*100:+.1f}%) | {p.stop:,.2f} | {p.entry_date} |")
    else:
        lines.append("None (all cash).")
    lines.append("")

    lines += ["## Fills today", ""]
    if fills:
        lines += ["| Side | Symbol | Qty | Price | Charges | Realised | Reason |", "|---|---|---|---|---|---|---|"]
        for f in fills:
            lines.append(f"| {f['side']} | {f['symbol']} | {f['qty']} | {f['price']:,.2f} | {f['charges']:,.2f} | {f['realised_pnl'] if f['realised_pnl'] != '' else '-'} | {f['reason']} |")
    else:
        lines.append("No fills.")
    lines.append("")

    lines += [f"## Orders proposed for next session (fill at next open)", ""]
    if proposals:
        lines += ["| Side | Symbol | Qty | Reason |", "|---|---|---|---|"]
        for o in proposals:
            lines.append(f"| {o['side']} | {o['symbol']} | {o['qty'] if o['qty'] else 'auto'} | {o['reason']} |")
    else:
        lines.append("No orders. Hold.")
    lines.append("")

    lines += ["## Market snapshot (top 10 by momentum, trend-filtered)", "",
              "| Rank | Symbol | Close | 63d return | SMA20 | SMA50 | Held |", "|---|---|---|---|---|---|---|"]
    top = tbl[tbl["eligible"]].head(10)
    for s, r in top.iterrows():
        lines.append(f"| {int(r['rank'])} | {s} | {r['close']:,.2f} | {r['momentum']*100:+.1f}% | {r['sma_fast']:,.0f} | {r['sma_slow']:,.0f} | {'yes' if s in pf.positions else ''} |")
    lines.append(f"\nEligible names passing trend filter: {int(tbl['eligible'].sum())} of {len(tbl)}.")
    if dropped:
        lines.append(f"Symbols with no data (dropped): {', '.join(dropped)}.")
    lines.append("")

    lines += ["## Automatic observations", ""]
    lines += [f"- {o}" for o in observations(perf, pf, fills, proposals, closes)]
    if extra_sections:   # the thesis sleeve; must stay ABOVE the review heading, which _existing_review splits on
        lines += [""] + extra_sections
    config.JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = config.JOURNAL_DIR / f"{day}.md"
    lines += ["", REVIEW_HEADING, "", _existing_review(path) or REVIEW_PLACEHOLDER, ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
