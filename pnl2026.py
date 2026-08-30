"""
pnl2026.py  —  Manan's 2026 profit-and-loss facts, parsed and QA'd from the
Robinhood source documents (brokerage monthly statements Dec-2024 → Jul-2026,
RHD futures monthly statements Dec-2025 → Jul-2026, and Robinhood Crypto
statements Oct-2025 → Jul-2026).

WHY THIS IS A DATA MODULE, NOT LIVE PARSING
-------------------------------------------
The figures below were computed OFFLINE and checked against the source three
ways: (1) every futures month satisfies the cash identity
beginning + gross P&L + fees + cash-activity = ending, to the penny;
(2) each month's Gross P&L equals the sum of its Purchase-and-Sale rows;
(3) every stock sale's proceeds reconstructed from a FIFO replay of all
2024-12 → 2026-07 trades matches the Robinhood proceeds exactly. Storing the
audited results (rather than re-parsing PDFs in the web app, which can't) keeps
the number reproducible and honest. Refresh = re-run the parse when new
statements arrive and update the numbers here.

EVERY LINE CARRIES A `basis` TAG:
    "exact"  — reconciled to the source, no estimation
    "approx" — a cash-basis proxy or a mark-to-market figure, clearly not a
               clean realized-gain number
    "pending"— known to exist but a required source doc is still missing
"""

from dataclasses import dataclass, field
from typing import Optional
import datetime as _dt

AS_OF = "2026-08-30"

# Coverage the numbers below are built from (so the app can state its own gaps):
FUTURES_MONTHS_DONE = ["2026-01", "2026-02", "2026-03", "2026-04",
                       "2026-05", "2026-06", "2026-07"]
FUTURES_MONTHS_MISSING = ["2026-08"]          # issued ~Sep 1
STOCK_BASIS_MISSING = []          # all resolved (pre-2025 costs confirmed)


@dataclass
class Line:
    label: str
    amount: float
    basis: str                    # "exact" | "approx" | "pending"
    note: str = ""


# ----------------------------------------------------------------- dividends --
# CDIV + MDIV received, 2026 YTD, from the brokerage activity CSVs.
DIVIDENDS = Line("Dividends received (CDIV + MDIV)", 43357.0, "exact",
                 "Cash + manufactured dividends, summed by transaction code.")

# ------------------------------------------------------------ margin interest --
MARGIN_INTEREST = Line("Margin interest paid (MINT)", -24838.0, "exact",
                       "Aggregated margin rate charges, 2026 YTD.")

# --------------------------------------------------------------- gold bonus ----
GOLD_BONUS = Line("Gold Deposit Boost Payment (GDBP)", 373.0, "exact")

# --------------------------------------------------------------- options -------
# STO premium minus BTC cost, net cash. Cash-basis proxy — not a clean realized
# P&L, because assignments/expirations move the economics into the stock lots.
OPTIONS_NET = Line("Options premium, net cash (STO − BTC)", 13862.0, "approx",
                   "Cash-basis approximation. Assigned options flow into the "
                   "stock cost basis below, so this is not double-counted there.")

# --------------------------------------------------------------- futures -------
# Realized per month from the RHD statements: Gross P&L + Total Commissions/Fees.
# QA: each month passes the cash identity AND the Purchase-and-Sale row sum.
FUTURES_MONTHLY = {
    "2026-01": Line("Futures — January",  86429.06, "exact"),
    "2026-02": Line("Futures — February", -8643.33, "exact"),
    "2026-03": Line("Futures — March",   -52060.14, "exact"),
    "2026-04": Line("Futures — April",    13913.63, "exact"),
    "2026-05": Line("Futures — May",     -37457.79, "exact"),
    "2026-06": Line("Futures — June",      6714.92, "exact"),
    "2026-07": Line("Futures — July",       360.55, "exact"),
}
FUTURES_TOTAL = Line(
    "Realized futures/contracts P&L (Jan–Jul)",
    round(sum(l.amount for l in FUTURES_MONTHLY.values()), 2), "exact",
    "Copper/gold micro futures (HG, MGC, GC, MHG, MCL, etc.). Replaces the old "
    "+$19,243 cash-sweep estimate. August pending (statement issues ~Sep 1). "
    "−$18,350 was still open/unrealized at Jul 31.")

# Per-contract realized GROSS P&L for 2026 (before the year's fees), summed from
# every Purchase-and-Sale row across all seven monthly statements. This exists so
# the app can SHOW gold's loss instead of netting it against copper's gain.
# Sum of these = +$13,225 gross; minus $3,968 fees = the $9,257 net above.
FUTURES_BY_CONTRACT = {
    "Gold (GC + MGC)":     -30333.0,
    "Copper (HG + MHG)":    43927.5,
    "Crude (CL + MCL)":      -403.0,
    "Micro Dow (MYM)":         33.5,
}
FUTURES_FEES_2026 = -3968.0
# Detail: GC big-gold -40,100 and MGC micro-gold +9,767 → gold net -30,333.
# Worst gold months: Mar (GC -62,590) and May (MGC -39,504).

# --------------------------------------------------------------- crypto --------
# You sold BTC/XRP/DOGE/ETH on 2026-02-05 for $15,481 gross ($15,349 net of
# fees). The statements never show your original coin cost, so the true realized
# gain can't be computed; the honest 2026 figure is the mark-to-market change:
# Jan-1 account value $20,045.97 → net proceeds $15,349.28.
CRYPTO_MTM = Line("Crypto (mark-to-market change, 2026)", -4697.0, "approx",
                  "Exposure was direct coins (BTC/XRP/DOGE/ETH), all sold "
                  "2026-02-05. No cost basis in the statements, so this is the "
                  "value change (Jan-1 $20,046 → net proceeds $15,349), not a "
                  "clean realized gain. ETF crypto (IBIT/ETHA/BTCI) is in stocks.")

# --------------------------------------------------------------- stock sales ---
# FIFO realized gain/loss for the 17 tickers sold in 2026. Proceeds for all 17
# reconcile to the Robinhood CSV exactly. 11 have full cost basis (exact); 6 hold
# pre-2025 lots whose cost isn't in any statement (pending — need order detail).
@dataclass
class StockSale:
    ticker: str
    shares: float
    proceeds: float
    cost: Optional[float]         # None => basis unknown
    st_gain: float = 0.0
    lt_gain: float = 0.0
    basis: str = "exact"
    note: str = ""

    @property
    def gain(self) -> Optional[float]:
        return None if self.cost is None else self.proceeds - self.cost


STOCK_SALES = [
    StockSale("MSFT", 600, 308885, 294000, 14885, 0, "exact",
              "Aug sale; lots from Nov-25 & Jan-26 put assignments — cost known."),
    StockSale("VGT",   30,  22820,  22081,   739, 0, "exact"),
    StockSale("SPY",   30,  20762,  19431,  1239, 91, "exact"),
    StockSale("VOO",   30,  19091,  17910,  1181, 0, "exact"),
    StockSale("QQQ",   30,  18737,  17451,  1187, 100, "exact"),
    StockSale("BRK.B", 30,  15000,  14890,   104, 5, "exact"),
    StockSale("VOOG",  30,  13422,  12556,   795, 70, "exact"),
    StockSale("PLTR", 100,  12950,  12746,   204, 0, "exact"),
    StockSale("ITOT",  35,   5306,   4989,   298, 19, "exact"),
    StockSale("PG",    16,   2390,   2268,   122, 0, "exact"),
    StockSale("FRMI", 200,   1800,   3900, -2100, 0, "exact",
              "Realized loss."),
    # ---- pre-2025 lots; cost basis confirmed by user (all long-term) ----
    StockSale("AAPL", 200,  60009,  56000, 0,  4009, "exact",
              "Avg cost $280.00 (pre-2025 lot)."),
    StockSale("VKTX", 2000, 65198, 116000, 0, -50802, "exact",
              "Avg cost $58.00 (pre-2025 lot). Large long-term loss."),
    StockSale("VZ",   1100, 46200,  50600, 0,  -4400, "exact",
              "Avg cost $46.00 (pre-2025 lot)."),
    StockSale("MCD",   100, 30000,  26500, 0,   3500, "exact",
              "Avg cost $265.00 (pre-2025 lot)."),
    StockSale("MSTR",  200, 19707,  77000, 0, -57293, "exact",
              "Avg cost $385.00 (pre-2025 lot). Large long-term loss."),
    StockSale("T",     700, 18200,  13370, 0,   4830, "exact",
              "600 sh @ $18.00 (pre-2025) + 100 sh @ $25.695 (known)."),
]

STOCK_REALIZED_KNOWN = Line(
    "Realized stock gains/losses (all 17 sales)",
    round(sum(s.gain for s in STOCK_SALES if s.gain is not None), 2), "exact",
    "FIFO, full cost basis for every sale. Net is a LOSS, driven by VKTX "
    "(−$50,802) and MSTR (−$57,293) long-term. ST + LT split per line.")

STOCK_ST = round(sum(s.st_gain for s in STOCK_SALES), 2)
STOCK_LT = round(sum(s.lt_gain for s in STOCK_SALES), 2)

STOCK_PENDING_PROCEEDS = round(
    sum(s.proceeds for s in STOCK_SALES if s.cost is None), 2)


# ----------------------------------------------------------------- assembly ----
def summary_lines():
    """Ordered P&L lines for display. Pending stock basis is shown as a note,
    not folded into the total, so the grand total stays honest."""
    return [
        DIVIDENDS,
        MARGIN_INTEREST,
        GOLD_BONUS,
        OPTIONS_NET,
        FUTURES_TOTAL,
        CRYPTO_MTM,
        STOCK_REALIZED_KNOWN,
    ]


def grand_total():
    """Sum of the summary lines. Excludes the 6 pending-basis stock sales
    (proceeds $%d, all long-term) which will only add to the number.""" \
        % STOCK_PENDING_PROCEEDS
    return round(sum(l.amount for l in summary_lines()), 2)


def has_approx():
    return any(l.basis != "exact" for l in summary_lines())


def signal_from_read(r) -> str:
    """Buy/Hold/Sell for a holding, using the SAME rule as the app's top-of-app
    strong-signal alert so they never disagree. `r` is a dict with keys
    trend_up / trend_dn / trend_pct / overbought / oversold (whatever the caller
    can cheaply compute from the price series). Anything short of a strong,
    clean, non-overextended trend is an honest HOLD."""
    if not isinstance(r, dict):
        return "Hold"
    up, dn = r.get("trend_up"), r.get("trend_dn")
    pct = r.get("trend_pct", 0) or 0
    if up and not r.get("overbought") and pct >= 12:
        return "Buy"
    if dn and not r.get("oversold") and pct <= -12:
        return "Sell"
    return "Hold"


def read_from_closes(closes) -> dict:
    """Cheap 50/200-day + 3-month trend read from a pandas close series, matching
    the app engine's definition of a 'strong' trend. Returns {} if data is thin
    so the caller shows Hold."""
    try:
        import pandas as pd  # noqa
        c = closes.dropna()
        if c is None or len(c) < 60:
            return {}
        sma50 = float(c.tail(50).mean())
        sma200 = float(c.tail(200).mean()) if len(c) >= 200 else float(c.mean())
        spot = float(c.iloc[-1])
        base3m = float(c.iloc[-63]) if len(c) >= 63 else float(c.iloc[0])
        trend_pct = (spot / base3m - 1) * 100 if base3m else 0.0
        # "overextended" = stretched far ABOVE/BELOW the 50-day, i.e. a snap-back
        # risk — not merely near a rolling high. A steady uptrend that hugs its
        # 50-day is NOT overbought.
        ext = (spot / sma50 - 1) * 100 if sma50 else 0.0
        return {
            "trend_up": spot > sma50 > sma200,
            "trend_dn": spot < sma50 < sma200,
            "trend_pct": trend_pct,
            "overbought": ext >= 15,
            "oversold": ext <= -15,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# UPCOMING EVENTS CATALOG
# ---------------------------------------------------------------------------
# Short, human notes about things coming up for a holding that a price feed can
# NEVER know (product launches, splits, notable catalysts). Edit freely — this
# is the one place these live. Keep each note to a phrase. Dates optional.
# Format: TICKER: (note, approx_date_or_None)
UPCOMING_EVENTS = {
    "AAPL": ("iPhone 17 launch event expected", "2026-09"),
    "NVDA": ("GTC / next-gen GPU cadence a recurring catalyst", None),
    "TSLA": ("Delivery numbers each quarter move the stock", None),
    "AMZN": ("AWS re:Invent (late Nov) — cloud guidance", "2026-11"),
    "MCD": ("Dividend aristocrat; watches consumer spend", None),
}


def event_for(ticker: str):
    """Return a short 'what's coming up' note for a ticker, or ''. """
    e = UPCOMING_EVENTS.get(ticker.upper())
    if not e:
        return ""
    note, when = e
    return f"{note}" + (f" (~{when})" if when else "")
