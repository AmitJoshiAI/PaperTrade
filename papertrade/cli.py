"""Command line: python -m papertrade <command>

  run            full daily cycle (update -> execute pending -> mark -> propose -> review)
  run --offline  same, but use cached prices (no download)
  status         print portfolio and last equity row
  pending        print the pending orders file
  thesis         print the thesis sleeve: cash, holdings, theses, queued thesis orders
  fno status     print the India F&O sleeve: equity, margin, positions, circuit breaker, queued orders
  us status      print the US sleeve (same, in USD with INR at the day's USD/INR)
  costs QTY PX   show Zerodha charges for a buy+sell of QTY shares at PX (delivery; --product MIS|FUT|OPT)
  export         write docs/data.json for the dashboard (read-only; no download, no state change)
  reset --yes    wipe state/, decisions/, journal/ and start again (both sleeves; theses/ is kept)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys

from . import config


def cmd_run(args):
    from .engine import run_day
    out = run_day(do_update=not args.offline)
    print(f"Date: {out['date']}")
    print(f"Equity: Rs {out['equity']['equity']:,.2f}  cash Rs {out['equity']['cash']:,.2f}  positions {out['equity']['positions']}")
    print(f"Fills today: {len(out['fills'])}")
    for f in out["fills"]:
        print(f"  {f['side']} {f['qty']} {f['symbol']} @ {f['price']}  charges {f['charges']}  {f['reason']}")
    print(f"Proposed for next open: {len(out['proposals'])}")
    for o in out["proposals"]:
        print(f"  {o['side']} {o['symbol']} qty={o['qty'] or 'auto'}  {o['reason']}")
    print(f"Review written: {out['report']}")
    print(f"Dashboard data: {out['dashboard'] or 'EXPORT FAILED (see error above)'}")
    t = out.get("thesis")
    if t is None:
        print("Thesis sleeve: STEP FAILED (see error above); momentum results above are saved.")
    else:
        e = t["equity"]
        print(f"Thesis sleeve: equity Rs {e['equity']:,.2f}  cash Rs {e['cash']:,.2f}  positions {e['positions']}  "
              f"theses {len(t['theses'])}  queued orders {len(t['orders'])}")
        for f in t["fills"]:
            print(f"  {f['side']} {f['qty']} {f['symbol']} @ {f['price']}  charges {f['charges']}  {f['reason']}")
        for m in t["hits"] + t["notes"] + t["warnings"]:
            print(f"  {m}")
    for key, name, ccy in (("fno", "India F&O sleeve", "Rs"), ("us", "US sleeve", "$")):
        s = out.get(key)
        if s is None:
            print(f"{name}: STEP FAILED (see error above); the other sleeves are saved.")
            continue
        e = s["equity"]
        inr = f" (Rs {e['equity_inr']:,.0f})" if key == "us" else ""
        print(f"{name} ({s['date']}): equity {ccy} {e['equity']:,.2f}{inr}  day {ccy} {e['day_pnl']:,.2f}  cash {ccy} {e['cash']:,.2f}  "
              f"margin {ccy} {e['margin']:,.2f}  positions {e['positions']}  queued orders {len(s['orders'])}  "
              f"breaker {'ON' if s['book'].breaker else 'off'}")
        for f in s["fills"]:
            print(f"  {f['date']} {f['action']} {f['side']} {f['lots']} {f['instrument']} @ {f['price']}  charges {f['charges']}  {f['pricing']}")
        for m in s["notes"] + s["warnings"]:
            print(f"  {m}")


def cmd_export(args):
    from .export import write_dashboard_data
    import json as _json
    path = write_dashboard_data()
    d = _json.loads(path.read_text(encoding="utf-8"))
    pf = d["portfolio"]
    print(f"As of: {d['as_of']}  (prices to {d['price_date']})")
    print(f"Equity: Rs {d['performance']['equity']:,.2f}  cash Rs {pf['cash']:,.2f}  positions {len(pf['positions'])}")
    print(f"Equity rows: {len(d['equity_curve'])}  trades: {len(d['trades'])}  "
          f"pending orders: {len(d['pending']['orders']) if d['pending'] else 0}  "
          f"journal: {d['journal']['file'] if d['journal'] else 'none'}")
    print(f"Dropped symbols: {d['dropped_symbols']}")
    print(f"Wrote {path}")


def cmd_status(args):
    from .portfolio import Portfolio
    pf = Portfolio.load()
    print(json.dumps({
        "cash": round(pf.cash, 2), "positions": {k: vars(v) for k, v in pf.positions.items()},
        "start_date": pf.start_date, "costs_paid": round(pf.costs_paid, 2),
        "realised_pnl": round(pf.realised_pnl, 2),
    }, indent=2))
    curve = pf.equity_curve()
    if len(curve):
        print("\nLast equity rows:")
        print(curve.tail(5).to_string(index=False))


def cmd_thesis(args):
    from .thesis import ThesisPortfolio, load_theses, load_orders, display_symbol, PAPER_SHORT
    pf = ThesisPortfolio.load()
    theses, warnings = load_theses()
    print(f"Thesis sleeve: cash Rs {pf.cash:,.2f} of Rs {pf.starting_capital:,.0f}  costs Rs {pf.costs_paid:,.2f}  "
          f"realised Rs {pf.realised_pnl:,.2f}")
    for s, p in pf.positions.items():
        short = f"  SHORT ({PAPER_SHORT})" if p.qty < 0 else ""
        ccy = "" if p.currency == "INR" else f" {p.currency}"
        print(f"  held {display_symbol(s)}: {p.qty} @ avg {p.avg_cost:,.2f}{ccy} since {p.entry_date}{short}")
    for t in theses.values():
        ev = f"  [{t.get('event')} {t.event_date}]" if t.type in ("catalyst", "second_order") else ""
        print(f"  [{t.status}] [{t.type}] {t.slug}: {t.label} - {t.get('thesis')}{ev}")
    for w in warnings:
        print(f"  WARNING {w}")
    print(json.dumps(load_orders(), indent=2))


def cmd_deriv(args):
    if args.what != "status":
        print("usage: python -m papertrade fno status | us status")
        sys.exit(1)
    from .derivbook import load_queue
    from .fno import position_rows
    if args.cmd == "fno":
        from .fno import FnoBook as Book
        qf, ccy, name = config.FNO_ORDERS_FILE, "Rs", "India F&O"
    else:
        from .us import UsBook as Book
        qf, ccy, name = config.US_ORDERS_FILE, "$", "US"
    b = Book.load()
    curve = b.equity_curve()
    day = str(curve["date"].iloc[-1]) if len(curve) else (b.start_date or "")
    eq = b.equity()
    inr = f" (Rs {eq * b.fx():,.0f} at USD/INR {b.fx():.4f}, {getattr(b, 'fx_note', '')})" if b.CURRENCY != "INR" else ""
    print(f"{name} sleeve as of {day or 'no run yet'}: equity {ccy} {eq:,.2f}{inr} of {ccy} {b.starting_capital:,.2f}  "
          f"cash {ccy} {b.cash:,.2f}  margin used {ccy} {b.margin_used():,.2f}")
    print(f"  charges {ccy} {b.costs_paid:,.2f}  realised {ccy} {b.realised_pnl:,.2f}  "
          f"open positions {len(b.positions)} of {config.DERIV_MAX_POSITIONS}  circuit breaker: {b.breaker_text()}")
    for r in position_rows(b, day or "2000-01-01"):
        dte = f"  {r['days_to_expiry']}d to expiry" if r["days_to_expiry"] is not None else ""
        print(f"  {r['id']}: {r['side']} {r['lots']} lot(s) ({r['units']} units)  entry {r['entry']:,.2f}  mark "
              f"{(r['mark'] or 0):,.2f}  P&L {ccy} {r['pnl']:,.2f}  margin {ccy} {r['margin']:,.2f}{dte}")
        print(f"      priced by: {r['pricing']}")
    if len(curve):
        print("\nLast equity rows:")
        print(curve.tail(5).to_string(index=False))
    print("\nQueued orders:")
    print(json.dumps(load_queue(qf), indent=2))


def cmd_pending(args):
    if config.PENDING_ORDERS_FILE.exists():
        print(config.PENDING_ORDERS_FILE.read_text())
    else:
        print("No pending orders.")


def cmd_costs(args):
    from .costs import charges
    b = charges("BUY", args.qty, args.price, args.product)
    s = charges("SELL", args.qty, args.price, args.product)
    print(f"{args.product} {args.qty} x Rs {args.price} (turnover Rs {args.qty*args.price:,.2f} each side)")
    print("BUY :", b.as_dict())
    print("SELL:", s.as_dict())
    rt = b.total + s.total
    print(f"Round trip: Rs {rt:.2f}  = {rt/(args.qty*args.price)*100:.3f}% of one-side turnover")
    print(f"Break-even move needed: {rt/(args.qty*args.price)*100:.3f}%")


def cmd_reset(args):
    if not args.yes:
        print("Refusing to reset without --yes")
        sys.exit(1)
    for d in (config.STATE_DIR, config.DECISIONS_DIR, config.JOURNAL_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    print("State, decisions and journal cleared.")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="papertrade", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run"); r.add_argument("--offline", action="store_true"); r.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("pending").set_defaults(fn=cmd_pending)
    sub.add_parser("thesis").set_defaults(fn=cmd_thesis)
    for name in ("fno", "us"):
        d = sub.add_parser(name); d.add_argument("what", nargs="?", default="status"); d.set_defaults(fn=cmd_deriv)
    c = sub.add_parser("costs"); c.add_argument("qty", type=int); c.add_argument("price", type=float)
    c.add_argument("--product", default="CNC", choices=["CNC", "MIS", "FUT", "OPT"]); c.set_defaults(fn=cmd_costs)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    rs = sub.add_parser("reset"); rs.add_argument("--yes", action="store_true"); rs.set_defaults(fn=cmd_reset)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
