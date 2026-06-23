"""indicators.py — classic technical indicators + plain-English readings.

All computed from the close price, so they work the same on metals and stocks.
Each indicator returns a signal in {-1, 0, +1} and a one-line human explanation.
These feed three things: a simple on-screen readout, the strategy backtest
(so we MEASURE whether they worked), and the conviction gate (a STRONG call
requires several of them to agree with the forecast).

Honest framing baked in: trend/momentum indicators have the strongest evidence;
RSI/Bollinger are mean-reversion signals best in range-bound markets. None is a
crystal ball — agreement across several is the high-probability setup.
"""
from __future__ import annotations
import pandas as pd


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    roll_up = up.ewm(alpha=1.0 / n, adjust=False).mean()
    roll_dn = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = roll_up / roll_dn.replace(0.0, 1e-12)
    return 100.0 - 100.0 / (1.0 + rs)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal          # macd, signal, histogram


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    upper = mid + k * sd
    lower = mid - k * sd
    width = (upper - lower)
    pctB = (close - lower) / width.replace(0.0, 1e-12)
    return mid, upper, lower, pctB


def compute_indicators(close: pd.Series) -> dict:
    """Return current readings, per-indicator signals + plain text, and a tally."""
    c = close.dropna()
    items: list[dict] = []
    n = len(c)
    price = float(c.iloc[-1])

    e50 = ema(c, 50)
    e200 = ema(c, 200)
    if n >= 205:
        cross = 1 if e50.iloc[-1] > e200.iloc[-1] else -1
        items.append({"key": "trend_cross", "name": "Trend (50 vs 200-day)",
                      "signal": cross,
                      "text": ("golden cross — the 50-day average is above the "
                               "200-day, an uptrend" if cross > 0 else
                               "death cross — the 50-day average is below the "
                               "200-day, a downtrend")})
        vs200 = 1 if price > float(e200.iloc[-1]) else -1
        items.append({"key": "vs200", "name": "Long-term trend (200-day)",
                      "signal": vs200,
                      "text": ("price is above its 200-day average — bullish "
                               "backdrop" if vs200 > 0 else
                               "price is below its 200-day average — bearish "
                               "backdrop")})

    if n >= 20:
        rv = float(rsi(c, 14).iloc[-1])
        rsig = 1 if rv < 30 else -1 if rv > 70 else 0
        items.append({"key": "rsi", "name": f"RSI ({rv:.0f})", "signal": rsig,
                      "text": ("oversold (below 30) — often due for a bounce"
                               if rsig > 0 else
                               "overbought (above 70) — may be due for a pullback"
                               if rsig < 0 else
                               "neutral (between 30 and 70) — no extreme")})

    if n >= 35:
        _, _, hist = macd(c)
        hv = float(hist.iloc[-1])
        msig = 1 if hv > 0 else -1 if hv < 0 else 0
        items.append({"key": "macd", "name": "MACD momentum", "signal": msig,
                      "text": ("momentum is positive — trend pointing up"
                               if msig > 0 else
                               "momentum is negative — trend pointing down"
                               if msig < 0 else "momentum flat")})

    if n >= 20:
        _, _, _, pb = bollinger(c)
        pbv = float(pb.iloc[-1])
        bsig = 1 if pbv < 0 else -1 if pbv > 1 else 0
        items.append({"key": "boll", "name": "Bollinger position", "signal": bsig,
                      "text": ("below the lower band — stretched down, bounce "
                               "risk" if bsig > 0 else
                               "above the upper band — stretched up, pullback "
                               "risk" if bsig < 0 else
                               "inside its normal range")})

    bull = sum(1 for s in items if s["signal"] > 0)
    bear = sum(1 for s in items if s["signal"] < 0)
    neutral = sum(1 for s in items if s["signal"] == 0)
    return {"list": items, "bull": bull, "bear": bear, "neutral": neutral,
            "net": bull - bear,
            "rsi": float(rsi(c, 14).iloc[-1]) if n >= 20 else None,
            "ema50": float(e50.iloc[-1]) if n else None,
            "ema200": float(e200.iloc[-1]) if n >= 205 else None}


def agreement_with(items: list[dict], lean: str) -> tuple[int, int]:
    """How many indicators agree / disagree with a BUY or SELL lean."""
    sign = 1 if lean == "BUY" else -1 if lean == "SELL" else 0
    if sign == 0:
        return 0, 0
    agree = sum(1 for s in items if s["signal"] == sign)
    disagree = sum(1 for s in items if s["signal"] == -sign)
    return agree, disagree
