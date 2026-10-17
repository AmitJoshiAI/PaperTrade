"""Zerodha equity charge model (NSE). Rates as published on zerodha.com/charges, checked 2026-10-17.
Re-verify after any budget or SEBI/exchange circular; these move.

Delivery (CNC):  brokerage 0; STT 0.1% buy + 0.1% sell; NSE txn 0.00297%; SEBI 0.0001%;
                 stamp duty 0.015% buy; GST 18% on (brokerage + txn + SEBI); DP charge on each sell.
Intraday (MIS):  brokerage min(0.03%, Rs 20) per order; STT 0.025% sell only; NSE txn 0.00297%;
                 SEBI 0.0001%; stamp 0.003% buy; GST 18% on (brokerage + txn + SEBI); no DP charge.
Futures (FUT):   brokerage min(0.03%, Rs 20) per order; STT 0.02% sell only; NSE txn 0.00173%;
                 SEBI 0.0001%; stamp 0.002% buy; GST 18% on (brokerage + txn + SEBI); no DP charge.
                 Used only for the thesis sleeve's PAPER shorts (a cash-market short must be squared off
                 the same day in India, so holding a short needs a stock future). The FUT rates were
                 written from the task brief, not read off zerodha.com: VERIFY AGAINST zerodha.com/charges
                 (F&O tab) before relying on them. In particular futures STT was 0.02% after Oct 2024;
                 check whether a later Budget changed it. Charges are on the whole contract turnover
                 (qty x price). The F&O sleeve (fno.py) uses the same FUT rates for its futures.
                 2026 NOTE: Zerodha's support pages and Budget 2026-27 reporting say futures STT rose to
                 0.05% (sell) on 1 April 2026. STT_FUT_SELL below is still 0.02% because changing it
                 changes the thesis sleeve's costs too; it is flagged for the user to decide.
Options (OPT):   brokerage Rs 20 flat per order; STT 0.15% of premium on the sell side; NSE txn 0.03503% of
                 premium; SEBI 0.0001%; stamp 0.003% of premium on buys; GST 18% on (brokerage + txn + SEBI).
                 On a long option exercised at expiry: STT 0.15% of the intrinsic value (no other charge
                 modelled). Turnover = premium x units. VERIFY AGAINST zerodha.com/charges (F&O tab): the
                 brief that commissioned this said 0.1% sell STT (the pre-April-2026 rate, exercise 0.125%);
                 0.15% for both follows Zerodha's STT support article as found on 2026-10-17.
US (USD):        us_charges(): zero commission; SEC fee 0.0000278 x sell notional; FINRA TAF $0.000166 per
                 share sold, max $8.30 per trade (options: $0.00279 per contract sold). VERIFY: both fees
                 are reset by the SEC/FINRA, usually once a year. FX: the day's USD/INR (fx.py).
"""
from __future__ import annotations
import math
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
STT_FUT_SELL = 0.0002               # VERIFY against zerodha.com/charges (see docstring)
NSE_TXN_FUT = 0.0000173
STAMP_FUT_BUY = 0.00002
FUT_BROKERAGE_PCT = 0.0003
FUT_BROKERAGE_CAP = 20.0
OPT_BROKERAGE_FLAT = 20.0           # VERIFY all OPT_* against zerodha.com/charges (see docstring)
STT_OPT_SELL = 0.0015
STT_OPT_EXERCISE = 0.0015
NSE_TXN_OPT = 0.0003503
STAMP_OPT_BUY = 0.00003
US_SEC_FEE = 0.0000278              # x sell notional (USD). VERIFY on sec.gov (Section 31 fee rate)
US_FINRA_TAF_SHARE = 0.000166       # per share sold. VERIFY on finra.org
US_FINRA_TAF_CAP = 8.30
US_FINRA_TAF_CONTRACT = 0.00279     # per option contract sold


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
    c.exchange = turnover * {"FUT": NSE_TXN_FUT, "OPT": NSE_TXN_OPT}.get(product, NSE_TXN)
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
    elif product == "FUT":
        c.brokerage = min(turnover * FUT_BROKERAGE_PCT, FUT_BROKERAGE_CAP)
        if side == "SELL":
            c.stt = turnover * STT_FUT_SELL
        else:
            c.stamp = turnover * STAMP_FUT_BUY
    elif product == "OPT":
        c.brokerage = OPT_BROKERAGE_FLAT
        if side == "SELL":
            c.stt = turnover * STT_OPT_SELL
        else:
            c.stamp = turnover * STAMP_OPT_BUY
    else:
        raise ValueError(f"unknown product {product}")
    c.gst = (c.brokerage + c.exchange + c.sebi) * GST
    for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "dp"):
        setattr(c, k, round(getattr(c, k), 2))
    return c


def round_trip_cost(qty: int, buy_price: float, sell_price: float, product: str = "CNC") -> float:
    return charges("BUY", qty, buy_price, product).total + charges("SELL", qty, sell_price, product).total


def exercise_charges(intrinsic_value: float) -> Charges:
    """A long NSE option settled in the money at expiry: STT on the intrinsic value (premium x units)."""
    return Charges(stt=round(max(intrinsic_value, 0.0) * STT_OPT_EXERCISE, 2))


def us_charges(side: str, qty: int, price: float, option: bool = False) -> Charges:
    """US regulatory fees in USD (commission is zero). qty = shares, or contracts for an option; price per
    share (an option's price is per share, x 100 per contract). Only sells pay: SEC fee and FINRA TAF."""
    c = Charges()
    if side.upper() == "SELL":
        notional = qty * price * (100 if option else 1)
        c.sebi = notional * US_SEC_FEE                     # the 'sebi' slot holds the regulator's fee (SEC)
        c.exchange = qty * US_FINRA_TAF_CONTRACT if option else min(qty * US_FINRA_TAF_SHARE, US_FINRA_TAF_CAP)
    for k in ("sebi", "exchange"):         # both fees are rounded UP to the next cent
        v = getattr(c, k)
        setattr(c, k, math.ceil(round(v * 100, 6)) / 100 if v > 0 else 0.0)
    return c
