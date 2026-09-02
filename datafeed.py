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


def _get(path: str, params: dict, timeout: float = 6.0):
    """GET helper. Returns parsed JSON or None on any error. NO retry/backoff on
    429 — when Finnhub rate-limits mid-batch, sleeping to retry just burns
    seconds and stalls the whole fetch. We fail fast; the disk cache + next
    refresh pick up whatever got limited, fetching only the still-missing few
    (which then succeed because there are far fewer of them)."""
    k = _key()
    if not k:
        return None
    params = dict(params)
    params["token"] = k
    url = _BASE + path + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "market-helper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
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


def stockpricesdev_quote(symbol: str):
    """Keyless JSON quote from stockprices.dev — no auth, no limits, covers US
    stocks and ETFs. Documented response:
      {"Ticker","Name","Price","ChangeAmount","ChangePercentage"}
    Uses /api/stocks/<ticker>. Returns our standard dict or None."""
    sym = symbol.replace(".", "-")           # class shares as BRK-B
    url = f"https://stockprices.dev/api/stocks/{sym}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.load(r)
        price = d.get("Price")
        if not price:
            return None
        price = float(price)
        chg = float(d.get("ChangeAmount") or 0.0)
        pct = float(d.get("ChangePercentage") or 0.0)
        return {"price": price, "prev_close": price - chg,
                "change": chg, "pct": pct}
    except Exception:
        return None


def best_quote(symbol: str):
    """Get a quote. Finnhub is the only source reachable from the deploy
    environment (confirmed by the in-app feed diagnostic: stockprices.dev and
    Stooq are both blocked there, while Finnhub returns real prices for BOTH
    stocks and ETFs — e.g. QDTE). So this is Finnhub-only, with the other two
    kept as defensive fallbacks in case the environment's egress rules change."""
    q = finnhub_quote(symbol)
    if q and q.get("price"):
        return q
    for src in (stockpricesdev_quote, stooq_quote):
        try:
            q = src(symbol)
            if q and q.get("price"):
                return q
        except Exception:
            continue
    return None


def source_selftest(sample: str = "AAPL", etf: str = "QDTE"):
    """Live-probe each source in the ACTUAL runtime environment and report which
    ones return a price. Returns a list of (source_name, stock_ok, etf_ok,
    detail). This is how we find out which feed works from where the app runs,
    instead of guessing — because a stock like AAPL and an ETF like QDTE exercise
    both coverage cases."""
    out = []
    for _pname, _pkey, _pfn in HISTORY_PROVIDERS:
        try:
            if _pkey and not _secret(_pkey):
                out.append((f"{_pname} (history)", False, False,
                            f"no key set — add {_pkey} in Secrets to enable"))
                continue
            _c = _pfn("CPER")
            out.append((f"{_pname} (history)", _c is not None, False,
                        f"{len(_c)} daily closes, last=${float(_c.iloc[-1]):.2f}"
                        if _c is not None else "denied / no data"))
        except Exception as _ex:
            out.append((f"{_pname} (history)", False, False, f"error: {str(_ex)[:40]}"))
    for name, fn in (("stockprices.dev", stockpricesdev_quote),
                     ("Stooq", stooq_quote),
                     ("Finnhub", finnhub_quote)):
        s_ok = e_ok = False
        detail = ""
        try:
            qs = fn(sample); s_ok = bool(qs and qs.get("price"))
            qe = fn(etf); e_ok = bool(qe and qe.get("price"))
            if s_ok:
                detail = f"{sample}=${qs['price']:.2f}"
                if e_ok:
                    detail += f", {etf}=${qe['price']:.2f}"
                elif name == "Finnhub":
                    detail += f", {etf}=not covered (free tier)"
                else:
                    detail += f", {etf}=no data"
            else:
                detail = "no response / blocked"
        except Exception as ex:
            detail = f"error: {str(ex)[:40]}"
        out.append((name, s_ok, e_ok, detail))
    return out


def source_label() -> str:
    """Human tag for which feeds are active."""
    return "multi-source" if have_finnhub() else "keyless (Stooq/stockprices)"


# ---------------------------------------------------------------------------
# DAILY HISTORY via Finnhub candles — the Yahoo-replacement for the forecaster
# ---------------------------------------------------------------------------
# Yahoo's chart API is dead from server IPs (401 Invalid Crumb), which starved
# the metals forecaster of history. Finnhub is the one source PROVEN reachable
# from the deploy environment (in-app diagnostic), so history now comes from
# /stock/candle. Futures symbols aren't on the free tier, so metals use liquid
# ETF proxies whose daily RETURNS track the futures closely; the UI labels this.
HIST_PROXY = {"HG=F": ("CPER", "US Copper Index ETF"),
              "GC=F": ("GLD", "SPDR Gold Shares ETF"),
              "SI=F": ("SLV", "iShares Silver ETF"),
              "PL=F": ("PPLT", "abrdn Platinum ETF"),
              "PA=F": ("PALL", "abrdn Palladium ETF")}


def finnhub_candles(symbol: str, days: int = 730, resolution: str = "D"):
    """Daily close series from Finnhub /stock/candle, or None. Response format:
    {"s":"ok","t":[epochs],"c":[closes],...}; s="no_data" or an error → None."""
    try:
        import pandas as pd
    except Exception:
        return None
    now = int(time.time())
    d = _get("/stock/candle", {"symbol": symbol, "resolution": resolution,
                               "from": now - days * 86400, "to": now},
             timeout=10.0)
    if not d or d.get("s") != "ok" or not d.get("c"):
        return None
    try:
        s = pd.Series(d["c"], index=pd.to_datetime(d["t"], unit="s"))
        s = s[~s.index.duplicated(keep="last")].sort_index()
        return s if len(s) >= 60 else None
    except Exception:
        return None


def daily_history(ticker: str):
    """(series, note) — daily history for a ticker via the provider chain, using
    the ETF proxy for futures symbols. On failure returns (None, status) where
    status says exactly what each provider did (denied vs no key), so the error
    the user sees names the fix instead of shrugging."""
    proxy, label = HIST_PROXY.get(ticker, (None, None))
    sym = proxy or ticker.replace("=F", "").replace("-", ".")
    statuses = []
    for name, keyname, fn in HISTORY_PROVIDERS:
        if keyname and not _secret(keyname):
            statuses.append(f"{name}: no key (add {keyname} in Secrets)")
            continue
        try:
            s = fn(sym)
        except Exception:
            s = None
        if s is not None:
            src = f"{name} daily history"
            note = (f"{src} via {proxy} ({label}) — futures feed is down, so "
                    f"price LEVELS are the ETF's; every % move, signal and "
                    f"forecast mirrors the metal." if proxy else f"{src} ({sym})")
            return s, note
        statuses.append(f"{name}: denied/no data")
    return None, " · ".join(statuses)


# ---------------------------------------------------------------------------
# EOD HISTORY PROVIDERS (key-authed — the kind that works from server IPs)
# ---------------------------------------------------------------------------
# Finnhub's free tier gates /stock/candle (quotes free, candles paid — confirmed
# by the in-app diagnostic), so daily history comes from a chain of free EOD
# APIs. Each is only tried if its key exists in Secrets. Tiingo first: cleanest
# free EOD, generous limits (~1000 req/day), covers CPER/GLD/SLV and stocks.
def _secret(name: str) -> str | None:
    try:
        import streamlit as st
        k = st.secrets.get(name)  # type: ignore[attr-defined]
        if k:
            return str(k).strip()
    except Exception:
        pass
    k = os.environ.get(name)
    return k.strip() if k else None


def _http_json(url: str, timeout: float = 10.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "market-helper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def tiingo_daily(symbol: str):
    """Tiingo EOD closes (needs TIINGO_KEY). ~2y of daily adj closes or None."""
    k = _secret("TIINGO_KEY")
    if not k:
        return None
    try:
        import pandas as pd, datetime as _dt
        start = (_dt.date.today() - _dt.timedelta(days=760)).isoformat()
        d = _http_json(f"https://api.tiingo.com/tiingo/daily/{symbol}/prices"
                       f"?startDate={start}&token={k}")
        if not isinstance(d, list) or not d:
            return None
        idx = pd.to_datetime([r["date"][:10] for r in d])
        vals = [float(r.get("adjClose") or r.get("close")) for r in d]
        s = pd.Series(vals, index=idx).sort_index()
        return s if len(s) >= 60 else None
    except Exception:
        return None


def twelvedata_daily(symbol: str):
    """Twelve Data EOD closes (needs TWELVEDATA_KEY) or None."""
    k = _secret("TWELVEDATA_KEY")
    if not k:
        return None
    try:
        import pandas as pd
        d = _http_json(f"https://api.twelvedata.com/time_series?symbol={symbol}"
                       f"&interval=1day&outputsize=750&apikey={k}")
        vals = (d or {}).get("values")
        if not vals:
            return None
        idx = pd.to_datetime([r["datetime"] for r in vals])
        s = pd.Series([float(r["close"]) for r in vals], index=idx).sort_index()
        return s if len(s) >= 60 else None
    except Exception:
        return None


def alphavantage_daily(symbol: str):
    """Alpha Vantage EOD closes (needs ALPHAVANTAGE_KEY; 25 req/day cap) or None."""
    k = _secret("ALPHAVANTAGE_KEY")
    if not k:
        return None
    try:
        import pandas as pd
        d = _http_json(f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
                       f"&symbol={symbol}&outputsize=full&apikey={k}")
        ts = (d or {}).get("Time Series (Daily)")
        if not ts:
            return None
        idx = pd.to_datetime(list(ts.keys()))
        s = pd.Series([float(v["4. close"]) for v in ts.values()], index=idx).sort_index()
        return s.tail(760) if len(s) >= 60 else None
    except Exception:
        return None


HISTORY_PROVIDERS = [
    ("Finnhub candles", None, finnhub_candles),        # key already present
    ("Tiingo", "TIINGO_KEY", tiingo_daily),
    ("Twelve Data", "TWELVEDATA_KEY", twelvedata_daily),
    ("Alpha Vantage", "ALPHAVANTAGE_KEY", alphavantage_daily),
]
