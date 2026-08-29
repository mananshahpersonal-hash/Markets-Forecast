"""portfolio.py — Robinhood-CSV portfolio analytics.

Parses a Robinhood activity/report CSV (flexible headers), classifies each row
(stocks & ETFs, crypto, futures, options, dividends, cash), rebuilds positions
with FIFO lots (short- vs long-term), computes realized/unrealized P&L, dividend
income by period, and produces a TRANSPARENT tax ESTIMATE for an Illinois
resident (flat 4.95% state; federal ST at your marginal rate, LT at 0/15/20%,
optional 3.8% NIIT). Estimates only — NOT tax advice; verify with a CPA.
"""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Optional

import numpy as np
import pandas as pd

CRYPTO = {"BTC", "ETH", "DOGE", "SOL", "ADA", "XRP", "LTC", "BCH", "AVAX",
          "SHIB", "LINK", "UNI", "MATIC", "DOT", "XLM", "AAVE", "COMP", "ETC",
          "USDC", "PEPE", "BONK"}
DIV_CODES = {"CDIV", "DIV", "MDIV", "QDIV"}
CASH_CODES = {"ACH", "DFEE", "GOLD", "INT", "DTAX", "WIRE", "RTP", "OCA",
              "AFEE", "MGN", "SLIP", "DCF"}
OPT_CODES = {"BTO", "STC", "STO", "BTC", "OEXP", "OASGN"}


def _num(x) -> float:
    """Parse Robinhood-style numbers: '$1,234.56', '($123.45)' (negative), ''."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    s = str(x).strip()
    if not s or s.lower() in ("nan", "none", "--"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[()$,]", "", s)
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return -v if neg else v


_HEADER_MAP = {
    "date": ["activity date", "date", "process date", "trade date"],
    "instrument": ["instrument", "symbol", "ticker"],
    "description": ["description", "desc"],
    "code": ["trans code", "transaction code", "type", "trans type", "action"],
    "qty": ["quantity", "qty", "shares"],
    "price": ["price", "avg price", "average price"],
    "amount": ["amount", "net amount", "value", "total"],
}


def parse_csv(data) -> pd.DataFrame:
    """Read a Robinhood CSV (bytes/str/file) into a normalized DataFrame with
    columns: date, instrument, description, code, qty, price, amount.
    Robust to real-world exports: finds the header row even if junk precedes it,
    and skips malformed lines (extra commas, footer disclaimers) instead of
    crashing — the skipped count is reported in df.attrs['skipped']."""
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", errors="ignore")
    elif isinstance(data, str):
        text = data
    else:
        blob = data.read()
        text = blob.decode("utf-8", errors="ignore") if isinstance(blob, (bytes, bytearray)) else str(blob)
    lines = text.splitlines()
    hdr_idx = 0
    for i, ln in enumerate(lines[:25]):
        low = ln.lower()
        if "activity date" in low or ("date" in low and "amount" in low):
            hdr_idx = i
            break
    body = "\n".join(lines[hdr_idx:])
    skipped: list = []

    def _bad(line):
        skipped.append(line)
        return None

    try:
        raw = pd.read_csv(io.StringIO(body))
    except Exception:
        try:
            raw = pd.read_csv(io.StringIO(body), engine="python", on_bad_lines=_bad)
        except TypeError:                       # very old pandas: no callable form
            raw = pd.read_csv(io.StringIO(body), engine="python",
                              on_bad_lines="skip")
    raw = raw.loc[:, [c for c in raw.columns if not str(c).startswith("Unnamed")]]
    cols = {str(c).lower().strip(): c for c in raw.columns}
    picked = {}
    for want, options in _HEADER_MAP.items():
        for o in options:
            if o in cols:
                picked[want] = cols[o]
                break
    if "date" not in picked or "amount" not in picked:
        raise ValueError(
            "Couldn't recognize the CSV columns. Found: "
            + ", ".join(str(c) for c in raw.columns))
    df = pd.DataFrame()
    df["date"] = pd.to_datetime(raw[picked["date"]], errors="coerce")
    df["instrument"] = (raw[picked["instrument"]].astype(str).str.strip().str.upper()
                        if "instrument" in picked else "")
    df["description"] = (raw[picked["description"]].astype(str)
                         if "description" in picked else "")
    df["code"] = (raw[picked["code"]].astype(str).str.strip().str.upper()
                  if "code" in picked else "")
    df["qty"] = raw[picked["qty"]].map(_num) if "qty" in picked else 0.0
    df["price"] = raw[picked["price"]].map(_num) if "price" in picked else 0.0
    df["amount"] = raw[picked["amount"]].map(_num)
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    df["instrument"] = df["instrument"].replace({"NAN": "", "NONE": ""})
    df.attrs["skipped"] = len(skipped)
    df.attrs["skipped_lines"] = skipped[:3]
    return df


def classify_row(instrument, description, code) -> str:
    d = str(description or "").lower()
    inst = str(instrument or "").upper().strip()
    if inst in ("NAN", "NONE"):
        inst = ""
    code = str(code or "").upper().strip()
    if code in DIV_CODES or "dividend" in d:
        return "dividend"
    if code in CASH_CODES or (not inst and code not in OPT_CODES):
        return "cash"
    if inst.startswith("/") or "future" in d:
        return "Futures"
    if inst in CRYPTO or "crypto" in d:
        return "Crypto"
    if code in OPT_CODES or re.search(r"\b(call|put)\b", d):
        return "Options"
    return "Stocks & ETFs"


def analyze(tx: pd.DataFrame) -> dict:
    """FIFO through the trades. Returns positions, realized gains (ST/LT), the
    dividend ledger, and per-class net cash for futures/options (approx P&L)."""
    tx = tx.copy()
    tx["class"] = [classify_row(i, d, c) for i, d, c in
                   zip(tx["instrument"], tx["description"], tx["code"])]
    lots: dict[str, list] = {}
    positions: dict[str, dict] = {}
    realized = []
    for _, r in tx.iterrows():
        cls = r["class"]
        inst = r["instrument"]
        if cls in ("dividend", "cash") or not inst:
            continue
        if cls in ("Futures", "Options"):
            p = positions.setdefault(inst, {"class": cls, "shares": 0.0,
                                            "cost": 0.0, "net_cash": 0.0})
            p["net_cash"] += r["amount"]
            continue
        code = r["code"]
        is_buy = code == "BUY" or (r["amount"] < 0 and r["qty"] > 0 and code not in ("SELL",))
        is_sell = code == "SELL" or (r["amount"] > 0 and r["qty"] > 0 and code not in ("BUY",))
        qty = abs(r["qty"])
        px = r["price"] if r["price"] > 0 else (abs(r["amount"]) / qty if qty else 0)
        if qty <= 0:
            continue
        L = lots.setdefault(inst, [])
        if is_buy:
            L.append({"date": r["date"], "qty": qty, "px": px})
        elif is_sell:
            remain = qty
            while remain > 1e-9 and L:
                lot = L[0]
                take = min(lot["qty"], remain)
                gain = (px - lot["px"]) * take
                term = "LT" if (r["date"] - lot["date"]).days > 365 else "ST"
                realized.append({"date": r["date"], "instrument": inst,
                                 "gain": gain, "term": term, "class": r["class"]})
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 1e-9:
                    L.pop(0)
        pos = positions.setdefault(inst, {"class": r["class"], "shares": 0.0,
                                          "cost": 0.0, "net_cash": 0.0})
        pos["shares"] = sum(l["qty"] for l in lots.get(inst, []))
        pos["cost"] = sum(l["qty"] * l["px"] for l in lots.get(inst, []))
    dividends = tx[tx["class"] == "dividend"][["date", "instrument", "amount"]].copy()
    rz = pd.DataFrame(realized) if realized else pd.DataFrame(
        columns=["date", "instrument", "gain", "term", "class"])
    return {"tx": tx, "positions": positions, "realized": rz, "dividends": dividends,
            "n_rows": int(len(tx)), "skipped": int(tx.attrs.get("skipped", 0)),
            "skipped_lines": list(tx.attrs.get("skipped_lines", []))}


def period_sums(df: pd.DataFrame, col: str, now: dt.datetime) -> dict:
    if df.empty:
        return {"today": 0.0, "week": 0.0, "month": 0.0, "ytd": 0.0, "all": 0.0}
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    t0 = pd.Timestamp(now.date())
    return {
        "today": float(d.loc[d["date"] >= t0, col].sum()),
        "week": float(d.loc[d["date"] >= t0 - pd.Timedelta(days=7), col].sum()),
        "month": float(d.loc[d["date"] >= t0 - pd.Timedelta(days=30), col].sum()),
        "ytd": float(d.loc[d["date"] >= pd.Timestamp(now.year, 1, 1), col].sum()),
        "all": float(d[col].sum()),
    }


def yahoo_symbol(instrument: str, cls: str) -> Optional[str]:
    if cls == "Crypto":
        return f"{instrument}-USD"
    if cls == "Stocks & ETFs":
        return instrument
    return None                       # futures/options: cash-flow only in v1


def market_windows(closes: pd.Series, now: dt.datetime) -> dict:
    """Return reference closes for today/week/month/ytd window starts."""
    if closes is None or len(closes) < 2:
        return {}
    c = closes.dropna()
    px_now = float(c.iloc[-1])
    def at_or_before(ts):
        s = c[c.index <= ts]
        return float(s.iloc[-1]) if len(s) else float(c.iloc[0])
    t0 = pd.Timestamp(now.date())
    return {"now": px_now,
            "today": at_or_before(t0 - pd.Timedelta(days=1)),
            "week": at_or_before(t0 - pd.Timedelta(days=7)),
            "month": at_or_before(t0 - pd.Timedelta(days=30)),
            "ytd": at_or_before(pd.Timestamp(now.year, 1, 1))}


def tax_estimate(st_gain: float, lt_gain: float, div_income: float,
                 fed_marginal: float, ltcg: float, niit: bool,
                 il_rate: float = 0.0495, dividends_qualified: bool = True) -> dict:
    """Transparent ESTIMATE. IL taxes everything at the flat rate (no LT break);
    federal: ST at marginal, LT at 0/15/20, dividends at LTCG if qualified else
    marginal; NIIT adds 3.8% on investment income if enabled."""
    st_pos, lt_pos, dv = max(st_gain, 0), max(lt_gain, 0), max(div_income, 0)
    net_offset = min(0.0, st_gain) + min(0.0, lt_gain)   # losses offset gains
    taxable_st = max(st_pos + min(net_offset, 0) if lt_pos == 0 else st_pos, 0)
    fed_st = st_pos * fed_marginal
    fed_lt = lt_pos * ltcg
    fed_dv = dv * (ltcg if dividends_qualified else fed_marginal)
    fed_niit = (st_pos + lt_pos + dv) * (0.038 if niit else 0.0)
    il = (st_pos + lt_pos + dv) * il_rate
    total = fed_st + fed_lt + fed_dv + fed_niit + il
    return {"fed_st": fed_st, "fed_lt": fed_lt, "fed_div": fed_dv,
            "fed_niit": fed_niit, "il": il, "total": total,
            "note_losses": net_offset}
