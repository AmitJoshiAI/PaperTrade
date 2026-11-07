"""Zerodha equity charge model (NSE). Rates as published on zerodha.com/charges, checked 2026-10-17.
Re-verify after any budget or SEBI/exchange circular; these move.

Delivery (CNC):  brokerage 0; STT 0.1% buy + 0.1% sell; NSE txn 0.00307% (was 0.00297% until 7 Nov 2026); SEBI 0.0001%;
                 stamp duty 0.015% buy; GST 18% on (brokerage + txn + SEBI); DP charge on each sell.
Intraday (MIS):  brokerage min(0.03%, Rs 20) per order; STT 0.025% sell only; NSE txn 0.00307%;
                 SEBI 0.0001%; stamp 0.003% buy; GST 18% on (brokerage + txn + SEBI); no DP charge.
Futures (FUT):   brokerage min(0.03%, Rs 20) per order; STT 0.05% sell only (raised from 0.02% on 1 April 2026,
                 Budget 2026-27, per Zerodha's bulletin); NSE txn 0.00183% (NSE circular NSE/FA/73061 of 27 Feb 2026,
                 effective 1 Mar 2026, read directly 7 Nov 2026); SEBI 0.0001%; stamp 0.002%
                 buy; GST 18% on (brokerage + txn + SEBI); no DP charge. Charges are on the whole contract
                 turnover (qty x price). Used by the F&O sleeve (fno.py) for its futures and by the thesis
                 sleeve for its PAPER shorts (a cash-market short must be squared off the same day in India, so
                 holding a short needs a stock future): from 4 Nov 2026 a new thesis SHORT pays 0.05% STT (a
                 COVER is a buy and pays no STT, so the open HINDPETRO short's exit cost is unchanged apart from
                 the 0.0001% txn change).
Options (OPT):   brokerage Rs 20 flat per order; STT 0.15% of premium on the sell side (from 1 April 2026; was
                 0.1%); NSE txn 0.03553% of premium (NSE/FA/73061, read directly 7 Nov 2026); SEBI 0.0001%; stamp 0.003% of premium on buys;
                 GST 18% on (brokerage + txn + SEBI). On a long option exercised at expiry: STT 0.15% of the
                 intrinsic value (no other charge modelled). Turnover = premium x units.
                 STT rates verified 4 Nov 2026 (broker notes); NSE transaction charges for cash, futures and options
                 verified 7 Nov 2026 against NSE/FA/73061 (https://nsearchives.nseindia.com/content/circulars/FA73061.pdf):
                 each figure is the total outflow (transaction charge + Re 0.01 IPFT) which Zerodha passes on.
US (USD):        us_charges(): zero commission; SEC fee 0.0000206 x sell notional (verified sec.gov 6 Nov 2026); FINRA TAF by calendar year
                 (US_FINRA_TAF_BY_YEAR; 2026: $0.000195 per share sold, max $9.79 per trade, $0.00329 per option
                 contract sold; steps up each 1 Jan to 2029; finra.org schedule read 7 Nov 2026). The SEC rate is
                 reset by the SEC, usually in April: re-read sec.gov each year. FX: the day's USD/INR (fx.py).
"""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict

STT_DELIVERY = 0.001
STT_INTRADAY_SELL = 0.00025
NSE_TXN = 0.0000307                # 0.00307%: Rs 306.99 txn + Rs 0.01 IPFT per crore each side (NSE/FA/73061, read 7 Nov 2026)
SEBI_FEE = 0.000001
STAMP_DELIVERY_BUY = 0.00015
STAMP_INTRADAY_BUY = 0.00003
GST = 0.18
DP_CHARGE_PER_SELL = 15.93          # Rs 13.5 + 18% GST, per scrip per day on delivery sells
INTRADAY_BROKERAGE_PCT = 0.0003
INTRADAY_BROKERAGE_CAP = 20.0
STT_FUT_SELL = 0.0005               # 0.05% sell side from 1 Apr 2026 (Zerodha bulletin; broker notes, 4 Nov 2026)
NSE_TXN_FUT = 0.0000183            # 0.00183%: Rs 183 per crore each side (NSE/FA/73061, read 7 Nov 2026)
STAMP_FUT_BUY = 0.00002
FUT_BROKERAGE_PCT = 0.0003
FUT_BROKERAGE_CAP = 20.0
OPT_BROKERAGE_FLAT = 20.0
STT_OPT_SELL = 0.0015               # 0.15% of sell premium from 1 Apr 2026 (verified, broker notes 4 Nov 2026)
STT_OPT_EXERCISE = 0.0015           # 0.15% of intrinsic value on exercise (verified, same source)
NSE_TXN_OPT = 0.0003553            # 0.03553% of premium: Rs 3,553 per crore each side (NSE/FA/73061, read 7 Nov 2026)
STAMP_OPT_BUY = 0.00003
US_SEC_FEE = 0.0000206              # x sell notional (USD): $20.60 per million from 4 Apr 2026 (sec.gov fee rate advisory 2026-2, read 6 Nov 2026)
# FINRA Trading Activity Fee by calendar year (effective 1 Jan of each year): (per share sold, max per trade,
# per option contract sold). finra.org SR-FINRA-2024-019 fee adjustment schedule, read directly 7 Nov 2026.
# Years after the last row keep the last row's rates until the schedule is re-read.
US_FINRA_TAF_BY_YEAR = {
    2025: (0.000166, 8.30, 0.00279),
    2026: (0.000195, 9.79, 0.00329),
    2027: (0.000232, 11.61, 0.00390),
    2028: (0.000240, 12.05, 0.00404),
    2029: (0.000249, 12.50, 0.00420),
}


def finra_taf(year: int | None = None) -> tuple[float, float, float]:
    """(per share, cap per trade, per contract) for a calendar year; default = today's year."""
    import datetime as _dt
    y = year or _dt.date.today().year
    return US_FINRA_TAF_BY_YEAR[max(k for k in US_FINRA_TAF_BY_YEAR if k <= y)] if y >= min(US_FINRA_TAF_BY_YEAR)         else US_FINRA_TAF_BY_YEAR[min(US_FINRA_TAF_BY_YEAR)]


US_FINRA_TAF_SHARE, US_FINRA_TAF_CAP, US_FINRA_TAF_CONTRACT = finra_taf()   # this year's rates


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
        taf_share, taf_cap, taf_contract = finra_taf()
        c.exchange = qty * taf_contract if option else min(qty * taf_share, taf_cap)
    for k in ("sebi", "exchange"):         # both fees are rounded UP to the next cent
        v = getattr(c, k)
        setattr(c, k, math.ceil(round(v * 100, 6)) / 100 if v > 0 else 0.0)
    return c
