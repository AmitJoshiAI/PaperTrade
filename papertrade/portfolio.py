"""Portfolio state (JSON), trade ledger (CSV) and equity curve (CSV)."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from . import config
from .costs import charges

TRADE_COLUMNS = [
    "date", "symbol", "side", "qty", "price", "gross", "charges", "net_cash",
    "product", "reason", "realised_pnl",
]


@dataclass
class Position:
    symbol: str
    qty: int
    avg_cost: float           # per share, including buy-side charges
    entry_date: str
    stop: float
    highest_close: float


@dataclass
class Portfolio:
    cash: float
    positions: dict = field(default_factory=dict)   # symbol -> Position
    start_date: Optional[str] = None
    starting_capital: float = config.STARTING_CAPITAL
    costs_paid: float = 0.0
    realised_pnl: float = 0.0

    # ---------- persistence ----------
    @classmethod
    def load(cls) -> "Portfolio":
        if not config.PORTFOLIO_FILE.exists():
            return cls(cash=config.STARTING_CAPITAL)
        d = json.loads(config.PORTFOLIO_FILE.read_text())
        d["positions"] = {k: Position(**v) for k, v in d["positions"].items()}
        return cls(**d)

    def save(self) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        config.PORTFOLIO_FILE.write_text(json.dumps(asdict(self), indent=2))

    # ---------- valuation ----------
    def market_value(self, closes: dict) -> float:
        return sum(p.qty * closes.get(s, p.avg_cost) for s, p in self.positions.items())

    def equity(self, closes: dict) -> float:
        return self.cash + self.market_value(closes)

    # ---------- trading ----------
    def buy(self, day: str, symbol: str, qty: int, price: float, reason: str, product: str = config.PRODUCT) -> dict:
        c = charges("BUY", qty, price, product)
        gross = qty * price
        total = gross + c.total
        if total > self.cash + 1e-6:
            raise ValueError(f"insufficient cash for {symbol}: need {total:.2f}, have {self.cash:.2f}")
        self.cash -= total
        self.costs_paid += c.total
        if symbol in self.positions:
            p = self.positions[symbol]
            new_qty = p.qty + qty
            p.avg_cost = (p.avg_cost * p.qty + total) / new_qty
            p.qty = new_qty
            p.highest_close = max(p.highest_close, price)
        else:
            self.positions[symbol] = Position(
                symbol=symbol, qty=qty, avg_cost=total / qty, entry_date=day,
                stop=round(price * (1 - config.STOP_LOSS_PCT), 2), highest_close=price,
            )
        row = dict(date=day, symbol=symbol, side="BUY", qty=qty, price=round(price, 2), gross=round(gross, 2),
                   charges=c.total, net_cash=round(-total, 2), product=product, reason=reason, realised_pnl="")
        self._append_trade(row)
        return row

    def sell(self, day: str, symbol: str, qty: int, price: float, reason: str, product: str = config.PRODUCT) -> dict:
        p = self.positions.get(symbol)
        if p is None or qty > p.qty:
            raise ValueError(f"cannot sell {qty} {symbol}: not held")
        c = charges("SELL", qty, price, product)
        gross = qty * price
        net = gross - c.total
        self.cash += net
        self.costs_paid += c.total
        pnl = net - p.avg_cost * qty
        self.realised_pnl += pnl
        p.qty -= qty
        if p.qty == 0:
            del self.positions[symbol]
        row = dict(date=day, symbol=symbol, side="SELL", qty=qty, price=round(price, 2), gross=round(gross, 2),
                   charges=c.total, net_cash=round(net, 2), product=product, reason=reason, realised_pnl=round(pnl, 2))
        self._append_trade(row)
        return row

    # ---------- ledgers ----------
    @staticmethod
    def _append_trade(row: dict) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        new = not config.TRADES_FILE.exists()
        with open(config.TRADES_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
            if new:
                w.writeheader()
            w.writerow(row)

    @staticmethod
    def trades() -> pd.DataFrame:
        if not config.TRADES_FILE.exists():
            return pd.DataFrame(columns=TRADE_COLUMNS)
        return pd.read_csv(config.TRADES_FILE)

    def record_equity(self, day: str, closes: dict) -> dict:
        eq = self.equity(closes)
        row = dict(date=day, cash=round(self.cash, 2), market_value=round(self.market_value(closes), 2),
                   equity=round(eq, 2), positions=len(self.positions), costs_paid=round(self.costs_paid, 2))
        curve = self.equity_curve()
        curve = curve[curve["date"].astype(str) != str(day)]
        curve = pd.concat([curve, pd.DataFrame([row])], ignore_index=True)
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        curve.to_csv(config.EQUITY_FILE, index=False)
        return row

    @staticmethod
    def equity_curve() -> pd.DataFrame:
        cols = ["date", "cash", "market_value", "equity", "positions", "costs_paid"]
        if not config.EQUITY_FILE.exists():
            return pd.DataFrame(columns=cols)
        return pd.read_csv(config.EQUITY_FILE)
