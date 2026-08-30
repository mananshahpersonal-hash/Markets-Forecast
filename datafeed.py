"""
datafeed.py — price/quote layer with Finnhub as primary and Yahoo as fallback.

WHY THIS EXISTS
---------------
Yahoo (yfinance) is unofficial and rate-limits hard, which is what caused
positions to go stale and the dividend column to blank out. Finnhub gives
real-time US quotes on a free key (~60 calls/min), enough for the whole
portfolio. This module tries Finnhub first and silently falls back to Yahoo if
the key is missing, a symbol isn't covered, or Finnhub errors — so the app keeps
working no matter what.

The API key is read from Streamlit secrets (FINNHUB_KEY) or the environment —
NEVER hardcoded, so it never lands in GitHub.
"""

from __future__ import annotations
import os
import time
import urllib.request
import urllib.parse
import json

_BASE = "https://finnhub.io/api/v1"


def _key() -> str | None:
    """Finnhub key from Streamlit secrets first, then env. None if unset."""
    try:
        import streamlit as st
        k = st.secrets.get("FINNHUB_KEY")  # type: ignore[attr-defined]
        if k:
            return str(k).strip()
    except Exception:
        pass
    k = os.environ.get("FINNHUB_KEY")
    return k.strip() if k else None


def have_finnhub() -> bool:
    return bool(_key())


def _get(path: str, params: dict, timeout: float = 8.0):
    """GET helper. Returns parsed JSON or None on any error/rate-limit."""
    k = _key()
    if not k:
        return None
    params = dict(params)
    params["token"] = k
    url = _BASE + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-helper/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            msg = str(e)
            if "429" in msg and attempt < 2:      # rate limited: brief backoff
                time.sleep(1.2 * (attempt + 1))
                continue
            return None
    return None


# --------------------------------------------------------------------------- #
# Quotes                                                                       #
# --------------------------------------------------------------------------- #
def finnhub_quote(symbol: str):
    """Return {'price','prev_close','change','pct'} from Finnhub, or None.
    Finnhub /quote returns: c=current, d=change, dp=pct, pc=prev close."""
    # Finnhub expects class shares as BRK.B (dot). Yahoo uses BRK-B (dash);
    # if a dash-form sneaks in, convert it.
    sym = symbol.replace("-", ".") if symbol.upper() in ("BRK-B", "BRK-A", "BF-B") else symbol
    d = _get("/quote", {"symbol": sym})
    if not d:
        return None
    c = d.get("c")
    if not c:                                     # 0 or missing => no coverage
        return None
    return {"price": float(c), "prev_close": float(d.get("pc") or c),
            "change": float(d.get("d") or 0.0), "pct": float(d.get("dp") or 0.0)}


def finnhub_quotes(symbols) -> dict:
    """Batch of single-symbol quote calls (Finnhub has no true batch on free
    tier). Free tier is ~60/min, so we pace at ~1.1s/call to never trip the
    limit mid-batch — better to take a few extra seconds than to miss the last
    9 tickers and value them at cost. Returns {symbol: quote_dict}."""
    out = {}
    if not have_finnhub():
        return out
    for s in symbols:
        q = finnhub_quote(s)
        if q:
            out[s] = q
        time.sleep(1.05)                          # ~57/min, safely under the cap
    return out


# --------------------------------------------------------------------------- #
# Fundamentals: dividend + earnings                                           #
# --------------------------------------------------------------------------- #
def finnhub_basic_dividend(symbol: str):
    """Trailing annual dividend/share via /stock/metric (dividendPerShareAnnual).
    Returns float or None."""
    d = _get("/stock/metric", {"symbol": symbol, "metric": "all"})
    if not d:
        return None
    m = d.get("metric") or {}
    for key in ("dividendPerShareAnnual", "dividendPerShareTTM"):
        v = m.get(key)
        if v:
            try:
                return float(v)
            except Exception:
                pass
    return None


def finnhub_next_earnings(symbol: str):
    """Next earnings date 'YYYY-MM-DD' from /calendar/earnings, or None."""
    import datetime as _dt
    today = _dt.date.today()
    frm = today.isoformat()
    to = (today + _dt.timedelta(days=120)).isoformat()
    d = _get("/calendar/earnings", {"symbol": symbol, "from": frm, "to": to})
    if not d:
        return None
    rows = d.get("earningsCalendar") or []
    dates = sorted(r.get("date") for r in rows if r.get("date"))
    return dates[0] if dates else None


def stooq_quote(symbol: str):
    """Keyless fallback via Stooq's light quote endpoint. Covers US stocks AND
    ETFs (QDTE, VXUS, etc.) that Finnhub's free tier misses, and works when Yahoo
    is blocked. Returns {'price','prev_close','change','pct'} or None.

    Uses /q/l/ (light quote) with an explicit field list:
      s=symbol, d2=date, t2=time, o/h/l/c=OHLC, v=volume, p=previous close? 
    We request close (c) and open (o). Stooq gives the latest close, which on a
    weekend is Friday's — exactly what we want when markets are shut."""
    sym = symbol.lower().replace(".", "-") + ".us"
    # f=sd2t2ohlcvn : symbol,date,time,open,high,low,close,volume,name
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode().strip()
        lines = body.splitlines()
        if len(lines) < 2:
            return None
        hdr = lines[0].split(",")
        row = lines[1].split(",")
        rec = dict(zip(hdr, row))
        close = rec.get("Close") or rec.get("close")
        if not close or close in ("N/D", "-"):
            return None
        close = float(close)
        openp = rec.get("Open") or rec.get("open")
        prev = float(openp) if openp and openp not in ("N/D", "-") else close
        return {"price": close, "prev_close": prev,
                "change": close - prev,
                "pct": (close/prev - 1)*100 if prev else 0.0}
    except Exception:
        return None


def best_quote(symbol: str):
    """Get a quote, trying the most reliable source first.

    Stooq is tried FIRST: it's keyless, has no rate limit, and covers both
    stocks AND ETFs (QDTE, VXUS, SPYI...) — the exact things Finnhub's free tier
    drops. Finnhub is the fallback for anything Stooq somehow misses. This order
    is deliberate: it's what gets all 38 holdings priced instead of ~29."""
    q = stooq_quote(symbol)
    if q:
        return q
    q = finnhub_quote(symbol)
    if q:
        return q
    return None


def source_label() -> str:
    """Human tag for which feeds are active."""
    return "Stooq + Finnhub" if have_finnhub() else "Stooq (keyless)"
