#!/usr/bin/env python3
"""
copper_forecaster.py
====================
An educational, self-recalibrating copper price forecasting engine.

What it does, once per run:
  1. Pulls copper price history (10y daily + recent hourly) from Yahoo Finance.
  2. Builds features: log returns, EWMA volatility, trend.
  3. Scans news (RSS headlines) + a calendar of scheduled market-moving events
     (FOMC decisions, US copper-tariff deadlines, China policy windows, etc.)
     and turns them into (a) a directional "news pressure" score and
     (b) a volatility multiplier for windows that contain a known event.
  4. Produces probabilistic forecasts for 4 horizons: 4h, 1 day, 1 week, 1 month.
     Each forecast is a MEDIAN PATH plus 68% and 95% confidence bands (a "fan").
  5. Draws one chart per horizon and writes a plain-English summary.
  6. Sends a notification (Telegram / email / console) and fires an ALERT when a
     high-impact event is near or news pressure is extreme.
  7. Logs every forecast and, on later runs, compares past forecasts to what
     actually happened -> recalibrates its volatility and news weights. This is
     the "self-evolving" part: light, transparent online calibration.

IMPORTANT - read this:
  No model reliably predicts short-term copper prices. Not banks, not hedge
  funds, not this. The honest output here is a *range* with a confidence level,
  plus event-aware risk flags - NOT a magic point price. The point line is the
  middle of the range, not a promise. This is a learning tool. It is NOT
  financial advice and must not be used to make real trades.

Run:
  python copper_forecaster.py --once                  # one cycle (use with cron)
  python copper_forecaster.py --loop                  # internal hourly scheduler
  python copper_forecaster.py --once --scenario mideast_war   # overlay a shock
  python copper_forecaster.py --list-scenarios

Config: copy config.example.yaml -> config.yaml and fill in your settings.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Matplotlib must use a non-interactive backend for headless / scheduled runs.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ---- Optional dependencies (the core still runs without these) --------------
try:
    import yaml  # PyYAML
except Exception:
    yaml = None

try:
    import feedparser  # RSS parsing for news
except Exception:
    feedparser = None

try:
    import yfinance as yf  # price data
except Exception:
    yf = None

try:
    import requests  # telegram + generic http
except Exception:
    requests = None

# anthropic + schedule are imported lazily where used.

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "output"
LOG_PATH = HERE / "state" / "forecasts_log.csv"
EVAL_PATH = HERE / "state" / "evaluations.csv"
CALIB_PATH = HERE / "state" / "calibration.yaml"
TICKER = "HG=F"  # COMEX copper front-month, quoted in USD per pound

# =============================================================================
# 1. CONFIG
# =============================================================================

DEFAULT_CONFIG = {
    "ticker": TICKER,
    "notify": {
        "console": True,
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "",
            "password": "",
            "to": "",
        },
    },
    "anthropic": {  # optional "smart" news classifier
        "enabled": False,
        "api_key": "",
        "model": "claude-sonnet-4-6",
        "max_headlines": 12,
    },
    "alerts": {
        # Fire a loud ALERT if a high-impact event is within this many hours,
        # or if the absolute news-pressure score exceeds this threshold (0-1).
        "event_hours": 24,
        "news_pressure": 0.45,
    },
    "model": {
        # How hard to shrink the recent-return drift toward zero. 0 = ignore
        # momentum entirely (pure random walk); 1 = use it fully. Short horizons
        # should keep this small - drift is mostly noise intraday.
        "drift_shrink": 0.15,
        # How strongly a news score nudges the near-term median, expressed as a
        # fraction of one per-period standard deviation per unit of news score.
        "news_drift_strength": 0.5,
        # EWMA decay for volatility (RiskMetrics standard is 0.94 daily).
        "ewma_lambda": 0.94,
        # Lookback (in periods) used to estimate the raw drift.
        "drift_lookback": 60,
    },
    "news": {
        "rss_feeds": [
            "https://news.google.com/rss/search?q=copper+price+when:2d&hl=en-US&gl=US&ceid=US:en",
            "https://news.google.com/rss/search?q=copper+(mine+OR+tariff+OR+China+OR+supply)+when:2d&hl=en-US&gl=US&ceid=US:en",
            "https://www.mining.com/tag/copper/feed/",
        ],
        "max_items": 40,
    },
}


def load_config(path: Optional[str]) -> dict:
    cfg = _deep_copy(DEFAULT_CONFIG)
    if path and Path(path).exists():
        if yaml is None:
            print("[warn] PyYAML not installed; ignoring config file.", file=sys.stderr)
        else:
            with open(path) as fh:
                user = yaml.safe_load(fh) or {}
            cfg = _deep_merge(cfg, user)
    # Allow secrets via environment too (handy on cloud hosts).
    cfg["notify"]["telegram"]["bot_token"] = (
        os.getenv("TELEGRAM_BOT_TOKEN") or cfg["notify"]["telegram"]["bot_token"]
    )
    cfg["notify"]["telegram"]["chat_id"] = (
        os.getenv("TELEGRAM_CHAT_ID") or cfg["notify"]["telegram"]["chat_id"]
    )
    cfg["anthropic"]["api_key"] = (
        os.getenv("ANTHROPIC_API_KEY") or cfg["anthropic"]["api_key"]
    )
    return cfg


def _deep_copy(d):
    import copy
    return copy.deepcopy(d)


def _deep_merge(base: dict, over: dict) -> dict:
    out = _deep_copy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# =============================================================================
# 2. PRICE DATA + FEATURES
# =============================================================================

def fetch_prices(ticker: str) -> tuple[pd.Series, pd.Series]:
    """Return (daily_close_10y, hourly_close_60d). Raises if yfinance missing."""
    if yf is None:
        raise RuntimeError("yfinance not installed. `pip install yfinance`.")
    daily = yf.download(ticker, period="10y", interval="1d",
                        auto_adjust=False, progress=False)
    hourly = yf.download(ticker, period="60d", interval="60m",
                         auto_adjust=False, progress=False)
    d = _close_series(daily)
    h = _close_series(hourly)
    if d.empty:
        raise RuntimeError(f"No daily data returned for {ticker}.")
    return d, h


def _close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    col = "Close"
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance sometimes returns a MultiIndex (field, ticker)
        s = df["Close"].iloc[:, 0]
    else:
        s = df[col]
    s = s.dropna()
    idx = pd.to_datetime(s.index)
    # Normalize to a single tz-naive (UTC) index. Recent yfinance returns
    # tz-aware timestamps for some tickers/intervals and tz-naive for others;
    # mixing them later raises "Cannot compare tz-naive and tz-aware timestamps".
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s.index = idx
    return s.astype(float)


def ewma_vol(log_ret: pd.Series, lam: float) -> float:
    """Per-period EWMA volatility (RiskMetrics)."""
    r = log_ret.dropna().values
    if len(r) < 5:
        return float(np.std(r)) if len(r) else 0.01
    var = r[0] ** 2
    for x in r[1:]:
        var = lam * var + (1 - lam) * x * x
    return math.sqrt(max(var, 1e-12))


@dataclass
class Features:
    last_price: float
    sigma_per_period: float       # EWMA vol on this frequency
    raw_drift: float              # mean log-return over lookback
    n_obs: int


def build_features(prices: pd.Series, lam: float, drift_lookback: int) -> Features:
    log_ret = np.log(prices / prices.shift(1)).dropna()
    sigma = ewma_vol(log_ret, lam)
    raw_drift = float(log_ret.tail(drift_lookback).mean()) if len(log_ret) else 0.0
    return Features(
        last_price=float(prices.iloc[-1]),
        sigma_per_period=sigma,
        raw_drift=raw_drift,
        n_obs=len(log_ret),
    )


# =============================================================================
# 3. EVENT CALENDAR  (scheduled, known-in-advance market movers)
# =============================================================================

@dataclass
class MarketEvent:
    when: dt.datetime          # UTC
    name: str
    vol_mult: float            # how much to widen bands if this is in-window
    note: str = ""


# 2026 FOMC decision days. Statement ~14:00 ET; we approximate as 18:00 UTC.
# (Verify against federalreserve.gov; refine tz if you need precision.)
FOMC_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


def build_event_calendar(today: Optional[dt.date] = None) -> list[MarketEvent]:
    """A starter calendar of recurring, copper-relevant scheduled events.

    Dates that recur monthly (PMIs, CPI, payrolls) are APPROXIMATE placeholders.
    Replace them with exact dates from an economic calendar for best accuracy.
    """
    today = today or dt.date.today()
    year = today.year
    ev: list[MarketEvent] = []

    # --- FOMC rate decisions (biggest scheduled macro driver for copper) -----
    for ds in FOMC_2026:
        d = dt.date.fromisoformat(ds)
        ev.append(MarketEvent(
            when=dt.datetime(d.year, d.month, d.day, 18, 0),
            name="FOMC rate decision",
            vol_mult=1.45,
            note="Fed policy moves the US dollar, which moves copper inversely.",
        ))

    # --- US copper tariff review deadline (live policy risk in 2026) ---------
    ev.append(MarketEvent(
        when=dt.datetime(2026, 6, 30, 18, 0),
        name="US refined-copper tariff review deadline",
        vol_mult=1.6,
        note="Decision on a phased refined-copper import duty (possible 15% from Jan 2027).",
    ))

    # --- China policy window: 'Two Sessions' (early March) -------------------
    ev.append(MarketEvent(
        when=dt.datetime(year, 3, 5, 2, 0),
        name="China Two Sessions (NPC) policy window",
        vol_mult=1.25,
        note="China sets GDP target / stimulus signals; China is ~50%+ of copper demand.",
    ))

    # --- Recurring monthly macro (APPROXIMATE - refine these) ----------------
    for m in range(1, 13):
        # China official PMI ~ last day of month / 1st
        ev.append(MarketEvent(dt.datetime(year, m, 1, 1, 30),
                              "China manufacturing PMI", 1.2,
                              "Leading gauge of Chinese industrial copper demand."))
        # US CPI ~ mid-month
        ev.append(MarketEvent(dt.datetime(year, m, 12, 12, 30),
                              "US CPI inflation", 1.25,
                              "Inflation surprises shift Fed expectations -> USD -> copper."))
        # US ISM manufacturing ~ 1st business day
        ev.append(MarketEvent(dt.datetime(year, m, 1, 15, 0),
                              "US ISM manufacturing PMI", 1.15,
                              "Global growth proxy; copper is 'Dr. Copper' for the economy."))
        # US nonfarm payrolls ~ first Friday
        ev.append(MarketEvent(_first_friday(year, m).replace(hour=12, minute=30),
                              "US nonfarm payrolls", 1.2,
                              "Labor data steers Fed path and risk appetite."))

    # Keep only events from ~2 days ago onward (so a just-passed event can still
    # be reported), sorted chronologically.
    cutoff = dt.datetime.combine(today, dt.time()) - dt.timedelta(days=2)
    ev = [e for e in ev if e.when >= cutoff]
    ev.sort(key=lambda e: e.when)
    return ev


def _first_friday(year: int, month: int) -> dt.datetime:
    d = dt.date(year, month, 1)
    while d.weekday() != 4:  # Friday
        d += dt.timedelta(days=1)
    return dt.datetime(d.year, d.month, d.day)


def events_in_window(events: list[MarketEvent], start: dt.datetime,
                     end: dt.datetime) -> list[MarketEvent]:
    return [e for e in events if start <= e.when <= end]


# =============================================================================
# 4. NEWS  ->  directional pressure + volatility bump
# =============================================================================

# Transparent, auditable keyword map. Each phrase carries a weight; bullish
# (price-up) is positive, bearish (price-down) is negative. Edit freely.
BULLISH_TERMS = {
    "supply deficit": 1.0, "deficit": 0.6, "shortage": 0.9, "mine strike": 1.0,
    "strike": 0.6, "production cut": 0.9, "smelter cut": 0.9, "disruption": 0.7,
    "outage": 0.7, "force majeure": 1.0, "stimulus": 0.8, "rate cut": 0.8,
    "weaker dollar": 0.7, "dollar falls": 0.7, "record high": 0.6,
    "stockpiling": 0.7, "tariff": 0.6, "low inventories": 0.8,
    "data center": 0.5, "ai demand": 0.5, "energy transition": 0.4,
    "grid spending": 0.6, "electrification": 0.4, "flooding": 0.6,
    "blockade": 0.8, "protest": 0.5, "export ban": 0.9,
}
BEARISH_TERMS = {
    "surplus": 0.9, "oversupply": 0.9, "demand slump": 0.9, "recession": 0.9,
    "slowdown": 0.7, "rate hike": 0.8, "hawkish": 0.7, "stronger dollar": 0.7,
    "dollar rises": 0.7, "property crisis": 0.8, "weak demand": 0.8,
    "destocking": 0.7, "inventory build": 0.7, "rising stockpiles": 0.7,
    "sell-off": 0.6, "selloff": 0.6, "ceasefire": 0.4, "de-escalation": 0.4,
    "production restart": 0.7, "mine reopens": 0.7, "substitution": 0.5,
}


@dataclass
class NewsResult:
    score: float                       # directional pressure in [-1, 1]
    vol_bump: float                    # extra volatility multiplier from news
    headlines: list[tuple[str, float]] # (headline, contribution)
    source: str = "keyword"


def fetch_news_headlines(cfg: dict, query: Optional[str] = None) -> list[str]:
    if feedparser is None:
        return []
    if query:
        feeds = [
            f"https://news.google.com/rss/search?q={query}+price+when:2d&hl=en-US&gl=US&ceid=US:en",
            f"https://news.google.com/rss/search?q={query}+(supply+OR+demand+OR+Fed+OR+China+OR+dollar)+when:2d&hl=en-US&gl=US&ceid=US:en",
        ]
    else:
        feeds = cfg["news"]["rss_feeds"]
    items: list[str] = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                if title:
                    items.append(title)
        except Exception as exc:
            print(f"[warn] news feed failed ({url}): {exc}", file=sys.stderr)
    # de-dup, cap
    seen, out = set(), []
    for t in items:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out[: cfg["news"]["max_items"]]


def fetch_news_items(query: str, n: int = 6) -> list:
    """Recent news as {title, link, published} for on-screen display (with the
    latest first). Best-effort; returns [] if news is unavailable. Never raises."""
    if feedparser is None or not query:
        return []
    import urllib.parse
    q = urllib.parse.quote(f"{query}")
    url = (f"https://news.google.com/rss/search?q={q}+when:7d"
           f"&hl=en-US&gl=US&ceid=US:en")
    out = []
    try:
        feed = feedparser.parse(url)
        for e in feed.entries[: n * 2]:
            title = getattr(e, "title", "").strip()
            if not title:
                continue
            out.append({"title": title, "link": getattr(e, "link", ""),
                        "published": getattr(e, "published", "")})
    except Exception:
        return []
    return out[:n]


def score_headlines_keyword(headlines: list[str], bullish: dict = None,
                            bearish: dict = None) -> NewsResult:
    """Sum weighted keyword hits across headlines -> bounded score + vol bump.
    Pass per-asset term dicts; defaults to the copper/metals base."""
    bull = bullish if bullish is not None else BULLISH_TERMS
    bear = bearish if bearish is not None else BEARISH_TERMS
    contribs: list[tuple[str, float]] = []
    total = 0.0
    hits = 0
    for h in headlines:
        low = h.lower()
        s = 0.0
        for term, w in bull.items():
            if term in low:
                s += w
        for term, w in bear.items():
            if term in low:
                s -= w
        if s != 0.0:
            contribs.append((h, round(s, 2)))
            total += s
            hits += abs(s)
    # squash the net signal to [-1, 1]
    score = math.tanh(total / 4.0)
    # more loaded headlines -> more uncertainty (bounded extra 0..0.6)
    vol_bump = min(0.6, 0.06 * math.sqrt(hits))
    contribs.sort(key=lambda x: -abs(x[1]))
    return NewsResult(score=score, vol_bump=vol_bump, headlines=contribs[:8])


def score_headlines_llm(headlines: list[str], cfg: dict,
                        asset_name: str = "copper") -> Optional[NewsResult]:
    """Optional 'smart' classifier using the Anthropic API. Returns None on any
    failure so the caller can fall back to keywords."""
    ac = cfg.get("anthropic", {})
    if not ac.get("enabled") or not ac.get("api_key") or not headlines:
        return None
    try:
        import anthropic
    except Exception:
        print("[warn] anthropic not installed; using keyword scorer.", file=sys.stderr)
        return None
    try:
        client = anthropic.Anthropic(api_key=ac["api_key"])
        subset = headlines[: ac.get("max_headlines", 12)]
        numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(subset))
        prompt = (
            f"You are a {asset_name} market analyst. For the headlines below, judge "
            f"the likely NET short-term effect on the {asset_name} price.\n"
            "Reply with ONLY a JSON object, no prose:\n"
            '{"score": <float -1..1, negative=bearish, positive=bullish>, '
            '"confidence": <float 0..1>, '
            '"drivers": [<up to 4 short strings>]}\n\n'
            f"Headlines:\n{numbered}"
        )
        msg = client.messages.create(
            model=ac.get("model", "claude-sonnet-4-6"),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        import json, re
        text = re.sub(r"```json|```", "", text).strip()
        data = json.loads(text)
        score = max(-1.0, min(1.0, float(data.get("score", 0.0))))
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        drivers = [(str(d), score) for d in data.get("drivers", [])][:4]
        # higher confidence in a strong view -> modestly more vol
        vol_bump = min(0.5, 0.5 * conf * abs(score))
        return NewsResult(score=score, vol_bump=vol_bump,
                          headlines=drivers, source="anthropic")
    except Exception as exc:
        print(f"[warn] LLM news scoring failed: {exc}", file=sys.stderr)
        return None


# =============================================================================
# 5. SCENARIOS  (answer "what if X happens" - illustrative overlays)
# =============================================================================

SCENARIOS: dict[str, dict] = {
    "mideast_war": {
        "label": "Middle East war / Strait of Hormuz disruption",
        "score": -0.35, "vol_mult": 1.5,
        "why": ("Oil spikes -> inflation + growth fears -> risk-off usually weighs "
                "on copper short term, though energy-driven cost-push is a wildcard."),
    },
    "china_stimulus": {
        "label": "Large China stimulus package",
        "score": 0.55, "vol_mult": 1.3,
        "why": "China is ~50%+ of copper demand; big stimulus is strongly bullish.",
    },
    "major_mine_outage": {
        "label": "Major mine outage (e.g. Chile/Peru force majeure)",
        "score": 0.6, "vol_mult": 1.4,
        "why": "Sudden supply loss into a tight market is sharply bullish.",
    },
    "fed_hawkish_surprise": {
        "label": "Hawkish Fed surprise (rate hike / higher-for-longer)",
        "score": -0.45, "vol_mult": 1.4,
        "why": "Stronger dollar + slower growth expectations pressure copper.",
    },
    "fed_dovish_surprise": {
        "label": "Dovish Fed surprise (faster cuts)",
        "score": 0.4, "vol_mult": 1.35,
        "why": "Weaker dollar + growth hopes lift industrial metals.",
    },
    "recession_scare": {
        "label": "Global recession scare (weak PMIs)",
        "score": -0.5, "vol_mult": 1.45,
        "why": "Copper is 'Dr. Copper' - it falls hard on demand-destruction fears.",
    },
    "us_copper_tariff": {
        "label": "US imposes refined-copper import tariff",
        "score": 0.45, "vol_mult": 1.5,
        "why": "Front-loading/stockpiling and regional tightness pushed US prices up in 2025.",
    },
}


# =============================================================================
# 6. FORECAST ENGINE  (geometric random walk + shrunk drift + vol fan)
# =============================================================================

@dataclass
class HorizonForecast:
    name: str
    periods: int
    times: list[dt.datetime]
    median: np.ndarray
    lo1: np.ndarray
    hi1: np.ndarray
    lo2: np.ndarray
    hi2: np.ndarray
    last_price: float
    vol_mult: float
    news_score: float
    active_events: list[MarketEvent] = field(default_factory=list)

    @property
    def end_median(self) -> float:
        return float(self.median[-1])

    @property
    def end_lo1(self) -> float:
        return float(self.lo1[-1])

    @property
    def end_hi1(self) -> float:
        return float(self.hi1[-1])

    @property
    def end_lo2(self) -> float:
        return float(self.lo2[-1])

    @property
    def end_hi2(self) -> float:
        return float(self.hi2[-1])


def forecast_fan(
    feats: Features,
    name: str,
    periods: int,
    step: dt.timedelta,
    start_time: dt.datetime,
    cfg_model: dict,
    news_score: float,
    vol_mult: float,
    news_decay: float = 1.0,
    drift_calib: float = 1.0,
    vol_calib: float = 1.0,
    anchor_price: Optional[float] = None,
) -> HorizonForecast:
    """Build a median path and 68%/95% bands for one horizon.

    Median path: last_price * exp((mu + news_drift) * t)
      mu          = drift_shrink * raw_drift          (heavily shrunk momentum)
      news_drift  = news_strength * news_score * sigma * news_decay
    Bands: sigma_eff * sqrt(t), where sigma_eff = sigma * vol_mult * vol_calib.

    news_decay lets news matter more at short horizons (≈1) and less at long
    horizons (it gets priced in). calib terms come from the feedback loop.
    """
    sigma = feats.sigma_per_period
    mu = cfg_model["drift_shrink"] * feats.raw_drift * drift_calib
    news_drift = cfg_model["news_drift_strength"] * news_score * sigma * news_decay
    sigma_eff = sigma * vol_mult * vol_calib

    steps = np.arange(1, periods + 1, dtype=float)
    drift_term = (mu + news_drift) * steps
    band = sigma_eff * np.sqrt(steps)

    last = anchor_price if anchor_price is not None else feats.last_price
    median = last * np.exp(drift_term)
    lo1 = last * np.exp(drift_term - 1.0 * band)
    hi1 = last * np.exp(drift_term + 1.0 * band)
    lo2 = last * np.exp(drift_term - 1.96 * band)
    hi2 = last * np.exp(drift_term + 1.96 * band)

    times = [start_time + step * int(k) for k in steps]
    return HorizonForecast(
        name=name, periods=periods, times=times,
        median=median, lo1=lo1, hi1=hi1, lo2=lo2, hi2=hi2,
        last_price=last, vol_mult=vol_mult * vol_calib, news_score=news_score,
    )


# =============================================================================
# 7. CHARTS
# =============================================================================

def make_fan_chart(fc: HorizonForecast, history: pd.Series, out_path: Path,
                   hist_points: int = 40) -> Path:
    fig, ax = plt.subplots(figsize=(8, 4.2), dpi=120)

    hist = history.tail(hist_points)
    ax.plot(hist.index, hist.values, color="#444441", lw=1.4, label="History")

    t = fc.times
    # connect the last actual point to the forecast start for a clean join
    join_x = [hist.index[-1]] + t
    join_med = np.concatenate([[fc.last_price], fc.median])
    ax.fill_between([hist.index[-1]] + t,
                    np.concatenate([[fc.last_price], fc.lo2]),
                    np.concatenate([[fc.last_price], fc.hi2]),
                    color="#1D9E75", alpha=0.12, label="95% range")
    ax.fill_between([hist.index[-1]] + t,
                    np.concatenate([[fc.last_price], fc.lo1]),
                    np.concatenate([[fc.last_price], fc.hi1]),
                    color="#1D9E75", alpha=0.25, label="68% range")
    ax.plot(join_x, join_med, color="#0F6E56", lw=1.8, ls="--", label="Median path")

    ax.axhline(fc.last_price, color="#888780", lw=0.8, ls=":", alpha=0.7)
    ax.annotate(f"{fc.last_price:.3f}", xy=(hist.index[-1], fc.last_price),
                xytext=(4, 4), textcoords="offset points", fontsize=8, color="#444441")

    ax.set_title(f"Copper ({TICKER}) - {fc.name} forecast", fontsize=11)
    ax.set_ylabel("USD / lb", fontsize=9)
    ax.legend(fontsize=7, loc="upper left", framealpha=0.85)
    ax.grid(True, alpha=0.2)

    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# =============================================================================
# 8. SELF-CALIBRATION (the feedback loop)
# =============================================================================

def load_calibration() -> dict:
    default = {"vol_calib": 1.0, "drift_calib": 1.0, "news_calib": 1.0,
               "n_evaluated": 0}
    if CALIB_PATH.exists() and yaml is not None:
        try:
            with open(CALIB_PATH) as fh:
                data = yaml.safe_load(fh) or {}
            default.update(data)
        except Exception:
            pass
    return default


def save_calibration(calib: dict) -> None:
    CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if yaml is None:
        return
    with open(CALIB_PATH, "w") as fh:
        yaml.safe_dump(calib, fh)


def log_forecasts(run_time: dt.datetime, forecasts: list[HorizonForecast]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["run_time", "horizon", "target_time", "last_price",
                        "median", "lo1", "hi1", "lo2", "hi2", "news_score",
                        "evaluated"])
        for fc in forecasts:
            w.writerow([run_time.isoformat(), fc.name, fc.times[-1].isoformat(),
                        f"{fc.last_price:.5f}", f"{fc.end_median:.5f}",
                        f"{fc.end_lo1:.5f}", f"{fc.end_hi1:.5f}",
                        f"{fc.end_lo2:.5f}", f"{fc.end_hi2:.5f}",
                        f"{fc.news_score:.3f}", 0])


def evaluate_and_recalibrate(daily: pd.Series, hourly: pd.Series,
                             now: dt.datetime) -> dict:
    """Find logged forecasts whose target time has passed, compare to the actual
    price, and nudge the calibration multipliers. Transparent + bounded."""
    calib = load_calibration()
    if not LOG_PATH.exists():
        return calib
    try:
        rows = list(csv.DictReader(open(LOG_PATH)))
    except Exception:
        return calib

    combined = pd.concat([daily, hourly]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]

    in1 = in2 = signed_hits = signed_tot = newly = 0
    abs_pct_err = []
    eval_rows = []

    for r in rows:
        if r.get("evaluated") == "1":
            continue
        try:
            ttime = pd.to_datetime(r["target_time"])
        except Exception:
            continue
        if ttime > pd.Timestamp(now):
            continue  # not due yet
        # nearest actual at/after target
        after = combined[combined.index >= ttime]
        if after.empty:
            continue
        actual = float(after.iloc[0])
        last = float(r["last_price"]); med = float(r["median"])
        lo1, hi1 = float(r["lo1"]), float(r["hi1"])
        lo2, hi2 = float(r["lo2"]), float(r["hi2"])
        ns = float(r["news_score"])

        newly += 1
        if lo1 <= actual <= hi1:
            in1 += 1
        if lo2 <= actual <= hi2:
            in2 += 1
        abs_pct_err.append(abs(actual - med) / last)
        # did the news-implied direction match reality?
        if abs(ns) > 0.05:
            signed_tot += 1
            if np.sign(actual - last) == np.sign(ns):
                signed_hits += 1
        r["evaluated"] = "1"
        eval_rows.append({
            "run_time": r["run_time"], "horizon": r["horizon"],
            "target_time": r["target_time"], "actual": f"{actual:.5f}",
            "median": r["median"], "in68": int(lo1 <= actual <= hi1),
            "in95": int(lo2 <= actual <= hi2),
        })

    if newly == 0:
        return calib

    # --- volatility calibration: aim for ~68% coverage of the 68% band ------
    cov1 = in1 / newly
    if cov1 < 0.55:               # bands too tight -> widen
        calib["vol_calib"] = min(1.6, calib["vol_calib"] * 1.05)
    elif cov1 > 0.80:             # bands too wide -> tighten
        calib["vol_calib"] = max(0.7, calib["vol_calib"] * 0.97)

    # --- news calibration: scale news influence by its directional hit-rate --
    if signed_tot >= 5:
        hit = signed_hits / signed_tot
        if hit < 0.45:            # news direction worse than a coin flip -> trust it less
            calib["news_calib"] = max(0.2, calib["news_calib"] * 0.9)
        elif hit > 0.6:           # news direction adds value -> trust it more
            calib["news_calib"] = min(1.5, calib["news_calib"] * 1.05)

    calib["n_evaluated"] = int(calib.get("n_evaluated", 0)) + newly
    calib["last_coverage68"] = round(cov1, 3)
    calib["last_mean_abs_pct_err"] = round(float(np.mean(abs_pct_err)), 4)
    save_calibration(calib)

    # persist evaluations + mark log rows evaluated
    _append_evaluations(eval_rows)
    _rewrite_log_evaluated(rows)
    return calib


def _append_evaluations(eval_rows: list[dict]) -> None:
    if not eval_rows:
        return
    new_file = not EVAL_PATH.exists()
    EVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(EVAL_PATH, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(eval_rows[0].keys()))
        if new_file:
            w.writeheader()
        w.writerows(eval_rows)


def _rewrite_log_evaluated(rows: list[dict]) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(LOG_PATH, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# =============================================================================
# 9. NOTIFICATIONS
# =============================================================================

def notify(cfg: dict, subject: str, body: str, image_paths: list[Path]) -> None:
    nc = cfg["notify"]
    if nc.get("console", True):
        print("\n" + "=" * 70)
        print(subject)
        print("=" * 70)
        print(body)
        print(f"[charts saved to: {OUT_DIR}]")
    if nc.get("telegram", {}).get("enabled"):
        _notify_telegram(nc["telegram"], subject, body, image_paths)
    if nc.get("email", {}).get("enabled"):
        _notify_email(nc["email"], subject, body, image_paths)


def _notify_telegram(tc: dict, subject: str, body: str,
                     image_paths: list[Path]) -> None:
    if requests is None:
        print("[warn] requests not installed; cannot send Telegram.", file=sys.stderr)
        return
    token, chat = tc.get("bot_token"), tc.get("chat_id")
    if not token or not chat:
        print("[warn] Telegram enabled but bot_token/chat_id missing.", file=sys.stderr)
        return
    base = f"https://api.telegram.org/bot{token}"
    text = f"*{_md(subject)}*\n\n{_md(body)}"
    try:
        requests.post(f"{base}/sendMessage",
                      data={"chat_id": chat, "text": text[:4000],
                            "parse_mode": "Markdown"}, timeout=20)
        for p in image_paths:
            with open(p, "rb") as fh:
                requests.post(f"{base}/sendPhoto",
                              data={"chat_id": chat}, files={"photo": fh},
                              timeout=60)
    except Exception as exc:
        print(f"[warn] Telegram send failed: {exc}", file=sys.stderr)


def _md(s: str) -> str:
    for ch in r"_*`[":
        s = s.replace(ch, "\\" + ch)
    return s


def _notify_email(ec: dict, subject: str, body: str,
                  image_paths: list[Path]) -> None:
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ec.get("username", "")
    msg["To"] = ec.get("to", "")
    msg.set_content(body)
    for p in image_paths:
        try:
            with open(p, "rb") as fh:
                msg.add_attachment(fh.read(), maintype="image",
                                   subtype="png", filename=p.name)
        except Exception:
            pass
    try:
        with smtplib.SMTP(ec["smtp_host"], int(ec["smtp_port"])) as s:
            s.starttls()
            s.login(ec["username"], ec["password"])
            s.send_message(msg)
    except Exception as exc:
        print(f"[warn] Email send failed: {exc}", file=sys.stderr)


# =============================================================================
# 10. SUMMARY TEXT
# =============================================================================

def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def build_summary(forecasts: list[HorizonForecast], news: NewsResult,
                  upcoming: list[MarketEvent], calib: dict,
                  scenario: Optional[dict], now: dt.datetime) -> tuple[str, bool]:
    last = forecasts[0].last_price
    lines = []
    lines.append(f"Copper (HG=F) now: {last:.3f} USD/lb   |   {now:%Y-%m-%d %H:%M} UTC")
    lines.append("")
    tone = ("bullish" if news.score > 0.15 else
            "bearish" if news.score < -0.15 else "neutral")
    lines.append(f"News pressure: {news.score:+.2f}  ({tone}, via {news.source})")
    if news.headlines:
        for h, c in news.headlines[:4]:
            arrow = "▲" if c > 0 else "▼" if c < 0 else "•"
            lines.append(f"   {arrow} {h}")
    if scenario:
        lines.append("")
        lines.append(f"SCENARIO OVERLAY: {scenario['label']}")
        lines.append(f"   {scenario['why']}")
    lines.append("")
    lines.append("Forecast ranges (median, then 68% band, then 95% band):")
    for fc in forecasts:
        lines.append(
            f"   {fc.name:<8} median {fc.end_median:.3f} "
            f"({pct(fc.end_median, last):+.1f}%)  |  "
            f"68% {fc.end_lo1:.3f}–{fc.end_hi1:.3f}  |  "
            f"95% {fc.end_lo2:.3f}–{fc.end_hi2:.3f}"
        )
    # alert logic
    alert = False
    if upcoming:
        lines.append("")
        lines.append("Upcoming scheduled events (next 14 days):")
        for e in upcoming[:6]:
            hrs = (e.when - now).total_seconds() / 3600.0
            lines.append(f"   • {e.when:%b %d %H:%M}  {e.name}  (in {hrs:.0f}h)")
    if abs(news.score) >= 0.45:
        alert = True
    lines.append("")
    lines.append("Model calibration (learned from past forecasts): "
                 f"vol×{calib.get('vol_calib',1):.2f}, "
                 f"news×{calib.get('news_calib',1):.2f}, "
                 f"evaluated={calib.get('n_evaluated',0)} forecasts; "
                 f"last 68%-band hit rate={calib.get('last_coverage68','n/a')}")
    lines.append("")
    lines.append("NOTE: ranges, not predictions. Educational only - not financial advice.")
    return "\n".join(lines), alert


# =============================================================================
# 11. ORCHESTRATION
# =============================================================================

# (periods, step, news_decay) per horizon.  Intraday horizons use hourly data;
# week/month use daily data.
def run_once(cfg: dict, scenario_key: Optional[str] = None) -> None:
    now = dt.datetime.utcnow().replace(microsecond=0)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] running copper_forecaster ...")

    daily, hourly = fetch_prices(cfg["ticker"])
    have_hourly = len(hourly) > 30

    # ----- features on each frequency -----
    m = cfg["model"]
    feats_daily = build_features(daily, m["ewma_lambda"], m["drift_lookback"])
    feats_hourly = (build_features(hourly, m["ewma_lambda"], m["drift_lookback"])
                    if have_hourly else feats_daily)

    # ----- feedback: evaluate past forecasts, update calibration -----
    calib = evaluate_and_recalibrate(daily, hourly, now)

    # ----- news -----
    headlines = fetch_news_headlines(cfg)
    news = score_headlines_llm(headlines, cfg) or score_headlines_keyword(headlines)

    # ----- scenario overlay (optional) -----
    scenario = SCENARIOS.get(scenario_key) if scenario_key else None
    scen_score = scenario["score"] if scenario else 0.0
    scen_vol = scenario["vol_mult"] if scenario else 1.0
    combined_score = max(-1.0, min(1.0, news.score * calib.get("news_calib", 1.0)
                                   + scen_score))

    # ----- events -----
    events = build_event_calendar(now.date())
    upcoming = [e for e in events if e.when >= now][:8]

    horizons = [
        # name,        periods, step,                    freq,   news_decay
        ("4 hour",     4,  dt.timedelta(hours=1),  "hourly", 1.0),
        ("Next day",   24, dt.timedelta(hours=1),  "hourly", 0.8),
        ("Next week",  5,  dt.timedelta(days=1),   "daily",  0.5),
        ("Next month", 21, dt.timedelta(days=1),   "daily",  0.3),
    ]

    forecasts: list[HorizonForecast] = []
    spot = float(hourly.iloc[-1]) if have_hourly else float(daily.iloc[-1])
    for name, periods, step, freq, decay in horizons:
        feats = feats_hourly if (freq == "hourly" and have_hourly) else feats_daily
        end = now + step * periods
        ev_window = events_in_window(events, now, end)
        ev_mult = max([1.0] + [e.vol_mult for e in ev_window])
        # A single scheduled event is a short-lived vol spike, not month-long
        # elevated vol, so taper the event + news vol bumps by horizon.
        event_component = 1.0 + (ev_mult - 1.0) * decay
        news_component = 1.0 + news.vol_bump * decay
        vol_mult = event_component * news_component * scen_vol
        fc = forecast_fan(
            feats=feats, name=name, periods=periods, step=step, start_time=now,
            cfg_model=m, news_score=combined_score, vol_mult=vol_mult,
            news_decay=decay, drift_calib=calib.get("drift_calib", 1.0),
            vol_calib=calib.get("vol_calib", 1.0), anchor_price=spot,
        )
        fc.active_events = ev_window
        forecasts.append(fc)

    # ----- charts -----
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    image_paths = []
    for fc, (_, _, _, freq, _) in zip(forecasts, horizons):
        hist = hourly if (freq == "hourly" and have_hourly) else daily
        slug = fc.name.lower().replace(" ", "_")
        path = OUT_DIR / f"copper_{slug}.png"
        make_fan_chart(fc, hist, path,
                       hist_points=48 if freq == "hourly" else 60)
        image_paths.append(path)

    # ----- summary + notify -----
    summary, news_alert = build_summary(forecasts, news, upcoming, calib,
                                        scenario, now)
    # event alert: any high-impact event within configured window?
    ev_hours = cfg["alerts"]["event_hours"]
    near_big = [e for e in upcoming
                if (e.when - now).total_seconds() / 3600.0 <= ev_hours
                and e.vol_mult >= 1.3]
    alert = news_alert or bool(near_big)
    subject = ("⚠ COPPER ALERT — " if alert else "Copper forecast — ") + \
              f"{now:%Y-%m-%d %H:%M} UTC"
    if near_big:
        summary += "\n\nALERT: high-impact event within " \
                   f"{ev_hours}h -> expect bigger swings: " \
                   + ", ".join(e.name for e in near_big)

    # ----- log this run's forecasts for future evaluation -----
    log_forecasts(now, forecasts)

    notify(cfg, subject, summary, image_paths)
    print(f"[{dt.datetime.utcnow():%H:%M:%S}] done.")


def run_loop(cfg: dict, scenario_key: Optional[str]) -> None:
    try:
        import schedule
    except Exception:
        print("`schedule` not installed. Either `pip install schedule` or use "
              "--once with cron / Task Scheduler (recommended).", file=sys.stderr)
        sys.exit(1)
    run_once(cfg, scenario_key)  # run immediately, then hourly
    schedule.every(1).hours.do(run_once, cfg=cfg, scenario_key=scenario_key)
    print("Scheduler started: running every hour. Ctrl+C to stop.")
    import time
    while True:
        schedule.run_pending()
        time.sleep(30)


# =============================================================================
# 12. CLI
# =============================================================================

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=str(HERE / "config.yaml"),
                   help="path to config.yaml")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="run a single cycle")
    g.add_argument("--loop", action="store_true",
                   help="run continuously, every hour (internal scheduler)")
    p.add_argument("--scenario", default=None,
                   help="overlay a what-if shock (see --list-scenarios)")
    p.add_argument("--list-scenarios", action="store_true")
    args = p.parse_args(argv)

    if args.list_scenarios:
        print("Available scenarios:\n")
        for k, v in SCENARIOS.items():
            print(f"  {k:<22} {v['label']}")
            print(f"  {'':22} -> {v['why']}\n")
        return

    cfg = load_config(args.config)

    if args.scenario and args.scenario not in SCENARIOS:
        print(f"Unknown scenario '{args.scenario}'. Use --list-scenarios.",
              file=sys.stderr)
        sys.exit(1)

    if args.loop:
        run_loop(cfg, args.scenario)
    else:
        run_once(cfg, args.scenario)  # default to one cycle


if __name__ == "__main__":
    main()
