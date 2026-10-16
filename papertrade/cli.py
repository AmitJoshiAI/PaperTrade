"""Command line: python -m papertrade <command>

  run            full daily cycle (update -> execute pending -> mark -> propose -> review)
  run --offline  same, but use cached prices (no download)
  status         print portfolio and last equity row
  pending        print the pending orders file
  costs QTY PX   show Zerodha charges for a buy+sell of QTY shares at PX (delivery)
  export         write docs/data.json for the dashboard (read-only; no download, no state change)
  reset --yes    wipe state/, decisions/, journal/ and start again
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
    c = sub.add_parser("costs"); c.add_argument("qty", type=int); c.add_argument("price", type=float)
    c.add_argument("--product", default="CNC", choices=["CNC", "MIS"]); c.set_defaults(fn=cmd_costs)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    rs = sub.add_parser("reset"); rs.add_argument("--yes", action="store_true"); rs.set_defaults(fn=cmd_reset)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
