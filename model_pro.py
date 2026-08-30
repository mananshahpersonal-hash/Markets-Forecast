#!/usr/bin/env python3
"""
model_pro.py
============
Upgraded copper forecasting engine. Reuses the news / event calendar / scenario
machinery from copper_forecaster.py and replaces the single-model fan with an
ENSEMBLE plus honest, measured uncertainty:

  * Ensemble of drift components (the M5 lesson: combining beats any single model)
      - random walk        (zero-drift anchor)
      - AR(1) mean reversion (price pulled toward its recent average)
      - momentum            (shrunk recent trend)
      - US-dollar signal    (copper moves inversely to the dollar index)
      - news / event tilt   (from copper_forecaster's news scorer)
      - LightGBM (optional) (the M5-winning method; used if installed)
  * GARCH(1,1) volatility (proper time-varying vol) with an EWMA fallback.
  * Empirical / conformal-style bands built from copper's ACTUAL historical
    move distribution -> fat-tail aware, not a normal-curve fantasy.
  * Walk-forward backtest that MEASURES directional accuracy + band coverage,
    so you see how good (and how imperfect) the model really is.

Anchoring: all forecasts start from a price you supply (your HGU26 print),
while volatility / correlations / shape come from real HG=F history.

NOT financial advice. Educational. No model predicts copper to ~100%.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import warnings
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import copper_forecaster as cf  # reuse news, events, scenarios, config
import assets  # per-asset profiles (copper, gold, silver, aluminium)
import storage  # optional persistent sync of the learning state (gist-backed)

# Safety net: if a deployed storage.py is out of date and missing a function the
# engine calls (push/pull/delete/…), replace it with a harmless no-op instead of
# crashing the whole prediction. The app still runs; only cloud-sync is affected.
for _sfn in ("pull", "push", "delete", "delete_all", "configured", "backend_name"):
    if not hasattr(storage, _sfn):
        setattr(storage, _sfn, (lambda *a, **k: 0))
import indicators  # classic technical indicators (EMA/RSI/MACD/Bollinger)

# Bump this whenever app.py starts depending on new functions here. app.py
# checks for the capabilities below and shows a friendly message if this file
# is an older copy than app.py (the #1 cause of deploy errors).
BUILD = "v35 · 2026-08-30 · Finnhub live prices + dividends/earnings, Yahoo auto-fallback"

warnings.filterwarnings("ignore")

QUANTILES = [0.025, 0.16, 0.5, 0.84, 0.975]

STATE_DIR = cf.HERE / "state"


def _pred_log(asset: str):
    return STATE_DIR / f"{asset}_predictions.csv"


def _eval_log(asset: str):
    return STATE_DIR / f"{asset}_evaluations.csv"


def _calib_path(asset: str):
    return STATE_DIR / f"{asset}_calibration.json"


def fmtp(p) -> str:
    """Format a price with sensible precision for the metal's magnitude."""
    p = float(p)
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 10:
        return f"{p:.3f}"
    return f"{p:.4f}"


# =============================================================================
# DATA
# =============================================================================

_DOLLAR_CACHE = {"tk": None}


def fetch_dollar_index() -> pd.Series:
    """Daily US Dollar Index with fallbacks. Yahoo killed DX=F, so we try the
    ICE index (DX-Y.NYB) first, then the UUP dollar ETF, then DX=F as a last
    resort — and remember which one worked so we stop hammering dead symbols."""
    if cf.yf is None:
        return pd.Series(dtype=float)
    order = [t for t in (_DOLLAR_CACHE["tk"], "DX-Y.NYB", "UUP", "DX=F") if t]
    seen = set()
    order = [t for t in order if not (t in seen or seen.add(t))]
    for tk in order:
        try:
            df = cf.yf.download(tk, period="3y", interval="1d",
                                auto_adjust=False, progress=False)
            s = cf._close_series(df)
            if s is not None and not s.empty:
                _DOLLAR_CACHE["tk"] = tk
                return s
        except Exception:
            continue
    return pd.Series(dtype=float)


def fetch_reference_series(profile: dict) -> pd.Series:
    """Daily macro reference for the asset: the US dollar index for metals, or
    a broad-market ETF (SPY) for stocks. Returns empty series on failure."""
    tk = profile.get("ref_ticker", "DX=F")
    if tk in ("DX=F", "DX-Y.NYB"):
        return fetch_dollar_index()
    if cf.yf is None:
        return pd.Series(dtype=float)
    try:
        df = cf.yf.download(tk, period="3y", interval="1d",
                            auto_adjust=False, progress=False)
        return cf._close_series(df)
    except Exception:
        return pd.Series(dtype=float)


# =============================================================================
# VOLATILITY  (GARCH with EWMA fallback)
# =============================================================================

def vol_per_period(log_ret: pd.Series, lam: float) -> tuple[float, str]:
    """Return (one-step-ahead sigma, method_label)."""
    r = log_ret.dropna()
    if len(r) < 60:
        return cf.ewma_vol(r, lam), "ewma"
    try:
        from arch import arch_model
        am = arch_model(r.values * 100.0, vol="Garch", p=1, q=1, mean="Zero",
                        dist="t")
        res = am.fit(disp="off")
        fc = res.forecast(horizon=1, reindex=False)
        sigma = math.sqrt(float(fc.variance.values[-1, 0])) / 100.0
        return max(sigma, 1e-5), "garch"
    except Exception:
        return cf.ewma_vol(r, lam), "ewma"


# =============================================================================
# ENSEMBLE DRIFT COMPONENTS  (all in per-period log-return units)
# =============================================================================

def ar1_reversion_drift(log_price: pd.Series, sigma: float,
                        window: int = 60) -> float:
    """Per-period expected log-return from mean reversion toward a rolling mean.
    Estimates how strongly deviations from the recent average get pulled back."""
    lp = log_price.dropna()
    if len(lp) < window + 5:
        return 0.0
    mean = lp.rolling(window).mean()
    dev = (lp - mean).dropna()
    if len(dev) < 20:
        return 0.0
    x = dev.shift(1).dropna()
    y = dev.loc[x.index]
    if x.std() < 1e-9:
        return 0.0
    phi = float(np.cov(x, y)[0, 1] / np.var(x))      # AR(1) on deviations
    phi = max(min(phi, 0.99), -0.5)
    current_dev = float(dev.iloc[-1])
    drift = (phi - 1.0) * current_dev                # pull toward the mean
    return float(np.clip(drift, -1.2 * sigma, 1.2 * sigma))


def momentum_drift(log_ret: pd.Series, sigma: float, span: int = 20,
                   shrink: float = 0.2) -> float:
    r = log_ret.dropna()
    if len(r) < span:
        return 0.0
    mom = float(r.ewm(span=span).mean().iloc[-1]) * shrink
    return float(np.clip(mom, -sigma, sigma))


def trend_drift(log_price: pd.Series, sigma: float, lookback: int = 63,
                scale: float = 0.55) -> tuple[float, float]:
    """Time-series momentum (the signal with the strongest academic support in
    commodities/equities): follow the medium-term trend. Returns (per-period
    drift in the trend direction, a regime-strength score in [-1, 1])."""
    lp = log_price.dropna()
    if len(lp) < lookback + 5:
        return 0.0, 0.0
    avg_daily = float((lp.iloc[-1] - lp.iloc[-lookback]) / lookback)   # avg daily log-return
    strength = float(np.tanh(avg_daily / (sigma + 1e-9)))             # how strong/steady
    drift = avg_daily * scale
    return float(np.clip(drift, -1.5 * sigma, 1.5 * sigma)), strength


def dollar_drift(copper_ret: pd.Series, dxy_ret: pd.Series, sigma: float,
                 lookback: int = 20) -> tuple[float, float]:
    """(per-period copper drift implied by the recent dollar trend, beta).
    Copper ~ inversely related to the dollar, so beta is usually negative."""
    if dxy_ret is None or dxy_ret.empty:
        return 0.0, 0.0
    try:
        c = copper_ret.copy(); c.index = pd.DatetimeIndex(c.index).normalize()
        d = dxy_ret.copy(); d.index = pd.DatetimeIndex(d.index).normalize()
        df = pd.concat([c.rename("c"), d.rename("d")], axis=1).dropna()
        if len(df) < 60 or df["d"].std() < 1e-9:
            return 0.0, 0.0
        beta = float(np.cov(df["c"], df["d"])[0, 1] / np.var(df["d"]))
        recent_dxy = float(df["d"].tail(lookback).mean())   # recent daily $ trend
        drift = beta * recent_dxy
        return float(np.clip(drift, -1.5 * sigma, 1.5 * sigma)), beta
    except Exception:
        return 0.0, 0.0      # never let the dollar signal crash the run


def lgbm_drift(log_price: pd.Series, dxy: Optional[pd.Series],
               sigma: float) -> Optional[float]:
    """Optional: the M5-winning method. Predict next-day return from lag/calendar
    features. Returns a per-period drift, or None if lightgbm isn't installed."""
    try:
        import lightgbm as lgb
    except Exception:
        return None
    lp = log_price.dropna()
    if len(lp) < 300:
        return None
    ret = lp.diff().dropna()
    feat = pd.DataFrame(index=ret.index)
    for k in (1, 2, 3, 5, 10):
        feat[f"lag{k}"] = ret.shift(k)
    feat["roll5"] = ret.rolling(5).mean()
    feat["roll20"] = ret.rolling(20).mean()
    feat["vol10"] = ret.rolling(10).std()
    feat["dow"] = feat.index.dayofweek
    feat["month"] = feat.index.month
    # technical-indicator features (proven trend/momentum signals)
    try:
        price = np.exp(lp)
        feat["rsi"] = indicators.rsi(price, 14).reindex(feat.index)
        _, _, _mh = indicators.macd(price)
        feat["macd_hist"] = _mh.reindex(feat.index)
        _e50 = indicators.ema(price, 50)
        _e200 = indicators.ema(price, 200)
        feat["ema_gap"] = ((_e50 - _e200) / _e200).reindex(feat.index)
    except Exception:
        pass
    if dxy is not None and not dxy.empty:
        dret = np.log(dxy / dxy.shift(1))
        feat["dxy1"] = dret.reindex(feat.index).shift(1)
        feat["dxy5"] = dret.reindex(feat.index).rolling(5).mean()
    target = ret
    data = feat.join(target.rename("y")).dropna()
    if len(data) < 200:
        return None
    X, y = data.drop(columns="y"), data["y"]
    try:
        model = lgb.LGBMRegressor(n_estimators=200, max_depth=4,
                                  learning_rate=0.03, subsample=0.8,
                                  colsample_bytree=0.8, min_child_samples=30,
                                  verbosity=-1)
        model.fit(X.iloc[:-1], y.iloc[:-1])
        pred = float(model.predict(X.iloc[[-1]])[0])
        return float(np.clip(pred, -2 * sigma, 2 * sigma))
    except Exception:
        return None


DEFAULT_WEIGHTS = {
    "reversion": 0.35,     # was 0.55 — it was fighting trends and getting run over
    "momentum": 0.20,
    "trend": 0.45,         # NEW: follow the medium-term trend (best academic support)
    "dollar": 0.30,
    "news": 0.50,
    "lgbm": 0.40,
}


def ensemble_per_period_drift(
    log_price: pd.Series, log_ret: pd.Series, sigma: float,
    dxy: Optional[pd.Series], news_score: float, news_decay: float,
    weights: dict, use_lgbm: bool = True,
) -> tuple[float, dict]:
    """Combine drift components -> one per-period drift + a breakdown dict."""
    dret = np.log(dxy / dxy.shift(1)) if (dxy is not None and not dxy.empty) else None
    rev = ar1_reversion_drift(log_price, sigma)
    mom = momentum_drift(log_ret, sigma)
    tr, trend_str = trend_drift(log_price, sigma)
    # REGIME GUARD: don't let mean-reversion bet against a strong prevailing trend.
    # If reversion points opposite the trend and the trend is strong, shrink it.
    if trend_str != 0 and rev != 0 and (np.sign(rev) != np.sign(trend_str)):
        rev *= max(0.0, 1.0 - 2.0 * abs(trend_str))  # kill reversion that fights a clear trend
    dol, beta = dollar_drift(log_ret, dret, sigma) if dret is not None else (0.0, 0.0)
    news = cf.DEFAULT_CONFIG["model"]["news_drift_strength"] * news_score * sigma * news_decay
    lgb_d = lgbm_drift(log_price, dxy, sigma) if use_lgbm else None

    parts = {
        "reversion": weights["reversion"] * rev,
        "momentum": weights["momentum"] * mom,
        "trend": weights.get("trend", 0.45) * tr,
        "dollar": weights["dollar"] * dol,
        "news": weights["news"] * news,
    }
    if lgb_d is not None:
        parts["lgbm"] = weights["lgbm"] * lgb_d
    total = float(sum(parts.values()))
    total = float(np.clip(total, -2.5 * sigma, 2.5 * sigma))  # safety
    parts["_beta_dollar"] = beta
    parts["_trend_strength"] = trend_str
    parts["_raw"] = {"reversion": rev, "momentum": mom, "trend": tr, "dollar": dol,
                     "news": news, "lgbm": lgb_d}
    return total, parts


# =============================================================================
# EMPIRICAL / CONFORMAL BANDS
# =============================================================================

def empirical_offsets(log_price: pd.Series, h: int) -> Optional[dict]:
    """Quantiles of copper's actual h-step log-return distribution, centered
    (median removed) so they describe SPREAD around a model's drift."""
    lp = log_price.dropna()
    if len(lp) < h + 50:
        return None
    rh = (lp - lp.shift(h)).dropna()
    if len(rh) < 30:
        return None
    qs = {q: float(rh.quantile(q)) for q in QUANTILES}
    med = qs[0.5]
    return {q: qs[q] - med for q in QUANTILES}      # centered offsets


# =============================================================================
# FORECAST CONTAINER + BUILDER
# =============================================================================

@dataclass
class HF:
    name: str
    times: list
    spot: float
    median: np.ndarray
    lo68: np.ndarray
    hi68: np.ndarray
    lo95: np.ndarray
    hi95: np.ndarray
    drift_per_period: float
    vol_per_period: float
    vol_method: str
    vol_mult: float
    components: dict
    p_up: float = 0.5
    events: list = field(default_factory=list)

    def _end(self, a):
        return float(a[-1])


def build_horizon(
    name: str, periods: int, step: dt.timedelta, start: dt.datetime, spot: float,
    log_price: pd.Series, log_ret: pd.Series, lam: float,
    dxy: Optional[pd.Series], news_score: float, news_decay: float,
    vol_mult: float, weights: dict, use_lgbm: bool,
    bias_per_period: float = 0.0, vol_calib: float = 1.0,
) -> HF:
    sigma, vmethod = vol_per_period(log_ret, lam)
    drift, comp = ensemble_per_period_drift(
        log_price, log_ret, sigma, dxy, news_score, news_decay, weights, use_lgbm)
    # apply the learned bias correction (from grading past predictions)
    drift = float(np.clip(drift + bias_per_period, -3 * sigma, 3 * sigma))

    steps = np.arange(1, periods + 1, dtype=float)
    cum_drift = drift * steps
    median = spot * np.exp(cum_drift)

    vm = vol_mult * vol_calib                 # learned volatility calibration
    off = empirical_offsets(log_price, max(periods, 1))
    if off is not None:
        scale = np.sqrt(steps / periods) * vm
        lo95 = spot * np.exp(cum_drift + off[0.025] * scale)
        lo68 = spot * np.exp(cum_drift + off[0.16] * scale)
        hi68 = spot * np.exp(cum_drift + off[0.84] * scale)
        hi95 = spot * np.exp(cum_drift + off[0.975] * scale)
    else:
        band = sigma * np.sqrt(steps) * vm
        lo95 = spot * np.exp(cum_drift - 1.96 * band)
        lo68 = spot * np.exp(cum_drift - 1.0 * band)
        hi68 = spot * np.exp(cum_drift + 1.0 * band)
        hi95 = spot * np.exp(cum_drift + 1.96 * band)

    times = [start + step * int(k) for k in steps]

    # Probability the price ends ABOVE the current price at the final step.
    # Use copper's actual h-step return distribution, recentred on the model's
    # drift; fall back to a normal approximation if history is too short.
    cum_end = float(cum_drift[-1])
    lp = log_price.dropna()
    p_up = 0.5
    if len(lp) >= periods + 50:
        rh = (lp - lp.shift(periods)).dropna().values
        if len(rh) >= 30:
            shifted = (rh - np.median(rh)) * vm + cum_end
            p_up = float(np.mean(shifted > 0.0))
    else:
        vol_h = sigma * math.sqrt(periods) * vm
        if vol_h > 1e-9:
            p_up = float(0.5 * (1 + math.erf((cum_end / vol_h) / math.sqrt(2))))

    return HF(name, times, spot, median, lo68, hi68, lo95, hi95,
              drift, sigma, vmethod, vm, comp, p_up=p_up)


# =============================================================================
# WALK-FORWARD BACKTEST  (the honest accuracy measurement)
# =============================================================================

def backtest(log_price: pd.Series, h: int, n_test: int = 250) -> dict:
    """Walk forward: at each past point, forecast h steps using ONLY prior data,
    then compare to what actually happened. Reports real accuracy."""
    lp = log_price.dropna()
    if len(lp) < h + 200:
        return {}
    n_test = min(n_test, len(lp) - h - 150)
    in68 = in95 = dir_ok = dir_tot = n = 0
    ape = []
    for i in range(len(lp) - h - n_test, len(lp) - h):
        hist = lp.iloc[: i + 1]
        spot = float(np.exp(hist.iloc[-1]))
        ret = hist.diff().dropna()
        sigma, _ = ("", "")
        sigma = cf.ewma_vol(ret, 0.94)               # cheap vol for speed
        drift = (ar1_reversion_drift(hist, sigma) * DEFAULT_WEIGHTS["reversion"]
                 + momentum_drift(ret, sigma) * DEFAULT_WEIGHTS["momentum"])
        off = empirical_offsets(hist, h)
        cum = drift * h
        med = spot * math.exp(cum)
        actual = float(np.exp(lp.iloc[i + h]))
        if off is not None:
            lo95 = spot * math.exp(cum + off[0.025])
            lo68 = spot * math.exp(cum + off[0.16])
            hi68 = spot * math.exp(cum + off[0.84])
            hi95 = spot * math.exp(cum + off[0.975])
        else:
            b = sigma * math.sqrt(h)
            lo95, lo68 = spot * math.exp(cum - 1.96 * b), spot * math.exp(cum - b)
            hi68, hi95 = spot * math.exp(cum + b), spot * math.exp(cum + 1.96 * b)
        n += 1
        in68 += lo68 <= actual <= hi68
        in95 += lo95 <= actual <= hi95
        ape.append(abs(actual - med) / spot)
        if abs(cum) > 1e-6:
            dir_tot += 1
            dir_ok += np.sign(actual - spot) == np.sign(cum)
    if n == 0:
        return {}
    return {
        "n": n,
        "coverage68": round(in68 / n, 3),
        "coverage95": round(in95 / n, 3),
        "mape": round(float(np.mean(ape)) * 100, 2),
        "directional_acc": round(dir_ok / dir_tot, 3) if dir_tot else None,
    }


# =============================================================================
# P&L BACKTEST: would trading the signals actually have made money? (with costs)
# =============================================================================

def pnl_backtest(log_d: pd.Series, h: int = 5, z_thresh: float = 0.5,
                 contract_lbs: int = 2500, spread: float = 0.0010,
                 commission_rt: float = 2.50, start_capital: float = 10000.0,
                 n_contracts: int = 1, warmup: int = 200) -> dict:
    """Walk forward; at each non-overlapping h-day block, take the model's
    price-based signal (long / short / flat) using ONLY prior data, hold h days,
    and book the result minus the bid-ask spread and commission. Honest because
    it has no lookahead and pays real-world costs.

    Note: this trades the price-based CORE signal (reversion+momentum) — the part
    that can be replayed on history. The live signal also uses news and the
    dollar, which can't be backtested without a historical news feed.
    """
    lp = log_d.dropna()
    p = np.exp(lp.values)
    if len(p) < h + warmup + 10:
        return {}
    equity = start_capital
    peak = equity
    maxdd = 0.0
    blew_up = False
    trades = wins = 0
    cost_sum = 0.0
    nets = []
    i = warmup
    while i + h < len(p):
        hist = lp.iloc[: i + 1]
        r = hist.diff().dropna()
        sigma = cf.ewma_vol(r, 0.94)
        drift = (ar1_reversion_drift(hist, sigma) * DEFAULT_WEIGHTS["reversion"]
                 + momentum_drift(r, sigma) * DEFAULT_WEIGHTS["momentum"])
        vol_h = sigma * math.sqrt(h)
        z = (drift * h) / vol_h if vol_h > 1e-9 else 0.0
        direction = 1 if z > z_thresh else -1 if z < -z_thresh else 0
        if direction != 0:
            gross = direction * (p[i + h] - p[i]) * contract_lbs * n_contracts
            cost = spread * contract_lbs * n_contracts + commission_rt * n_contracts
            net = gross - cost
            equity += net
            trades += 1
            wins += int(net > 0)
            cost_sum += cost
            nets.append(net)
            peak = max(peak, equity)
            maxdd = max(maxdd, peak - equity)
            if equity <= 0:
                blew_up = True
        i += h

    if trades == 0:
        return {"trades": 0}
    nets = np.array(nets)
    gains = nets[nets > 0].sum()
    losses = -nets[nets < 0].sum()
    net_total = float(equity - start_capital)
    # buy & hold one position the whole span, paying one round trip
    bh = float((p[i] - p[warmup]) * contract_lbs * n_contracts
               - (spread * contract_lbs * n_contracts + commission_rt * n_contracts))
    return {
        "trades": trades,
        "win_rate": round(wins / trades * 100, 1),
        "net_total": round(net_total, 0),
        "ret_pct": round(net_total / start_capital * 100, 1),
        "final_equity": round(equity, 0),
        "costs_paid": round(cost_sum, 0),
        "max_drawdown": round(maxdd, 0),
        "max_dd_pct": round(maxdd / start_capital * 100, 1),
        "profit_factor": round(float(gains / losses), 2) if losses > 0 else None,
        "buy_hold_net": round(bh, 0),
        "blew_up": blew_up,
        "start_capital": start_capital,
        "contract_lbs": contract_lbs,
    }


# =============================================================================
# STRATEGY ENGINE: test several strategies, pick the one that has worked best
# =============================================================================

def _signal_dir(strategy: str, hist: pd.Series, r: pd.Series,
                sigma: float, h: int) -> int:
    """Direction (+1 long / -1 short / 0 flat) for a named strategy."""
    if strategy == "momentum":
        m = momentum_drift(r, sigma)
        return 1 if m > 1e-9 else -1 if m < -1e-9 else 0
    if strategy == "reversion":
        rev = ar1_reversion_drift(hist, sigma)
        return 1 if rev > 1e-9 else -1 if rev < -1e-9 else 0
    if strategy == "ma_cross":
        if len(hist) < 205:
            return 0
        price = np.exp(hist)
        e50 = indicators.ema(price, 50).iloc[-1]
        e200 = indicators.ema(price, 200).iloc[-1]
        return 1 if e50 > e200 else -1
    if strategy == "macd":
        if len(hist) < 35:
            return 0
        _, _, hh = indicators.macd(np.exp(hist))
        v = float(hh.iloc[-1])
        return 1 if v > 0 else -1 if v < 0 else 0
    if strategy == "rsi_mr":
        if len(hist) < 20:
            return 0
        v = float(indicators.rsi(np.exp(hist), 14).iloc[-1])
        return 1 if v < 40 else -1 if v > 60 else 0
    # "ensemble" core (reversion + momentum blend)
    drift = (ar1_reversion_drift(hist, sigma) * DEFAULT_WEIGHTS["reversion"]
             + momentum_drift(r, sigma) * DEFAULT_WEIGHTS["momentum"])
    return 1 if drift > 1e-9 else -1 if drift < -1e-9 else 0


def strategy_backtest(log_d: pd.Series, strategy: str = "ensemble", h: int = 5,
                      mode: str = "futures", contract_lbs: int = 1,
                      spread: float = 0.0, commission_rt: float = 0.0,
                      cost_bps: float = 5.0, start_capital: float = 10000.0,
                      warmup: int = 200) -> dict:
    """Walk forward, take the strategy's direction using only prior data, hold h
    days, book the result minus costs. mode='futures' = fixed contract, leverage,
    can blow up; mode='shares' = invest full equity, unleveraged, compounds."""
    lp = log_d.dropna()
    p = np.exp(lp.values)
    if len(p) < h + warmup + 10:
        return {"trades": 0, "strategy": strategy}
    equity = start_capital
    peak = equity
    maxdd = 0.0
    blew = False
    trades = wins = 0
    cost_sum = 0.0
    nets = []
    i = warmup
    while i + h < len(p):
        hist = lp.iloc[: i + 1]
        r = hist.diff().dropna()
        sigma = cf.ewma_vol(r, 0.94)
        d = _signal_dir(strategy, hist, r, sigma, h)
        if d != 0:
            if mode == "futures":
                gross = d * (p[i + h] - p[i]) * contract_lbs
                cost = spread * contract_lbs + commission_rt
                net = gross - cost
                equity += net
            else:  # shares: unleveraged, compounding
                period_ret = d * (p[i + h] / p[i] - 1.0)
                cost_frac = cost_bps / 1e4
                before = equity
                equity *= (1.0 + period_ret - cost_frac)
                net = equity - before
                cost = before * cost_frac
            trades += 1
            wins += int(net > 0)
            cost_sum += cost
            nets.append(net)
            peak = max(peak, equity)
            maxdd = max(maxdd, peak - equity)
            if equity <= 0:
                blew = True
                break
        i += h

    if trades == 0:
        return {"trades": 0, "strategy": strategy}
    nets = np.array(nets)
    gains = nets[nets > 0].sum()
    losses = -nets[nets < 0].sum()
    net_total = float(equity - start_capital)
    if mode == "futures":
        bh = float((p[i] - p[warmup]) * contract_lbs
                   - (spread * contract_lbs + commission_rt))
    else:
        bh = float(start_capital * (p[min(i, len(p) - 1)] / p[warmup] - 1.0))
    return {
        "strategy": strategy, "trades": trades,
        "win_rate": round(wins / trades * 100, 1),
        "net_total": round(net_total, 0),
        "ret_pct": round(net_total / start_capital * 100, 1),
        "final_equity": round(equity, 0), "costs_paid": round(cost_sum, 0),
        "max_drawdown": round(maxdd, 0),
        "max_dd_pct": round(maxdd / start_capital * 100, 1),
        "profit_factor": round(float(gains / losses), 2) if losses > 0 else None,
        "buy_hold_net": round(bh, 0), "blew_up": blew,
        "start_capital": start_capital,
    }


# friendly label -> (strategy, horizon-days)
STRATEGY_MENU = {
    "Trend (momentum, weekly)": ("momentum", 5),
    "Mean-reversion (weekly)": ("reversion", 5),
    "Model ensemble (weekly)": ("ensemble", 5),
    "MA crossover (50/200)": ("ma_cross", 5),
    "MACD momentum (weekly)": ("macd", 5),
    "RSI mean-reversion (weekly)": ("rsi_mr", 5),
}


def evaluate_strategies(log_d: pd.Series, profile: dict) -> tuple[dict, str]:
    mode = "shares" if profile.get("kind") == "stock" else "futures"
    kw = dict(mode=mode)
    if mode == "futures":
        kw.update(contract_lbs=profile["contract_size"], spread=profile["spread"],
                  commission_rt=profile["commission_rt"])
    else:
        kw.update(cost_bps=profile.get("cost_bps", 5.0))
    out = {}
    for label, (strat, h) in STRATEGY_MENU.items():
        out[label] = strategy_backtest(log_d, strategy=strat, h=h, **kw)
    return out, mode


def pick_best_strategy(cands: dict) -> Optional[dict]:
    """Rank tradeable strategies by profit factor (then net). Only flag one as
    'recommended' if it actually made money after costs."""
    best = None
    for label, v in cands.items():
        if not v or not v.get("trades"):
            continue
        pf = v.get("profit_factor")
        score = pf if pf is not None else (3.0 if v["net_total"] > 0 else 0.0)
        cand = dict(v); cand["label"] = label; cand["score"] = score
        if best is None or score > best["score"]:
            best = cand
    if best is None:
        return None
    best["recommended"] = best["net_total"] > 0
    best["beats_hold"] = best["net_total"] > best.get("buy_hold_net", 0)
    return best


def current_strategy_dir(log_d: pd.Series, strategy: str, h: int) -> int:
    lp = log_d.dropna()
    r = lp.diff().dropna()
    sigma = cf.ewma_vol(r, 0.94)
    return _signal_dir(strategy, lp, r, sigma, h)


# =============================================================================
# CHART
# =============================================================================

def fan_chart(hf: HF, history: pd.Series, spot: float, unit: str = "$/lb",
              hist_points: int = 50):
    me = float(hf.median[-1])
    up = me >= spot
    color = "#1D9E75" if up else "#D85A30"     # green up / coral down
    fill = "#9FE1CB" if up else "#F0997B"
    fig, ax = plt.subplots(figsize=(7.2, 3.5), dpi=130)

    hist = history.tail(hist_points)
    ax.plot(hist.index, hist.values, color="#9c9a92", lw=1.3)

    x = [hist.index[-1]] + hf.times
    cat = lambda a: np.concatenate([[spot], a])
    ax.fill_between(x, cat(hf.lo95), cat(hf.hi95), color=fill, alpha=0.30, lw=0)
    ax.fill_between(x, cat(hf.lo68), cat(hf.hi68), color=fill, alpha=0.55, lw=0)
    ax.plot(x, cat(hf.median), color=color, lw=2.4, ls="--")

    ax.axhline(spot, color="#5f5e5a", lw=0.9, ls=(0, (2, 2)))
    ax.annotate(f"now  ${fmtp(spot)}", xy=(hist.index[0], spot),
                xytext=(0, 4), textcoords="offset points",
                fontsize=8.5, color="#5f5e5a")
    ax.annotate(f"${fmtp(me)}\n{(me/spot-1)*100:+.1f}%",
                xy=(hf.times[-1], me), xytext=(8, 0), textcoords="offset points",
                fontsize=10, color=color, fontweight="bold", va="center")

    arrow = "\u25B2" if up else "\u25BC"
    ax.set_title(
        f"{hf.name}:  {arrow} {(me/spot-1)*100:+.1f}%      "
        f"likely ${fmtp(hf.lo68[-1])} \u2013 ${fmtp(hf.hi68[-1])}",
        fontsize=10.5, color="#3d3d3a", loc="left")
    ax.set_ylabel(unit, fontsize=8.5)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, axis="y", alpha=0.16)
    ax.tick_params(labelsize=8)
    span = hf.times[-1] - hist.index[0]
    ax.set_xlim(hist.index[0], hf.times[-1] + span * 0.16)   # room for label
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    fig.tight_layout()
    return fig


# =============================================================================
# SELF-LEARNING: log predictions, grade them once their date passes, recalibrate
# =============================================================================

CALIB_DEFAULT = {"vol_calib": 1.0, "bias_per_day": 0.0, "n_eval": 0,
                 "cum_in68": 0, "cum_in95": 0, "dir_ok": 0, "dir_tot": 0,
                 "errpd_sum": 0.0, "last_note": "", "n_adjust": 0,
                 "acc_history": []}

HORIZON_DAYS = {"4 hours": 0.17, "Next day": 1.0, "Next 2 days": 2.0,
                "Next week": 5.0, "Next month": 21.0, "Next 3 months": 63.0}


def load_calibration(asset: str) -> dict:
    c = dict(CALIB_DEFAULT)
    p = _calib_path(asset)
    if p.exists():
        try:
            c.update(json.loads(p.read_text()))
        except Exception:
            pass
    return c


def save_calibration(c: dict, asset: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _calib_path(asset).write_text(json.dumps(c, indent=2))


def log_predictions(asset: str, now: dt.datetime, spot: float,
                    forecasts: list) -> None:
    """Record this run's forecasts so future runs can grade them."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _pred_log(asset)
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_time", "horizon", "target_time", "anchor",
                        "median", "lo68", "hi68", "lo95", "hi95", "evaluated"])
        for hf, _ in forecasts:
            e = lambda a: float(a[-1])
            w.writerow([now.isoformat(), hf.name, hf.times[-1].isoformat(),
                        f"{spot:.5f}", f"{e(hf.median):.5f}", f"{e(hf.lo68):.5f}",
                        f"{e(hf.hi68):.5f}", f"{e(hf.lo95):.5f}",
                        f"{e(hf.hi95):.5f}", 0])


def _append_evals(asset: str, graded: list) -> None:
    if not graded:
        return
    path = _eval_log(asset)
    new = not path.exists()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["made", "horizon", "anchor", "predicted", "actual", "in68",
              "in95", "err_pct", "correct"]
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        for g in graded:
            w.writerow({"made": g["made"], "horizon": g["horizon"],
                        "anchor": f"{g['anchor']:.4f}",
                        "predicted": f"{g['predicted']:.4f}",
                        "actual": f"{g['actual']:.4f}", "in68": int(g["in68"]),
                        "in95": int(g["in95"]), "err_pct": f"{g['err_pct']:.2f}",
                        "correct": int(g["correct"])})


def _rewrite_csv(path, rows) -> None:
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def read_recent_evals(asset: str, n: int = 8) -> list:
    path = _eval_log(asset)
    if not path.exists():
        return []
    try:
        rows = list(csv.DictReader(open(path)))
    except Exception:
        return []
    return rows[-n:][::-1]


def _count_pending(asset: str) -> int:
    """How many logged predictions haven't been graded yet."""
    path = _pred_log(asset)
    if not path.exists():
        return 0
    try:
        rows = list(csv.DictReader(open(path)))
        return sum(1 for r in rows if r.get("evaluated") != "1")
    except Exception:
        return 0


# ---- per-indicator scorecard: track each indicator's own hit rate per asset --
def _ind_log(asset: str):
    return STATE_DIR / f"{asset}_indicator_signals.csv"


def _ind_scores_path(asset: str):
    return STATE_DIR / f"{asset}_indicator_scores.json"

_IND_FIELDS = ["run_time", "target_time", "anchor", "indicator_key",
               "indicator_name", "signal"]


def load_indicator_scores(asset: str) -> dict:
    p = _ind_scores_path(asset)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_indicator_scores(asset: str, scores: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _ind_scores_path(asset).write_text(json.dumps(scores, indent=2))


def log_indicator_signals(asset: str, now: dt.datetime, spot: float,
                          ind_list: list, horizon_days: int = 7) -> None:
    """Record each indicator's current call to grade ~a week later. Deduped to
    at most one entry per ~12h so the hit-rate samples don't get flooded."""
    path = _ind_log(asset)
    if path.exists():
        try:
            rows = list(csv.DictReader(open(path)))
            if rows:
                last = max(pd.to_datetime(r["run_time"]) for r in rows)
                if (pd.Timestamp(now) - last) < pd.Timedelta(hours=12):
                    return
        except Exception:
            pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tt = (now + dt.timedelta(days=horizon_days)).isoformat()
    new = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_IND_FIELDS)
        if new:
            w.writeheader()
        for s in ind_list:
            w.writerow({"run_time": now.isoformat(), "target_time": tt,
                        "anchor": f"{spot:.5f}", "indicator_key": s["key"],
                        "indicator_name": s["name"], "signal": s["signal"]})


def evaluate_indicator_signals(asset: str, daily: pd.Series,
                               now: dt.datetime) -> int:
    """Grade indicator calls whose week is up: was the move in the signaled
    direction? Update the per-indicator tallies + a rolling recent window."""
    path = _ind_log(asset)
    if not path.exists():
        return 0
    try:
        rows = list(csv.DictReader(open(path)))
    except Exception:
        return 0
    d = daily.dropna()
    d.index = pd.DatetimeIndex(d.index)
    scores = load_indicator_scores(asset)
    graded_now = 0
    remaining = []
    for r in rows:
        try:
            tt = pd.to_datetime(r["target_time"])
        except Exception:
            continue
        if tt > pd.Timestamp(now):
            remaining.append(r)
            continue
        after = d[d.index >= tt]
        if after.empty:
            remaining.append(r)
            continue
        actual = float(after.iloc[0])
        anchor = float(r["anchor"])
        sgn = int(float(r["signal"]))
        if sgn != 0:
            correct = int((actual > anchor and sgn > 0) or
                          (actual < anchor and sgn < 0))
            k = r["indicator_key"]
            e = scores.setdefault(k, {"name": r.get("indicator_name", k),
                                      "graded": 0, "correct": 0, "recent": []})
            e["graded"] += 1
            e["correct"] += correct
            e["recent"] = (e.get("recent", []) + [correct])[-20:]
            e["name"] = r.get("indicator_name", e["name"])
            graded_now += 1
    # keep only ungraded rows (rewrite even if empty, to avoid double-grading)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_IND_FIELDS)
        w.writeheader()
        for r in remaining:
            w.writerow({k: r.get(k, "") for k in _IND_FIELDS})
    if graded_now:
        save_indicator_scores(asset, scores)
    return graded_now


def indicator_scorecard(asset: str) -> dict:
    """Display-ready per-indicator hit rates (all-time + recent window)."""
    scores = load_indicator_scores(asset)
    out = {}
    for k, e in scores.items():
        g = e.get("graded", 0)
        c = e.get("correct", 0)
        rec = e.get("recent", [])
        out[k] = {"name": e.get("name", k), "graded": g,
                  "hit_all": (c / g) if g else None,
                  "hit_recent": (sum(rec) / len(rec)) if rec else None,
                  "recent_n": len(rec)}
    return out


def evaluate_due(asset: str, daily: pd.Series, hourly: pd.Series,
                 now: dt.datetime) -> tuple[list, dict, str]:
    """Grade every logged prediction whose target time has passed against the
    actual price, then nudge the calibration (drift bias + band width)."""
    calib = load_calibration(asset)
    path = _pred_log(asset)
    if not path.exists():
        return [], calib, calib.get("last_note", "")
    try:
        rows = list(csv.DictReader(open(path)))
    except Exception:
        return [], calib, calib.get("last_note", "")

    combined = pd.concat([daily, hourly]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]

    graded, changed = [], False
    for r in rows:
        if r.get("evaluated") == "1":
            continue
        try:
            tt = pd.to_datetime(r["target_time"])
        except Exception:
            continue
        if tt > pd.Timestamp(now):
            continue
        after = combined[combined.index >= tt]
        if after.empty:
            continue
        actual = float(after.iloc[0])
        anchor, med = float(r["anchor"]), float(r["median"])
        lo68, hi68 = float(r["lo68"]), float(r["hi68"])
        lo95, hi95 = float(r["lo95"]), float(r["hi95"])
        dir_pred = 1 if med > anchor else (-1 if med < anchor else 0)
        dir_act = 1 if actual > anchor else (-1 if actual < anchor else 0)
        correct = dir_pred != 0 and dir_pred == dir_act
        err_frac = (actual - med) / anchor
        graded.append({
            "made": r["run_time"][:16].replace("T", " "), "horizon": r["horizon"],
            "anchor": anchor, "predicted": med, "actual": actual,
            "in68": lo68 <= actual <= hi68, "in95": lo95 <= actual <= hi95,
            "err_pct": err_frac * 100, "correct": correct})
        r["evaluated"] = "1"; changed = True
        # update running calibration counters
        calib["n_eval"] += 1
        calib["cum_in68"] += int(lo68 <= actual <= hi68)
        calib["cum_in95"] += int(lo95 <= actual <= hi95)
        calib["errpd_sum"] += err_frac / HORIZON_DAYS.get(r["horizon"], 1.0)
        if dir_pred != 0:
            calib["dir_tot"] += 1
            calib["dir_ok"] += int(correct)

    if not changed:
        return [], calib, calib.get("last_note", "")

    n = max(calib["n_eval"], 1)
    cov68 = calib["cum_in68"] / n
    diracc = (calib["dir_ok"] / calib["dir_tot"]) if calib["dir_tot"] else None
    mean_errpd = calib["errpd_sum"] / n
    old_bias, old_vol = calib["bias_per_day"], calib["vol_calib"]
    # nudge future drift to correct a systematic over/under-prediction.
    # stronger + wider cap than before, so a persistent directional bias (e.g. the
    # model leaning up while the market falls) gets corrected faster.
    calib["bias_per_day"] = float(max(-0.004, min(0.004, mean_errpd * 0.7)))
    # nudge band width toward proper 68% coverage
    if cov68 < 0.60:
        calib["vol_calib"] = min(1.6, calib["vol_calib"] * 1.04)
    elif cov68 > 0.78:
        calib["vol_calib"] = max(0.7, calib["vol_calib"] * 0.97)
    # count this as a real "adjustment" only if it actually moved the model
    if (abs(calib["bias_per_day"] - old_bias) > 1e-6
            or abs(calib["vol_calib"] - old_vol) > 1e-4):
        calib["n_adjust"] = calib.get("n_adjust", 0) + 1
    # record an accuracy snapshot for the "is it improving?" trend
    hist = calib.get("acc_history", [])
    hist.append({"t": now.isoformat(),
                 "dir_acc": round(diracc, 4) if diracc is not None else None,
                 "cov68": round(cov68, 4), "n": calib["n_eval"]})
    calib["acc_history"] = hist[-60:]

    dirtxt = (f"{diracc*100:.0f}% right on direction"
              if diracc is not None else "direction n/a")
    biastxt = ("nudged future forecasts DOWN" if calib["bias_per_day"] < -1e-5
               else "nudged future forecasts UP" if calib["bias_per_day"] > 1e-5
               else "kept the center steady")
    voltxt = ("widened the ranges (it was over-confident)" if cov68 < 0.60
              else "tightened the ranges (they were too wide)" if cov68 > 0.78
              else "kept the range width")
    calib["last_note"] = (
        f"Graded {calib['n_eval']} past prediction(s): {dirtxt}; prices landed "
        f"inside the 68% range {cov68*100:.0f}% of the time. Based on that I "
        f"{biastxt} and {voltxt}. ({calib.get('n_adjust', 0)} model adjustments "
        f"so far.)")
    save_calibration(calib, asset)
    _append_evals(asset, graded)
    _rewrite_csv(_pred_log(asset), rows)
    return graded, calib, calib["last_note"]


def compute_signal(hf: HF, spot: float, reliability) -> dict:
    """Translate a forecast into a plain BUY / SELL / HOLD lean.
    z = how big the expected move is relative to one 68% band (≈1 std)."""
    me, lo, hi = float(hf.median[-1]), float(hf.lo68[-1]), float(hf.hi68[-1])
    m = math.log(me / spot) if me > 0 and spot > 0 else 0.0
    half = (math.log(hi / spot) - math.log(lo / spot)) / 2 if hi > 0 and lo > 0 else 0.0
    z = m / half if half > 1e-9 else 0.0
    lean = "BUY" if z > 0.5 else "SELL" if z < -0.5 else "HOLD"
    strength = ("strong" if abs(z) > 1.5 else
                "moderate" if abs(z) > 0.8 else "weak")
    return {"lean": lean, "strength": strength, "z": z,
            "move_pct": (me / spot - 1) * 100, "reliability": reliability,
            "p_up": hf.p_up}


# =============================================================================
# TOP-LEVEL: run the whole upgraded prediction once
# =============================================================================

HORIZONS = [
    # name, periods, step,                  freq,     news_decay
    ("4 hours",      4,  dt.timedelta(hours=1), "hourly", 1.0),
    ("Next day",     24, dt.timedelta(hours=1), "hourly", 0.85),
    ("Next 2 days",  48, dt.timedelta(hours=1), "hourly", 0.7),
    ("Next week",    5,  dt.timedelta(days=1),  "daily",  0.5),
    ("Next month",   21, dt.timedelta(days=1),  "daily",  0.3),
    ("Next 3 months", 63, dt.timedelta(days=1), "daily",  0.2),
]


DEFAULT_SCAN_STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
                       "AMD", "NFLX", "JPM", "V", "WMT", "XOM", "JNJ", "PG", "KO",
                       "DIS", "BA", "CAT", "GE", "INTC", "PFE", "NKE", "BAC"]


def _read_from_daily(cfg: dict, profile: dict, daily) -> dict:
    """Compute a lightweight read (price, weekly lean + odds, indicator-based
    conviction) from an already-fetched daily series. No network, no persistence."""
    lam = cfg["model"]["ewma_lambda"]
    log_d = np.log(daily)
    ret = log_d.diff().dropna()
    sigma = cf.ewma_vol(ret, lam)
    rev = ar1_reversion_drift(log_d, sigma)
    mom = momentum_drift(ret, sigma)
    tr, trend_str = trend_drift(log_d, sigma)
    if trend_str != 0 and rev != 0 and (np.sign(rev) != np.sign(trend_str)):
        rev *= max(0.0, 1.0 - 2.0 * abs(trend_str))  # kill reversion that fights a clear trend     # don't fight strong trends
    drift = (rev * DEFAULT_WEIGHTS["reversion"] + mom * DEFAULT_WEIGHTS["momentum"]
             + tr * DEFAULT_WEIGHTS["trend"])
    cum = drift * 5.0
    vol_h = sigma * math.sqrt(5.0)
    z = cum / vol_h if vol_h > 1e-9 else 0.0
    p_up = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    move_pct = (math.exp(cum) - 1.0) * 100.0
    spot = float(daily.iloc[-1])
    ind = indicators.compute_indicators(daily)
    isig = {it["key"]: it["signal"] for it in ind["list"]}
    sign = 1 if z > 0 else -1 if z < 0 else 0
    agree = sum(1 for it in ind["list"] if it["signal"] == sign) if sign else 0
    overext_up = isig.get("rsi", 0) < 0 or isig.get("boll", 0) < 0
    overext_down = isig.get("rsi", 0) > 0 or isig.get("boll", 0) > 0
    az = abs(z)
    strength = 3 if az > 1.2 else 2 if az > 0.8 else 1 if az > 0.4 else 0
    if strength >= 2 and agree < 2:          # need indicator backing for high conviction
        strength = 1
    if (z > 0 and overext_up) or (z < 0 and overext_down):   # overextension brake
        strength = max(0, strength - 1)
    lean = ("BUY" if (z > 0.4 and p_up > 0.53) else
            "SELL" if (z < -0.4 and p_up < 0.47) else "HOLD")
    if strength == 0:
        lean = "HOLD"
    conv = {0: "\u2014", 1: "weak", 2: "moderate", 3: "strong"}[strength]
    trend_up = isig.get("trend_cross", 0) == 1 and isig.get("vs200", 0) == 1
    trend_dn = isig.get("trend_cross", 0) == -1 and isig.get("vs200", 0) == -1
    trend_pct = 0.0
    if len(log_d) >= 64:
        trend_pct = (math.exp(float(log_d.iloc[-1] - log_d.iloc[-64])) - 1.0) * 100.0
    return {"name": profile["name"], "ticker": profile["ticker"],
            "kind": profile["kind"], "unit": profile["unit"], "spot": spot,
            "move_pct": move_pct, "p_up": p_up, "z": z, "lean": lean,
            "strength": strength, "conv": conv, "ind_bull": ind["bull"],
            "ind_bear": ind["bear"], "rsi": ind.get("rsi"),
            "trend_up": trend_up, "trend_dn": trend_dn, "trend_pct": trend_pct,
            "overbought": overext_up, "oversold": overext_down}


# ---- smart ticker resolver: accept company names + fix typos + suggest --------
TICKER_NAME = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
    "GOOGL": "Alphabet (Google)", "GOOG": "Alphabet (Google)", "META": "Meta (Facebook)",
    "TSLA": "Tesla", "AVGO": "Broadcom", "AMD": "AMD", "INTC": "Intel", "MU": "Micron",
    "QCOM": "Qualcomm", "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe",
    "CSCO": "Cisco", "IBM": "IBM", "NFLX": "Netflix", "DIS": "Disney", "CMCSA": "Comcast",
    "TMUS": "T-Mobile", "VZ": "Verizon", "T": "AT&T", "DELL": "Dell", "HPQ": "HP",
    "PYPL": "PayPal", "V": "Visa", "MA": "Mastercard", "AXP": "American Express",
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "WFC": "Wells Fargo",
    "GS": "Goldman Sachs", "MS": "Morgan Stanley", "C": "Citigroup", "BLK": "BlackRock",
    "BRK.B": "Berkshire Hathaway", "SCHW": "Charles Schwab", "COF": "Capital One",
    "XOM": "ExxonMobil", "CVX": "Chevron", "COP": "ConocoPhillips", "SLB": "SLB",
    "WMT": "Walmart", "COST": "Costco", "TGT": "Target", "HD": "Home Depot",
    "LOW": "Lowe's", "NKE": "Nike", "MCD": "McDonald's", "SBUX": "Starbucks",
    "KO": "Coca-Cola", "PEP": "PepsiCo", "PG": "Procter & Gamble", "CL": "Colgate",
    "MDLZ": "Mondelez", "KHC": "Kraft Heinz", "MO": "Altria", "PM": "Philip Morris",
    "UNH": "UnitedHealth", "JNJ": "Johnson & Johnson", "LLY": "Eli Lilly",
    "ABBV": "AbbVie", "MRK": "Merck", "PFE": "Pfizer", "TMO": "Thermo Fisher",
    "ABT": "Abbott", "DHR": "Danaher", "BMY": "Bristol Myers Squibb", "AMGN": "Amgen",
    "GILD": "Gilead", "CVS": "CVS Health", "MDT": "Medtronic", "ISRG": "Intuitive Surgical",
    "BA": "Boeing", "CAT": "Caterpillar", "GE": "GE Aerospace", "HON": "Honeywell",
    "UPS": "UPS", "FDX": "FedEx", "LMT": "Lockheed Martin", "RTX": "RTX (Raytheon)",
    "DE": "John Deere", "MMM": "3M", "UNP": "Union Pacific", "GM": "General Motors",
    "F": "Ford", "TRV": "Travelers", "EMR": "Emerson", "GD": "General Dynamics",
    "LIN": "Linde", "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Co",
    "AMT": "American Tower", "PLTR": "Palantir", "UBER": "Uber", "ABNB": "Airbnb",
    "SHOP": "Shopify", "SQ": "Block (Square)", "COIN": "Coinbase", "SNOW": "Snowflake",
    "DDOG": "Datadog", "CRWD": "CrowdStrike", "PANW": "Palo Alto Networks", "NOW": "ServiceNow",
    "MRVL": "Marvell", "SMCI": "Super Micro", "ARM": "Arm", "MSTR": "MicroStrategy",
    "NKE": "Nike", "LULU": "Lululemon", "CVNA": "Carvana", "RIVN": "Rivian", "LCID": "Lucid",
    "SOFI": "SoFi", "HOOD": "Robinhood", "GME": "GameStop", "AMC": "AMC",
    "SPY": "S&P 500 ETF", "QQQ": "Nasdaq-100 ETF", "DIA": "Dow ETF", "IWM": "Russell 2000 ETF",
    "NEM": "Newmont", "PARA": "Paramount", "WBD": "Warner Bros Discovery", "SNAP": "Snap",
    "PINS": "Pinterest", "ROKU": "Roku", "ZM": "Zoom", "DOCU": "DocuSign", "NET": "Cloudflare",
    "U": "Unity Software", "RBLX": "Roblox", "DKNG": "DraftKings", "MARA": "Marathon Digital",
    "RIOT": "Riot Platforms", "PLUG": "Plug Power", "ENPH": "Enphase", "FSLR": "First Solar",
    "DAL": "Delta Air Lines", "UAL": "United Airlines", "AAL": "American Airlines",
    "CCL": "Carnival", "RCL": "Royal Caribbean", "MGM": "MGM Resorts", "BABA": "Alibaba",
    "NIO": "NIO", "PDD": "PDD (Temu)", "TSM": "Taiwan Semiconductor", "ASML": "ASML",
    "TTD": "The Trade Desk", "NEE": "NextEra Energy",
}
_NAME_ALIASES = {
    "google": "GOOGL", "alphabet": "GOOGL", "facebook": "META", "meta platforms": "META",
    "coke": "KO", "coca cola": "KO", "pepsi": "PEP", "j&j": "JNJ", "johnson": "JNJ",
    "amazon.com": "AMZN", "berkshire": "BRK.B", "berkshire hathaway": "BRK.B",
    "att": "T", "at and t": "T", "tmobile": "TMUS", "t mobile": "TMUS",
    "raytheon": "RTX", "deere": "DE", "john deere": "DE", "square": "SQ", "block": "SQ",
    "exxon": "XOM", "exxonmobil": "XOM", "chevron": "CVX", "lilly": "LLY", "eli lilly": "LLY",
    "united health": "UNH", "unitedhealthcare": "UNH", "p&g": "PG", "procter and gamble": "PG",
    "3m": "MMM", "ge": "GE", "general electric": "GE", "microsoft corp": "MSFT",
    "apple inc": "AAPL", "nvidia corp": "NVDA", "tesla motors": "TSLA", "the trade desk": "TTD",
    "micro strategy": "MSTR", "super micro": "SMCI", "palo alto": "PANW",
}
_COMPANY_TO_TICKER = {v.lower(): k for k, v in TICKER_NAME.items()}
_COMPANY_TO_TICKER.update(_NAME_ALIASES)
# strip parentheticals so "alphabet (google)" also matches "alphabet"
for _tk, _nm in list(TICKER_NAME.items()):
    _base = _nm.split("(")[0].strip().lower()
    _COMPANY_TO_TICKER.setdefault(_base, _tk)
KNOWN_TICKERS = set(TICKER_NAME) | set(DEFAULT_SCAN_STOCKS)


def ticker_name(tk: str) -> str:
    return TICKER_NAME.get(tk.upper(), tk.upper())


def _yahoo_search(q: str) -> list:
    """Best-effort live symbol search (Yahoo autocomplete). Returns [(sym, name)].
    Never raises; short timeout so it can't hang the app."""
    try:
        import requests
        r = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                         params={"q": q, "quotesCount": 6, "newsCount": 0},
                         timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            out = []
            for it in r.json().get("quotes", []):
                sym = it.get("symbol")
                nm = it.get("shortname") or it.get("longname") or sym
                if sym and it.get("quoteType") in ("EQUITY", "ETF"):
                    out.append((sym, nm))
            return out
    except Exception:
        pass
    return []


# ---- extra context signals (free feeds; best-effort, never raise) ----------
def analyst_view(ticker: str):
    """Wall Street consensus from the free feed: rating, avg price target, count."""
    try:
        info = cf.yf.Ticker(ticker).info or {}
        rating = (info.get("recommendationKey") or "").replace("_", " ").title()
        tgt = info.get("targetMeanPrice")
        n = info.get("numberOfAnalystOpinions")
        if not rating and not tgt:
            return None
        return {"rating": rating or "n/a", "target": tgt, "n": n}
    except Exception:
        return None


def options_positioning(ticker: str):
    """Put/call open-interest ratio on the nearest expiry — a rough read of how
    options traders are positioned. NOT real-time 'flow' (that's paid data)."""
    try:
        tk = cf.yf.Ticker(ticker)
        exps = tk.options
        if not exps:
            return None
        ch = tk.option_chain(exps[0])
        calls = float(pd.to_numeric(ch.calls["openInterest"], errors="coerce").fillna(0).sum())
        puts = float(pd.to_numeric(ch.puts["openInterest"], errors="coerce").fillna(0).sum())
        if calls <= 0:
            return None
        return {"pc": puts / calls, "expiry": str(exps[0])}
    except Exception:
        return None


def social_buzz(ticker: str):
    """Recent StockTwits chatter: post count + tagged bullish/bearish split.
    Free public feed; noisy by nature, sometimes unavailable. Never raises."""
    try:
        import requests
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        msgs = (r.json() or {}).get("messages", []) or []
        def _sent(m):
            return (((m.get("entities") or {}).get("sentiment") or {}) or {}).get("basic")
        bull = sum(1 for m in msgs if _sent(m) == "Bullish")
        bear = sum(1 for m in msgs if _sent(m) == "Bearish")
        return {"msgs": len(msgs), "bull": bull, "bear": bear}
    except Exception:
        return None


def next_earnings(ticker: str):
    """Best-effort next earnings date for a stock. Returns a display string or
    None. yfinance's calendar/info are flaky, so this is wrapped and never raises."""
    import datetime as _dt
    try:
        cal = cf.yf.Ticker(ticker).calendar
        ed = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if isinstance(ed, (list, tuple)) and ed:
                ed = ed[0]
        elif cal is not None and hasattr(cal, "loc"):
            try:
                ed = cal.loc["Earnings Date"][0]
            except Exception:
                ed = None
        if ed is not None:
            try:
                return pd.Timestamp(ed).strftime("%b %d, %Y")
            except Exception:
                return str(ed)
    except Exception:
        pass
    try:
        info = cf.yf.Ticker(ticker).info or {}
        ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ts:
            return _dt.datetime.utcfromtimestamp(int(ts)).strftime("%b %d, %Y")
    except Exception:
        pass
    return None


def resolve_symbol(query: str):
    """Turn a user's text into a ticker. Returns (ticker_or_None, suggestions),
    where suggestions is a list of (ticker, name). Handles company names, fixes
    typos via fuzzy match, and offers predictive suggestions."""
    import difflib
    q = (query or "").strip()
    if not q:
        return None, []
    ql, qup = q.lower(), q.upper()
    if ql in _COMPANY_TO_TICKER:                 # exact company name / alias
        return _COMPANY_TO_TICKER[ql], []
    if qup in KNOWN_TICKERS:                      # exact known ticker
        return qup, []
    sugg = []
    for nm in difflib.get_close_matches(ql, list(_COMPANY_TO_TICKER.keys()), n=6, cutoff=0.7):
        sugg.append((_COMPANY_TO_TICKER[nm], ticker_name(_COMPANY_TO_TICKER[nm])))
    for nm, tk in _COMPANY_TO_TICKER.items():     # substring (predictive) match
        if len(ql) >= 2 and ql in nm:
            sugg.append((tk, ticker_name(tk)))
    for tk in difflib.get_close_matches(qup, list(KNOWN_TICKERS), n=5, cutoff=0.75):
        sugg.append((tk, ticker_name(tk)))
    if not sugg:                                  # nothing local — try live search
        sugg = _yahoo_search(q)
    seen, ded = set(), []
    for tk, nm in sugg:
        if tk not in seen:
            seen.add(tk)
            ded.append((tk, nm))
    sugg = ded[:8]
    # a plausible ticker we simply don't have listed → let them try it directly
    if not sugg and qup.isalpha() and 1 <= len(qup) <= 5:
        return qup, []
    return None, sugg


def strong_trend_signal(r) -> Optional[str]:
    """Shared definition of a 'strong' buy/sell, used by BOTH the top-of-app alert
    and the Top & Bottom 'strong only' view so they never disagree. It's a
    medium-term MOMENTUM signal: a clean 50/200-day trend, a big 3-month move
    (>=12%), and not overextended. The edge here is weeks-to-months, not next week
    (short-term odds stay near a coin flip — that's honest)."""
    if not isinstance(r, dict):
        return None
    if r.get("trend_up") and not r.get("overbought") and r.get("trend_pct", 0) >= 12:
        return "BUY"
    if r.get("trend_dn") and not r.get("oversold") and r.get("trend_pct", 0) <= -12:
        return "SELL"
    return None


def quick_read(cfg: dict, asset_key: str = None,
               stock_ticker: str = None) -> dict:
    """Fast read for the overview: fetch daily, then compute the lean. Returns
    {..., 'error': reason} on any data problem so the caller can show a blank row."""
    profile = assets.resolve(asset_key, stock_ticker)
    base = {"name": profile["name"], "ticker": profile["ticker"],
            "kind": profile["kind"], "unit": profile["unit"]}
    try:
        df = cf.yf.download(profile["ticker"], period="2y", interval="1d",
                            auto_adjust=False, progress=False)
        daily = cf._close_series(df)
    except Exception:
        return {**base, "error": "no data"}
    if daily is None or len(daily) < 60:
        return {**base, "error": "thin data"}
    return _read_from_daily(cfg, profile, daily)


# ---- market scan: rank a whole universe, and grade its own past calls --------
def _scan_log():
    return STATE_DIR / "scan_signals.csv"


def _scan_scores_path():
    return STATE_DIR / "scan_scores.json"

_SCAN_FIELDS = ["run_time", "target_time", "key", "name", "anchor", "lean"]


def _load_scan_scores() -> dict:
    p = _scan_scores_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"graded": 0, "correct": 0, "per": {}}


# --- broad index universes for the scan (curated; edit anytime) --------------
DOW_30 = ["AAPL","AMGN","AXP","BA","CAT","CRM","CSCO","CVX","DIS","DOW","GS","HD",
    "HON","IBM","JNJ","JPM","KO","MCD","MMM","MRK","MSFT","NKE","PG","TRV","UNH",
    "V","VZ","WMT","NVDA","AMZN"]

NASDAQ_100 = ["AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","AVGO","TSLA","COST",
    "NFLX","ADBE","PEP","AMD","CSCO","TMUS","INTC","QCOM","INTU","TXN","AMGN","ISRG",
    "HON","AMAT","BKNG","VRTX","ADP","SBUX","GILD","MDLZ","ADI","REGN","LRCX","PANW",
    "MU","KLAC","SNPS","CDNS","MELI","PYPL","MAR","CRWD","ORLY","CSX","ABNB","FTNT",
    "DASH","ADSK","NXPI","WDAY","TTD","CHTR","MNST","PCAR","PAYX","ROP","KDP","ODFL",
    "FANG","EA","CTAS","DDOG","VRSK","EXC","GEHC","KHC","LULU","FAST","CSGP","BKR",
    "XEL","IDXX","ZS","TTWO","ANSS","ON","CDW","BIIB","MRVL","DXCM","WBD","ILMN",
    "ARM","SMCI","TEAM","CCEP","GFS","MDB","AZN"]

SP_100 = ["AAPL","ABBV","ABT","ACN","ADBE","AIG","AMD","AMGN","AMT","AMZN","AVGO",
    "AXP","BA","BAC","BK","BKNG","BLK","BMY","BRK.B","C","CAT","CHTR","CL","CMCSA",
    "COF","COP","COST","CRM","CSCO","CVS","CVX","DHR","DIS","DOW","DUK","EMR","EXC",
    "F","FDX","GD","GE","GILD","GM","GOOG","GOOGL","GS","HD","HON","IBM","INTC","JNJ",
    "JPM","KO","LIN","LLY","LMT","LOW","MA","MCD","MDLZ","MDT","MET","META","MMM","MO",
    "MRK","MS","MSFT","NEE","NFLX","NKE","NVDA","ORCL","PEP","PFE","PG","PM","PYPL",
    "QCOM","RTX","SBUX","SCHW","SO","T","TGT","TMO","TSLA","TXN","UNH","UNP","UPS","V",
    "VZ","WFC","WMT","XOM"]

SCAN_UNIVERSES = {
    "My custom list": None,        # uses whatever is typed in the box
    "Dow 30": DOW_30,
    "Nasdaq-100 (big tech)": NASDAQ_100,
    "S&P 100 (large caps)": SP_100,
    "Everything (big US mix)": sorted(set(DOW_30 + NASDAQ_100 + SP_100)),
}


def _yf_download_retry(grp, tries: int = 4, **kw):
    """yf.download with backoff on Yahoo rate limits (HTTP 429). Returns the
    dataframe, or None after exhausting retries. A cold Streamlit boot fires many
    requests at once and Yahoo throttles; retrying with a short wait clears it
    most of the time instead of failing the whole page."""
    import time as _t
    delay = 1.5
    for attempt in range(tries):
        try:
            return cf.yf.download(grp, **kw)
        except Exception as e:
            msg = str(e).lower()
            rate = "429" in msg or "too many" in msg or "rate" in msg
            if rate and attempt < tries - 1:
                _t.sleep(delay)
                delay *= 2          # 1.5s, 3s, 6s
                continue
            return None
    return None


def _batch_close_series(tickers, chunk: int = 40, progress=None,
                        base: float = 0.0, span: float = 1.0) -> dict:
    """Download many tickers in a few batched requests (rate-limit friendly) and
    return {ticker: tz-naive close Series}. Failures are skipped, not raised.
    Rate-limited batches are retried with backoff before giving up."""
    out = {}
    total = max(len(tickers), 1)
    done = 0
    for i in range(0, len(tickers), chunk):
        grp = tickers[i:i + chunk]
        data = _yf_download_retry(grp, period="1y", interval="1d",
                                  group_by="ticker", auto_adjust=False,
                                  progress=False, threads=True)
        for t in grp:
            done += 1
            if progress:
                progress(base + span * done / total, t)
            try:
                if data is None:
                    continue
                if len(grp) == 1:
                    sub = data
                elif hasattr(data.columns, "levels") and t in data.columns.get_level_values(0):
                    sub = data[t]
                else:
                    sub = None
                if sub is None or "Close" not in sub.columns:
                    continue
                s = cf._close_series(sub)
                if s is not None and len(s) >= 60:
                    out[t] = s
            except Exception:
                continue
    return out


# Liquid names watched for the background "screaming buy/sell" alert.
ALERT_UNIVERSE = sorted(set(DOW_30 + [
    "NVDA", "AMD", "AVGO", "MU", "CRM", "ADBE", "NFLX", "TSLA", "AMZN", "GOOGL",
    "META", "AAPL", "MSFT", "QCOM", "ORCL", "COST", "LLY", "XOM"]))


def quick_universe_reads(cfg: dict, tickers, progress=None) -> list:
    """Fast batched read of a stock universe -> list of per-stock reads (price,
    lean, conviction/strength, odds). Used by the alert scan and live checks.
    No grading/persistence — just the current read. Metals are added too."""
    reads = []
    closes = _batch_close_series(list(tickers), chunk=40, progress=progress,
                                 base=0.05, span=0.9)
    for m in ["copper", "gold", "silver", "aluminium"]:
        prof = assets.resolve(m, None)
        try:
            df = cf.yf.download(prof["ticker"], period="1y", interval="1d",
                                auto_adjust=False, progress=False)
            s = cf._close_series(df)
            if s is not None and len(s) >= 60:
                closes[prof["ticker"]] = s
        except Exception:
            pass
    for t in list(tickers) + ["HG=F", "GC=F", "SI=F", "ALI=F"]:
        s = closes.get(t)
        if s is None or len(s) < 60:
            continue
        prof = assets.resolve(None, t) if t not in ("HG=F", "GC=F", "SI=F", "ALI=F") \
            else assets.resolve({"HG=F": "copper", "GC=F": "gold", "SI=F": "silver",
                                 "ALI=F": "aluminium"}[t], None)
        try:
            reads.append(_read_from_daily(cfg, prof, s))
        except Exception:
            continue
    return reads


def market_scan(cfg: dict, stock_tickers=None, progress=None) -> dict:
    """Read every metal + a (possibly large) stock universe, rank by weekly lean,
    and (self-learning) grade the prior scan's calls that have come due. Stocks are
    fetched in batches so 100+ names won't rate-limit. Ranks/odds, NOT facts."""
    storage.pull("scan")
    metals = ["copper", "gold", "silver", "aluminium"]
    stocks = stock_tickers if stock_tickers is not None else DEFAULT_SCAN_STOCKS
    scores = _load_scan_scores()
    lp = _scan_log()
    pending = []
    if lp.exists():
        try:
            pending = list(csv.DictReader(open(lp)))
        except Exception:
            pending = []
    now = dt.datetime.utcnow().replace(microsecond=0)
    items, errors = [], []

    # ---- pre-fetch all price series ----
    # metals individually (only 4, special futures tickers); stocks in batches.
    close_map = {}
    n_metal = len(metals)
    for idx, m in enumerate(metals):
        prof = assets.resolve(m, None)
        try:
            df = cf.yf.download(prof["ticker"], period="1y", interval="1d",
                                auto_adjust=False, progress=False)
            s = cf._close_series(df)
            if s is not None and len(s) >= 60:
                close_map[prof["ticker"]] = s
        except Exception:
            pass
        if progress:
            progress(0.05 * (idx + 1) / max(n_metal, 1), prof["name"])
    close_map.update(_batch_close_series(stocks, chunk=40, progress=progress,
                                         base=0.05, span=0.85))

    keys = [("metal", m) for m in metals] + [("stock", t) for t in stocks]
    remaining = list(pending)
    for kind, key in keys:
        prof = assets.resolve(key if kind == "metal" else None,
                              key if kind == "stock" else None)
        sk = prof["state_key"]
        daily = close_map.get(prof["ticker"])
        if daily is None or len(daily) < 60:
            errors.append(prof["name"])
            continue
        d = daily.dropna()
        d.index = pd.DatetimeIndex(d.index)
        # grade this asset's due scan calls
        still = []
        for r in remaining:
            if r.get("key") != sk:
                still.append(r)
                continue
            try:
                tt = pd.to_datetime(r["target_time"])
            except Exception:
                continue
            if tt > pd.Timestamp(now):
                still.append(r)
                continue
            after = d[d.index >= tt]
            if after.empty:
                still.append(r)
                continue
            actual = float(after.iloc[0])
            anchor = float(r["anchor"])
            lean = r.get("lean", "HOLD")
            if lean in ("BUY", "SELL"):
                correct = int((actual > anchor and lean == "BUY") or
                              (actual < anchor and lean == "SELL"))
                scores["graded"] += 1
                scores["correct"] += correct
                pk = scores["per"].setdefault(sk, {"graded": 0, "correct": 0,
                                                   "name": r.get("name", sk)})
                pk["graded"] += 1
                pk["correct"] += correct
        remaining = still
        rd = _read_from_daily(cfg, prof, daily)
        items.append(rd)
        # log today's lean (deduped to ~once per 12h per asset)
        recent = False
        for r in remaining:
            if r.get("key") == sk and r.get("run_time"):
                try:
                    if (pd.Timestamp(now) - pd.to_datetime(r["run_time"])) < pd.Timedelta(hours=12):
                        recent = True
                        break
                except Exception:
                    pass
        if not recent:
            remaining.append({"run_time": now.isoformat(),
                              "target_time": (now + dt.timedelta(days=7)).isoformat(),
                              "key": sk, "name": rd["name"],
                              "anchor": f"{rd['spot']:.5f}", "lean": rd["lean"]})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(lp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_SCAN_FIELDS)
        w.writeheader()
        for r in remaining:
            w.writerow({k: r.get(k, "") for k in _SCAN_FIELDS})
    _scan_scores_path().write_text(json.dumps(scores, indent=2))
    storage.push("scan")
    if progress:
        progress(1.0, "done")
    valid = [r for r in items if "z" in r]
    top = sorted(valid, key=lambda r: r["z"], reverse=True)[:10]
    bottom = sorted(valid, key=lambda r: r["z"])[:10]
    g = scores["graded"]
    return {"top": top, "bottom": bottom, "all": valid, "errors": errors,
            "graded": g, "correct": scores["correct"], "n_scanned": len(valid),
            "acc": (scores["correct"] / g) if g else None, "asof": now}


DEFAULT_IDEAS_UNIVERSE = ["AAPL", "MSFT", "NVDA", "AVGO", "AMD", "MU", "ORCL",
    "CRM", "ADBE", "QCOM", "DELL", "ANET", "GOOGL", "META", "AMZN", "NFLX", "TSLA",
    "HD", "COST", "WMT", "PG", "KO", "PEP", "JPM", "BAC", "GS", "V", "MA", "AXP",
    "XOM", "CVX", "CAT", "GE", "HON", "UNH", "JNJ", "LLY", "ABBV", "MRK", "T",
    "VZ", "MO", "IBM"]


def _fundamentals(ticker: str) -> dict:
    """Best-effort fundamentals from Yahoo's free feed (slow, sometimes missing).
    Returns Nones rather than raising so a screen still works on partial data."""
    pe = peg = dy = mc = eg = rg = None
    try:
        info = cf.yf.Ticker(ticker).info or {}
        pe = info.get("trailingPE") or info.get("forwardPE")
        peg = info.get("pegRatio") or info.get("trailingPegRatio")
        dy = info.get("dividendYield")
        if dy is not None and dy > 1.5:      # some feeds give percent, normalize to fraction
            dy = dy / 100.0
        mc = info.get("marketCap")
        eg = info.get("earningsGrowth")
        rg = info.get("revenueGrowth")
    except Exception:
        pass
    return {"pe": pe, "peg": peg, "div_yield": dy, "market_cap": mc,
            "earnings_growth": eg, "revenue_growth": rg}


def gather_ideas(cfg: dict, tickers=None, progress=None) -> list:
    """Build the raw idea table: price-momentum metrics (reliable) + best-effort
    fundamentals + this model's own technical lean, per stock. Screens are applied
    on top of this. Idea generation, NOT advice."""
    tickers = tickers if tickers is not None else DEFAULT_IDEAS_UNIVERSE
    out = []
    total = max(len(tickers), 1)
    done = 0
    yr = dt.datetime.utcnow().year
    for t in tickers:
        prof = assets.resolve(None, t)
        try:
            df = cf.yf.download(prof["ticker"], period="2y", interval="1d",
                                auto_adjust=False, progress=False)
            close = cf._close_series(df)
        except Exception:
            df, close = None, None
        done += 1
        if progress:
            progress(done / total, t)
        if close is None or len(close) < 60:
            out.append({"ticker": t, "name": prof["name"], "error": "no data"})
            continue
        spot = float(close.iloc[-1])
        ytd = None
        cy = close[close.index.year == yr]
        if len(cy) > 0:
            base = float(cy.iloc[0])
            ytd = (spot / base - 1.0) * 100.0 if base > 0 else None
        avgvol = None
        try:
            if df is not None and "Volume" in df:
                avgvol = float(pd.to_numeric(df["Volume"], errors="coerce").tail(90).mean())
        except Exception:
            pass
        sma50 = float(close.tail(50).mean())
        sma200 = float(close.tail(200).mean()) if len(close) >= 200 else None
        hi = float(close.tail(252).max())
        pct_from_high = (spot / hi - 1.0) * 100.0 if hi > 0 else None
        rd = _read_from_daily(cfg, prof, close)
        row = {"ticker": t, "name": prof["name"], "spot": spot, "ytd": ytd,
               "avgvol": avgvol, "above50": spot > sma50,
               "above200": (sma200 is not None and spot > sma200),
               "pct_from_high": pct_from_high, "lean": rd["lean"],
               "p_up": rd["p_up"], "z": rd["z"]}
        row.update(_fundamentals(prof["ticker"]))
        out.append(row)
    return out


IDEA_SCREENS = ["Momentum", "Momentum + Value", "Momentum + Growth",
                "Momentum + Income"]


def apply_screen(rows: list, screen: str) -> list:
    """Apply a Fidelity-style factor screen to the gathered rows. Thresholds are
    sensible defaults in the spirit of the article (point-in-time, adjustable)."""
    v = [r for r in rows if "error" not in r]
    ytd = lambda r: (r.get("ytd") if r.get("ytd") is not None else -999.0)
    liq = lambda r: (r.get("avgvol") or 0) >= 300_000
    if screen == "Momentum + Value":
        res = [r for r in v if ytd(r) >= 15 and r.get("peg") is not None
               and 0 < r["peg"] <= 2.0 and liq(r)]
        res.sort(key=lambda r: (r.get("market_cap") or 0), reverse=True)
    elif screen == "Momentum + Growth":
        def grows(r):
            eg, rg = r.get("earnings_growth"), r.get("revenue_growth")
            return (eg is not None and eg >= 0.20) or (rg is not None and rg >= 0.20)
        res = [r for r in v if ytd(r) >= 15 and grows(r) and liq(r)]
        res.sort(key=lambda r: (r.get("market_cap") or 0), reverse=True)
    elif screen == "Momentum + Income":
        res = [r for r in v if ytd(r) >= 8 and r.get("div_yield") is not None
               and r["div_yield"] >= 0.03 and liq(r)]
        res.sort(key=lambda r: (r.get("market_cap") or 0), reverse=True)
    else:  # pure Momentum (price-based, most reliable)
        res = [r for r in v if ytd(r) >= 15 and r.get("above200") and liq(r)]
        res.sort(key=lambda r: ytd(r), reverse=True)
    return res


RESET_TOKEN = "trend-v10-2026-08-22"   # bump to force a fresh one-time reset


def reset_learning(state_key: str) -> None:
    """Wipe an asset's learning history (calibration + logged/graded predictions +
    indicator scorecard) both locally and on the gist, so the score starts fresh
    after a model change instead of dragging old mistakes forever."""
    try:
        for p in STATE_DIR.glob(f"{state_key}_*"):
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass
    try:
        storage.delete(state_key)          # remove from the gist too, or pull restores it
    except Exception:
        pass


def maybe_reset_all_once() -> bool:
    """Once after a model change, wipe EVERY asset's old track record (which came
    from the old logic) so the improved engine is measured fresh. Uses a marker so
    it runs a single time per install/gist. Returns True if it just reset."""
    storage.pull("resetmarker")
    mpath = STATE_DIR / "resetmarker_token.json"
    cur = None
    if mpath.exists():
        try:
            cur = json.loads(mpath.read_text()).get("token")
        except Exception:
            cur = None
    if cur == RESET_TOKEN:
        return False
    # wipe the gist (keep only the marker) and all local state
    try:
        storage.delete_all(keep_prefix="resetmarker_")
    except Exception:
        pass
    try:
        for p in STATE_DIR.glob("*"):
            if p.name.startswith("resetmarker_"):
                continue
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        mpath.write_text(json.dumps({"token": RESET_TOKEN}))
        storage.push("resetmarker")
    except Exception:
        pass
    return True


def trade_plan(hf, spot: float, signal: dict) -> dict:
    """Turn the forecast range + lean into a simple, risk-defined plan:
    entry / target / stop, reward:risk, the odds, and an honest edge check.
    These are MECHANICAL levels from the model's own range — not advice, and
    not a profit prediction. The stop is the part that protects you."""
    e = lambda a: float(a[-1])
    hi68, lo68 = e(hf.hi68), e(hf.lo68)
    hi95, lo95 = e(hf.hi95), e(hf.lo95)
    lean = signal["lean"]
    p_up = float(hf.p_up)
    if lean == "BUY":
        direction = "LONG"
        entry, dip = spot, lo68
        target, stop = hi68, lo95
        prob = p_up
    elif lean == "SELL":
        direction = "SHORT"
        entry, dip = spot, hi68
        target, stop = lo68, hi95
        prob = 1.0 - p_up
    else:
        return {"direction": "WAIT", "prob": max(p_up, 1.0 - p_up),
                "grades_on": hf.times[-1]}
    reward = abs(target - entry)
    risk = abs(entry - stop)
    rr = (reward / risk) if risk > 1e-9 else None
    ev = prob * reward - (1.0 - prob) * risk      # expected $ per unit, pre-cost
    return {"direction": direction, "entry": entry, "dip": dip, "target": target,
            "stop": stop, "reward": reward, "risk": risk, "rr": rr, "prob": prob,
            "ev": ev, "ev_positive": ev > 0, "grades_on": hf.times[-1]}


def run_prediction(cfg: dict, asset_key: str = "copper",
                   stock_ticker: Optional[str] = None,
                   spot_override: Optional[float] = None,
                   scenario_key: Optional[str] = None,
                   use_lgbm: bool = True) -> dict:
    """Forecast a metal (asset_key) OR any stock (stock_ticker). Returns a dict
    with forecasts, news, events, a strategy comparison + auto-pick, an honest
    backtest, the self-learning track record, and metadata."""
    profile = assets.resolve(asset_key, stock_ticker)
    state_key = profile["state_key"]
    now = dt.datetime.utcnow().replace(microsecond=0)
    daily, hourly = cf.fetch_prices(profile["ticker"])
    have_hourly = len(hourly) > 30
    ref = fetch_reference_series(profile)        # dollar for metals, SPY for stocks

    spot = float(spot_override) if spot_override else float(
        hourly.iloc[-1] if have_hourly else daily.iloc[-1])

    lam = cfg["model"]["ewma_lambda"]
    log_d = np.log(daily); ret_d = log_d.diff().dropna()
    if have_hourly:
        log_h = np.log(hourly); ret_h = log_h.diff().dropna()
    else:
        log_h, ret_h = log_d, ret_d

    # news + scenario (per-asset terms / query / scenarios)
    headlines = cf.fetch_news_headlines(cfg, query=profile["news_query"])
    news = (cf.score_headlines_llm(headlines, cfg, profile["name"])
            or cf.score_headlines_keyword(headlines, profile["bullish"],
                                          profile["bearish"]))
    scenarios = profile["scenarios"]
    scenario = scenarios.get(scenario_key) if scenario_key else None
    scen_score = scenario["score"] if scenario else 0.0
    scen_vol = scenario["vol_mult"] if scenario else 1.0
    combined_score = float(np.clip(news.score + scen_score, -1.0, 1.0))

    events = cf.build_event_calendar(now.date())
    upcoming = [e for e in events if e.when >= now][:8]

    # ---- LEARN: grade this asset's past predictions, get its calibration ----
    storage.pull(state_key)        # hydrate persisted memory (no-op if unconfigured)
    graded, calib, learned_note = evaluate_due(state_key, daily, hourly, now)
    bias_per_day = calib.get("bias_per_day", 0.0)
    vol_calib = calib.get("vol_calib", 1.0)

    forecasts = []
    for name, periods, step, freq, decay in HORIZONS:
        lp, lr = (log_h, ret_h) if (freq == "hourly" and have_hourly) else (log_d, ret_d)
        end = now + step * periods
        win = cf.events_in_window(events, now, end)
        ev_mult = max([1.0] + [e.vol_mult for e in win])
        event_c = 1.0 + (ev_mult - 1.0) * decay
        news_c = 1.0 + news.vol_bump * decay
        vol_mult = event_c * news_c * scen_vol
        bias_pp = bias_per_day if freq == "daily" else bias_per_day / 24.0
        hf = build_horizon(name, periods, step, now, spot, lp, lr, lam, ref,
                           combined_score, decay, vol_mult, DEFAULT_WEIGHTS,
                           use_lgbm, bias_per_period=bias_pp, vol_calib=vol_calib)
        hf.events = win
        forecasts.append((hf, freq))

    backtests = {"1 day": backtest(log_d, 1), "2 day": backtest(log_d, 2),
                 "1 week": backtest(log_d, 5), "1 month": backtest(log_d, 21),
                 "3 month": backtest(log_d, 63)}

    # ---- STRATEGIES: test several, pick the one that has worked best here ----
    strategies, bt_mode = evaluate_strategies(log_d, profile)
    best_strategy = pick_best_strategy(strategies)
    if best_strategy:
        strat, h = STRATEGY_MENU[best_strategy["label"]]
        d = current_strategy_dir(log_d, strat, h)
        best_strategy["current_dir"] = d
        best_strategy["current_call"] = ("LONG" if d > 0 else "SHORT" if d < 0 else "FLAT")

    # ---- SIGNAL: plain BUY / SELL / HOLD lean per horizon ----
    bt_for = {"Next day": backtests["1 day"], "Next 2 days": backtests["2 day"],
              "Next week": backtests["1 week"], "Next month": backtests["1 month"],
              "Next 3 months": backtests["3 month"]}
    signals = {}
    for hf, _ in forecasts:
        bt = bt_for.get(hf.name) or {}
        signals[hf.name] = compute_signal(hf, spot, bt.get("directional_acc"))
    headline = signals.get("Next week") or next(iter(signals.values()))

    near = [signals[h] for h in ("4 hours", "Next day", "Next 2 days", "Next week")
            if h in signals]
    ups = sum(1 for s in near if s["lean"] == "BUY")
    downs = sum(1 for s in near if s["lean"] == "SELL")
    wk_rel = headline["reliability"]

    # technical indicators: a readout + how many agree with the weekly lean
    ind = indicators.compute_indicators(daily)
    ind_agree, ind_disagree = indicators.agreement_with(ind["list"], headline["lean"])
    ind["agree"] = ind_agree
    ind["disagree"] = ind_disagree
    # per-indicator scorecard: grade calls that are due, log today's, attach rates
    ind["graded_now"] = evaluate_indicator_signals(state_key, daily, now)
    log_indicator_signals(state_key, now, spot, ind["list"])
    _scard = indicator_scorecard(state_key)
    for _it in ind["list"]:
        _s = _scard.get(_it["key"])
        if _s:
            _it["hit_all"] = _s["hit_all"]
            _it["hit_recent"] = _s["hit_recent"]
            _it["graded"] = _s["graded"]
            _it["recent_n"] = _s["recent_n"]
    ind["scorecard"] = _scard

    # STRONG now ALSO requires several proven indicators to agree with the call
    horizons_align = max(ups, downs) >= 3 and abs(headline["z"]) > 0.8
    ind_confirms = ind_agree >= 3 and ind_agree > ind_disagree
    if horizons_align and (wk_rel or 0) >= 0.55 and ind_confirms:
        conviction = "STRONG"
    elif headline["lean"] != "HOLD" and abs(headline["z"]) > 0.5 and ind_agree >= ind_disagree:
        conviction = "MODERATE"
    else:
        conviction = "LOW"

    # OVEREXTENSION BRAKE: don't be confident chasing a stretched move. If we're
    # leaning UP while overbought (or DOWN while oversold), cut conviction a notch.
    _isig = {it["key"]: it["signal"] for it in ind["list"]}
    overext_up = _isig.get("rsi", 0) < 0 or _isig.get("boll", 0) < 0    # overbought / above upper band
    overext_down = _isig.get("rsi", 0) > 0 or _isig.get("boll", 0) > 0  # oversold / below lower band
    brake = False
    if (headline["lean"] == "BUY" and overext_up) or \
       (headline["lean"] == "SELL" and overext_down):
        if conviction == "STRONG":
            conviction, brake = "MODERATE", True
        elif conviction == "MODERATE":
            conviction, brake = "LOW", True
    headline.update({"conviction": conviction, "agree_up": ups,
                     "agree_down": downs, "n_near": len(near),
                     "ind_agree": ind_agree, "ind_disagree": ind_disagree,
                     "overext_brake": brake})

    ref_ret_daily = (np.log(ref / ref.shift(1)).dropna() if not ref.empty else None)
    _, ref_beta = (dollar_drift(ret_d, ref_ret_daily, cf.ewma_vol(ret_d, lam))
                   if ref_ret_daily is not None else (0.0, 0.0))

    # ---- LOG this run's forecasts so a future run can grade them ----
    log_predictions(state_key, now, spot, forecasts)
    storage.push(state_key)        # persist updated memory (no-op if unconfigured)
    n_pending = _count_pending(state_key)
    n_made = calib.get("n_eval", 0) + n_pending

    # ---- simple entry/target/stop plans for the short horizons ----
    _fmap = {hf.name: hf for hf, _ in forecasts}
    plans = {}
    for nm in ("4 hours", "Next day", "Next week"):
        if nm in _fmap and nm in signals:
            plans[nm] = trade_plan(_fmap[nm], spot, signals[nm])

    return {
        "now": now, "spot": spot, "ticker": profile["ticker"],
        "news_items": cf.fetch_news_items(profile["news_query"], 6),
        "earnings_date": (next_earnings(profile["ticker"])
                          if profile["kind"] == "stock" else None),
        "analyst": (analyst_view(profile["ticker"])
                    if profile["kind"] == "stock" else None),
        "options_pos": (options_positioning(profile["ticker"])
                        if profile["kind"] == "stock" else None),
        "social": (social_buzz(profile["ticker"])
                   if profile["kind"] == "stock" else None),
        "prev_close": (float(daily.iloc[-2]) if len(daily) >= 2 else None),
        "asset_key": asset_key, "asset_name": profile["name"],
        "kind": profile["kind"], "state_key": state_key,
        "unit": profile["unit"], "contract_label": profile["contract_label"],
        "contract_size": profile["contract_size"],
        "drivers_up": profile["drivers_up"], "drivers_down": profile["drivers_down"],
        "ref_beta": ref_beta, "ref_label": profile["ref_label"],
        "daily": daily, "hourly": hourly if have_hourly else daily,
        "have_hourly": have_hourly, "ref_loaded": not ref.empty,
        "news": news, "scenario": scenario, "combined_score": combined_score,
        "events": upcoming, "forecasts": forecasts, "backtests": backtests,
        "lgbm_used": any("lgbm" in hf.components for hf, _ in forecasts),
        "signals": signals, "headline_signal": headline,
        "calibration": calib, "learned_note": learned_note,
        "graded_now": graded, "evaluations_recent": read_recent_evals(state_key, 8),
        "pnl": strategies, "strategies": strategies, "best_strategy": best_strategy,
        "bt_mode": bt_mode, "n_made": n_made, "n_pending": n_pending,
        "indicators": ind, "plans": plans,
        "dir_acc": (calib["dir_ok"] / calib["dir_tot"]) if calib.get("dir_tot") else None,
        "dir_tot": calib.get("dir_tot", 0),
    }


if __name__ == "__main__":
    import json
    cfg = cf.load_config(str(cf.HERE / "config.yaml"))
    out = run_prediction(cfg, spot_override=6.4050)
    print(f"spot {out['spot']}  news {out['combined_score']:+.2f}  "
          f"lgbm={out['lgbm_used']}  dxy={out['dxy_loaded']}")
    for hf, _ in out["forecasts"]:
        print(f"{hf.name:<11} median {hf._end(hf.median):.3f}  "
              f"68% [{hf._end(hf.lo68):.3f},{hf._end(hf.hi68):.3f}]  "
              f"95% [{hf._end(hf.lo95):.3f},{hf._end(hf.hi95):.3f}]")
    print("backtests:", json.dumps(out["backtests"], indent=2))
