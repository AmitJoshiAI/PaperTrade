"""Zerodha equity charge model (NSE). Rates as published on zerodha.com/charges, checked 2026-10-17.
Re-verify after any budget or SEBI/exchange circular; these move.

Delivery (CNC):  brokerage 0; STT 0.1% buy + 0.1% sell; NSE txn 0.00297%; SEBI 0.0001%;
                 stamp duty 0.015% buy; GST 18% on (brokerage + txn + SEBI); DP charge on each sell.
Intraday (MIS):  brokerage min(0.03%, Rs 20) per order; STT 0.025% sell only; NSE txn 0.00297%;
                 SEBI 0.0001%; stamp 0.003% buy; GST 18% on (brokerage + txn + SEBI); no DP charge.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict

STT_DELIVERY = 0.001
STT_INTRADAY_SELL = 0.00025
NSE_TXN = 0.0000297
SEBI_FEE = 0.000001
STAMP_DELIVERY_BUY = 0.00015
STAMP_INTRADAY_BUY = 0.00003
GST = 0.18
DP_CHARGE_PER_SELL = 15.93          # Rs 13.5 + 18% GST, per scrip per day on delivery sells
INTRADAY_BROKERAGE_PCT = 0.0003
INTRADAY_BROKERAGE_CAP = 20.0


@dataclass
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    dp: float = 0.0

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp, 2)

    def as_dict(self):
        d = asdict(self)
        d["total"] = self.total
        return d


def charges(side: str, qty: int, price: float, product: str = "CNC") -> Charges:
    side = side.upper()
    turnover = qty * price
    c = Charges()
    c.exchange = turnover * NSE_TXN
    c.sebi = turnover * SEBI_FEE
    if product == "CNC":
        c.stt = turnover * STT_DELIVERY
        if side == "BUY":
            c.stamp = turnover * STAMP_DELIVERY_BUY
        else:
            c.dp = DP_CHARGE_PER_SELL
    elif product == "MIS":
        c.brokerage = min(turnover * INTRADAY_BROKERAGE_PCT, INTRADAY_BROKERAGE_CAP)
        if side == "SELL":
            c.stt = turnover * STT_INTRADAY_SELL
        else:
            c.stamp = turnover * STAMP_INTRADAY_BUY
    else:
        raise ValueError(f"unknown product {product}")
    c.gst = (c.brokerage + c.exchange + c.sebi) * GST
    for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp"):
        setattr(c, k, round(getattr(c, k), 2))
    return c


def round_trip_cost(qty: int, buy_price: float, sell_price: float, product: str = "CNC") -> float:
    return charges("BUY", qty, buy_price, product).total + charges("SELL", qty, sell_price, product).total
