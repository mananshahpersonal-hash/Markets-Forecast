#!/usr/bin/env python3
"""app.py — one unified forecast dashboard for METALS and STOCKS.

Pick a mode (Metals or Stocks). For metals choose copper/gold/silver/aluminium;
for stocks type any ticker. The engine forecasts six horizons, auto-picks the
strategy that has worked best on that asset, and — above all — grades its own
past calls, counts the adjustments it makes, and shows whether it's improving.

Educational. Ranges and odds, not promises. NOT financial advice."""
import os
import sys
import time
import logging
import pandas as pd
import streamlit as st

# --- startup diagnostics: these print to the Streamlit deploy LOGS so we can see
# what's actually happening (Python version, price-fetch counts) instead of
# guessing from screenshots. Look for lines tagged [MH] in the log panel. ---
logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("market_helper")
_log.info(f"[MH] starting — Python {sys.version.split()[0]}")
print(f"[MH] starting — Python {sys.version.split()[0]}", flush=True)

import copper_forecaster as cf
import model_pro as mp
import assets
import storage
import datetime as _dt

try:
    import portfolio as pfm
except Exception:
    pfm = None

try:
    import pnl2026 as pnl
except Exception:
    pnl = None

try:
    import datafeed as feed
except Exception:
    feed = None

try:
    from streamlit_autorefresh import st_autorefresh
    HAVE_AUTOREFRESH = True
except Exception:
    HAVE_AUTOREFRESH = False

st.set_page_config(page_title="Markets forecast", page_icon="📈", layout="wide")


def _md(s: str):
    """Escape $ for PLAIN markdown so Streamlit's LaTeX doesn't pair them and
    mangle the text. Use for st.caption/markdown/etc that show dollar amounts."""
    return s.replace("$", "\\$")


def _html(s: str):
    """Render raw HTML via st.markdown. Inside an HTML block, Streamlit does NOT
    run its LaTeX processor, so dollar signs are safe as-is — we must NOT
    backslash-escape them (that would print a literal '\\'). We only guard the
    rare case of a bare '$' by using the HTML entity, which renders as '$'."""
    st.markdown(s.replace("$", "&#36;"), unsafe_allow_html=True)


def _last_price_store():
    """Session-persistent map of ticker -> last KNOWN market price. When a feed
    later fails, we reuse the last real price we ever saw instead of dropping to
    cost, so P/L is always shown and only goes truly blank if we've never once
    priced that ticker."""
    if "_last_px" not in st.session_state:
        st.session_state["_last_px"] = {}
    return st.session_state["_last_px"]


@st.cache_data(ttl=300, show_spinner=False)
def _cached_closes(symbols_tuple):
    """Batched holdings-price fetch, cached 5 min so page reloads during a Yahoo
    rate-limit window reuse the last good pull instead of re-hitting the API."""
    return mp._batch_close_series(list(symbols_tuple), chunk=40)


def _quote_cache():
    """Session store of successful quotes: {symbol: (quote, timestamp)}. Only
    SUCCESSES are kept, so rate-limited tickers are retried on the next load
    instead of being cached as failures for 15 minutes (that was why the same 9
    stayed stale). Quotes older than 15 min are considered refreshable."""
    if "_quote_store" not in st.session_state:
        st.session_state["_quote_store"] = {}
    return st.session_state["_quote_store"]


import json as _json


def _price_cache_path():
    return mp.STATE_DIR / "live_prices.json"


def _load_disk_prices():
    """Load persisted prices {symbol: [price, prev_close, change, pct, ts]}.
    Disk-backed so successful quotes survive reruns AND app reboots — this is
    what lets the portfolio accumulate all 38 over time instead of resetting to
    the same 29 every restart."""
    try:
        p = _price_cache_path()
        if p.exists():
            return _json.loads(p.read_text())
    except Exception:
        pass
    return {}


def _save_disk_prices(d):
    try:
        mp.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _price_cache_path().write_text(_json.dumps(d))
    except Exception:
        pass


def _cached_live_quotes(symbols_tuple):
    """Return {symbol: quote}. Merges a disk-persisted price cache (survives
    reruns/reboots) with fresh fetches. Each load fetches the still-missing or
    stale tickers first, shuffled, capped to stay under Finnhub's rate limit; the
    rest are served from the persisted cache. Over a few refreshes every holding
    gets priced and STAYS priced. Only successes are written."""
    import time as _t
    import random as _r
    out = {}
    if feed is None:
        return out
    disk = _load_disk_prices()
    store = _quote_cache()
    # seed session store from disk on first run
    for s, v in disk.items():
        if s not in store:
            store[s] = ({"price": v[0], "prev_close": v[1], "change": v[2],
                         "pct": v[3]}, v[4])
    now_ts = _t.time()
    STALE = 900
    to_fetch = []
    for s in symbols_tuple:
        rec = store.get(s)
        if rec and (now_ts - rec[1]) < STALE:
            out[s] = rec[0]
        else:
            to_fetch.append(s)
    try:
        print(f"[MH] quotes: {len(symbols_tuple)} requested, "
              f"{len(out)} served from cache, {len(to_fetch)} to fetch: "
              f"{to_fetch[:15]}", flush=True)
    except Exception:
        pass
    _r.shuffle(to_fetch)
    changed = False
    _ok = _fail = 0
    for s in to_fetch:
        try:
            q = feed.best_quote(s)
        except Exception:
            q = None
        if q and q.get("price"):
            store[s] = (q, now_ts)
            disk[s] = [q["price"], q.get("prev_close", q["price"]),
                       q.get("change", 0.0), q.get("pct", 0.0), now_ts]
            out[s] = q
            changed = True
            _ok += 1
        else:
            _fail += 1
    try:
        print(f"[MH] price fetch: {_ok} ok, {_fail} failed; "
              f"{len(out)}/{len(symbols_tuple)} priced total", flush=True)
    except Exception:
        pass
    # Serve any still-missing from disk even if stale (better than cost).
    for s in symbols_tuple:
        if s not in out and s in disk:
            v = disk[s]
            out[s] = {"price": v[0], "prev_close": v[1], "change": v[2], "pct": v[3]}
    if changed:
        _save_disk_prices(disk)
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_info_bundle(symbols_tuple):
    """Dividend + earnings facts. Yahoo's .info endpoint is dead (401) and each
    call times out, which was silently adding tens of seconds to the load, so we
    no longer call it. Div/yr comes from the verified own-payments table and
    Earnings/Ex-div from the researched tables in pnl2026 — the caller already
    falls back to those. This returns empties instantly so the table is fast."""
    return {t: {"div_rate": None, "div_yield": None, "ex_date": None,
                "earn_date": None} for t in symbols_tuple}


def fmtp(p):
    """Magnitude-aware price formatting (kept local so app.py never depends on
    model_pro for this)."""
    p = float(p)
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 10:
        return f"{p:.3f}"
    return f"{p:.4f}"

# Bridge Streamlit secrets -> environment so the persistent-memory layer
# (storage.py, used deep in the engine) can see the gist credentials on deploy.
try:
    for _k in ("GITHUB_TOKEN", "GIST_ID"):
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
except Exception:
    pass

# --- guard: if model_pro.py is an OLDER copy than this app.py, say so clearly
# instead of crashing with a redacted AttributeError. ---
_REQUIRED = ["run_prediction", "quick_read", "market_scan", "DEFAULT_SCAN_STOCKS",
             "fan_chart", "trade_plan", "gather_ideas", "apply_screen"]
_missing = [a for a in _REQUIRED if not hasattr(mp, a)]
if _missing:
    st.error(
        "### ⚠️ Your files are out of sync\n\n"
        "`app.py` is the latest version, but **`model_pro.py` is an older copy** "
        "— it's missing: `" + "`, `".join(_missing) + "`.\n\n"
        "**Fix it in your GitHub repo:**\n"
        "1. Open the repo → click **`model_pro.py`** → the 🗑️ trash icon → "
        "**Commit changes** (deletes the old one).\n"
        "2. **Add file → Upload files** → drag in the new **`model_pro.py`** from "
        "your latest download → **Commit changes**.\n"
        "3. Check that `model_pro.py` now shows the **same fresh timestamp** as "
        "`app.py` in the file list.\n"
        "4. Reboot: app page → **Manage app** (bottom-right) → **⋮ → Reboot app**.\n\n"
        "Tip: when updating, drag **all** the files in together so they can never "
        "fall out of sync.")
    st.caption(f"Loaded model_pro build: {getattr(mp, 'BUILD', 'unknown / very old')}")
    st.stop()

# One-time reset after the trend-aware engine change: wipe every asset's old
# track record (from the old trend-fighting logic) so the new engine is judged
# fresh. Runs once per install/gist (guarded by a marker), once per session.
if "reset_all_checked" not in st.session_state:
    try:
        if mp.maybe_reset_all_once():
            st.session_state["just_reset_all"] = True
    except Exception:
        pass
    st.session_state["reset_all_checked"] = True
if st.session_state.pop("just_reset_all", False):
    st.success("♻️ Engine upgraded to the trend-aware version — every score was "
               "reset to start fresh. From now on you're seeing the new engine's "
               "real track record.")

METAL_META = {"copper": ("Copper", "🟠"), "gold": ("Gold", "🟡"),
              "silver": ("Silver", "⚪"), "aluminium": ("Aluminium", "⚫")}

# ----------------------------------------------------------- mode + picker ---
st.title("📈 Market Helper")
st.caption(f"🏷️ **App version:** {getattr(mp, 'BUILD', 'unknown')}  ·  "
           f"if this version line matches what you just uploaded, your update is live.")
with st.expander("❓ New here? What does this app do? (tap to read)"):
    st.markdown(
        "This app watches prices — **metals** like copper and gold, and **stocks** "
        "like Apple.\n\n"
        "For anything you pick, it does three simple things:\n"
        "1. **Guesses** whether the price is more likely to go **up** or **down** next.\n"
        "2. Gives you a simple **buy / take-profit / get-out** plan, if you want to trade.\n"
        "3. **Keeps score** of how often its guesses were right, so you know how much "
        "to trust it.\n\n"
        "It **cannot** know the future — nobody can. So it deals in **odds, not "
        "promises.** Only ever use money you can afford to lose.\n\n"
        "**The tabs below:**\n"
        "- **Metals / Stocks** — look at one thing in detail.\n"
        "- **Overview** — a quick look at everything at once.\n"
        "- **Top & Bottom** — which things look strongest up / weakest down right now.\n"
        "- **Stock Ideas** — lists of stocks that match simple ideas (like 'going up "
        "and cheap').")

mode = st.radio("What do you want to look at?",
                ["Metals", "Stocks", "Overview (all at once)",
                 "Top & Bottom (scan)", "Stock Ideas (screens)",
                 "My Portfolio (CSV)"], horizontal=True)

# ===================== 🔴 LIVE MODE + pinned strong-signal ALERT =============
live = st.session_state.get("live_mode", False)
REFRESH_MIN = 5
if mode != "My Portfolio (CSV)":
    lc1, lc2, lc3 = st.columns([1.1, 1.2, 1])
    with lc1:
        live = st.toggle("🔴 Live mode", value=st.session_state.get("live_mode", False),
                         key="live_mode", help="Auto-refreshes every 5 minutes while the "
                         "app is open, re-checking prices, news and strong signals.")
    with lc2:
        check_now = st.button("🚨 Check for strong signals now", width='stretch')
    with lc3:
        if live:
            if HAVE_AUTOREFRESH:
                st_autorefresh(interval=REFRESH_MIN * 60 * 1000, key="live_tick")
                st.caption(f"🔴 LIVE · every {REFRESH_MIN} min")
            else:
                st.caption("Add-on missing — use the button to refresh.")

    # decide whether to (re)run the background alert scan (cache ~10 min)
    _now = time.time()
    _last = st.session_state.get("alert_ts")
    need_alert = check_now or ("alert_reads" not in st.session_state)
    if live and (_last is None or (_now - _last) > 10 * 60):
        need_alert = True
    if need_alert:
        with st.spinner("Scanning the market for strong signals…"):
            _cfg = cf.load_config(str(cf.HERE / "config.yaml"))
            try:
                st.session_state["alert_reads"] = mp.quick_universe_reads(_cfg, mp.ALERT_UNIVERSE)
            except Exception:
                st.session_state["alert_reads"] = st.session_state.get("alert_reads", [])
            st.session_state["alert_ts"] = _now

    _reads = st.session_state.get("alert_reads", [])
    _sbuys = sorted([r for r in _reads if mp.strong_trend_signal(r) == "BUY"],
                    key=lambda r: r.get("trend_pct", 0), reverse=True)
    _ssells = sorted([r for r in _reads if mp.strong_trend_signal(r) == "SELL"],
                     key=lambda r: r.get("trend_pct", 0))


    def _sig_line(r, kind):
        arrow = "▲" if kind == "buy" else "▼"
        return (f"**{r['name']}** — ${fmtp(r['spot'])} · {arrow} {r.get('trend_pct', 0):+.0f}% "
                f"over 3mo · {r['ind_bull']}▲/{r['ind_bear']}▼ · next-wk odds "
                f"{r['p_up']*100:.0f}%")


    if _sbuys:
        st.markdown(
            "<div style='background:#0F6E56;color:white;border-radius:10px;"
            "padding:14px 18px;font-size:17px;'>🚨🟢 <b>STRONG BUY SETUPS (uptrend "
            "momentum)</b></div>", unsafe_allow_html=True)
        for r in _sbuys[:10]:
            st.markdown("🟢 " + _sig_line(r, "buy"))
        st.caption("Flagged for a strong **3-month uptrend** (above key averages, not "
                   "overextended) — a momentum edge that plays out over weeks-to-months. "
                   "The near-50% next-week odds are honest: short-term is still a coin "
                   "flip; the trend is the edge. Odds, not a promise — use a stop.")
    if _ssells:
        st.markdown(
            "<div style='background:#A32D2D;color:white;border-radius:10px;"
            "padding:14px 18px;font-size:17px;margin-top:8px;'>🚨🔴 <b>STRONG SELL / "
            "AVOID (downtrend momentum)</b></div>", unsafe_allow_html=True)
        for r in _ssells[:10]:
            st.markdown("🔴 " + _sig_line(r, "sell"))
        st.caption("In a strong **3-month downtrend** (below key averages, not yet "
                   "oversold) — momentum is against these; a heads-up if you hold them. "
                   "**Not advice.** Odds, not certainty.")
    if not _sbuys and not _ssells:
        ts = st.session_state.get("alert_ts")
        when = time.strftime("%H:%M", time.localtime(ts)) if ts else "—"
        st.info(f"✅ No strong buy or sell setups right now (checked {when}). That's the "
                f"honest, healthy default — clean, strong trends are the exception, not "
                f"the rule. {'Live mode keeps watching.' if live else 'Turn on Live mode to keep watching.'}")

    st.divider()
# ===================== 📊 OVERVIEW — all metals + your stocks at a glance ====
if mode == "Overview (all at once)":
    st.title("📊 Overview — everything at a glance")
    st.caption("A quick read across all metals and your stocks: price, the "
               "model's weekly lean, the odds, and the indicator tally. A **scan, "
               "not advice** — open Metals or Stocks for the full plan on any one.")
    default_stocks = "AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AMD, NFLX, JPM"
    tickers_str = st.text_input("Stocks to include (comma-separated — edit freely)",
                                value=default_stocks)
    go = st.button("🔄  Load / refresh overview", type="primary",
                   width='stretch')
    okey = "OV:" + tickers_str
    if go or st.session_state.get("ov_key") != okey or "overview" not in st.session_state:
        cfg = cf.load_config(str(cf.HERE / "config.yaml"))
        metals = ["copper", "gold", "silver", "aluminium"]
        stocks = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
        rows = []
        prog = st.progress(0.0, text="Reading markets…")
        total = max(len(metals) + len(stocks), 1)
        done = 0
        for mk in metals:
            rows.append(mp.quick_read(cfg, asset_key=mk))
            done += 1
            prog.progress(done / total, text=f"Reading {mk}…")
        for tk in stocks:
            rows.append(mp.quick_read(cfg, stock_ticker=tk))
            done += 1
            prog.progress(done / total, text=f"Reading {tk}…")
        prog.empty()
        st.session_state["overview"] = rows
        st.session_state["ov_key"] = okey

    rows = st.session_state["overview"]
    good = [r for r in rows if not r.get("error")]
    good.sort(key=lambda r: r.get("p_up", 0.5), reverse=True)
    emolean = lambda l: {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "HOLD": "⚪ HOLD"}.get(l, l)
    if good:
        tbl = [{"Asset": r["name"], "Price": f"${fmtp(r['spot'])}",
                "Signal": emolean(r["lean"]), "Odds ↑": f"{r['p_up']*100:.0f}%",
                "~1-wk move": f"{r['move_pct']:+.1f}%",
                "Indicators": f"{r['ind_bull']}▲ / {r['ind_bear']}▼"} for r in good]
        st.table(pd.DataFrame(tbl).set_index("Asset"))
        buys = [r["name"] for r in good if r["lean"] == "BUY"]
        sells = [r["name"] for r in good if r["lean"] == "SELL"]
        if buys:
            st.markdown("**🟢 Leaning up:** " + ", ".join(buys))
        if sells:
            st.markdown("**🔴 Leaning down:** " + ", ".join(sells))
        if not buys and not sells:
            st.info("No clear directional leans across the list right now — mostly "
                    "coin-flips. That's normal and honest for short horizons.")
    errs = [r for r in rows if r.get("error")]
    if errs:
        st.caption("No data right now for: " + ", ".join(r["name"] for r in errs) +
                   " — Yahoo can rate-limit many requests; try refresh in a moment.")
    st.caption("Quick read = price-based ensemble + indicators on daily data "
               "(no news or backtest — those are in the full Metals/Stocks view). "
               "Sorted by odds of going up. Short-horizon direction is near "
               "50/50; this is a scan, **not financial advice**.")
    st.stop()

# ===================== 🏆 TOP & BOTTOM — ranked market scan ==================
if mode == "Top & Bottom (scan)":
    st.title("🏆 Top & Bottom — the market ranked right now")
    st.caption("Runs every metal + a whole universe of stocks through the model and "
               "ranks them: strongest **bullish lean** up top, strongest "
               "**bearish lean** at the bottom. These are the model's leans and "
               "odds — **not facts, not guarantees.** The scan's own measured "
               "hit-rate is shown below, and it learns from every run.")
    uni_choice = st.selectbox(
        "Which stocks should it scan?", list(mp.SCAN_UNIVERSES.keys()), index=3,
        help="Pick a real index list for a convincing top 10, or 'My custom list' "
             "to type your own.")
    preset = mp.SCAN_UNIVERSES[uni_choice]
    if preset is None:
        tickers_str = st.text_area("Your stocks (comma-separated)",
                                   value=", ".join(mp.DEFAULT_SCAN_STOCKS), height=90)
        stocks = [t.strip().upper() for t in tickers_str.split(",") if t.strip()]
        uni_id = "custom:" + tickers_str
    else:
        stocks = list(preset)
        uni_id = uni_choice
        st.caption(f"Scanning **{len(stocks)} stocks** in {uni_choice} + 4 metals. "
                   f"Bigger lists take longer (~1–3 min) and Yahoo may skip a few — "
                   f"just run it again if so.")
    go = st.button("🔍  Run market scan", type="primary", width='stretch')
    skey = "SCAN:" + uni_id
    if go or st.session_state.get("scan_key") != skey or "scan" not in st.session_state:
        cfg = cf.load_config(str(cf.HERE / "config.yaml"))
        prog = st.progress(0.0, text="Scanning the market… (this can take a minute or two)")

        def _cb(frac, label):
            try:
                prog.progress(min(frac, 1.0), text=f"Reading {label}…")
            except Exception:
                pass
        st.session_state["scan"] = mp.market_scan(cfg, stocks, progress=_cb)
        st.session_state["scan_key"] = skey
        prog.empty()
    sc = st.session_state["scan"]
    st.caption(f"✅ Ranked **{sc.get('n_scanned', 0)}** names this run "
               f"(the top/bottom 10 below are drawn from all of them).")

    if sc["acc"] is not None:
        st.metric("Scan track record — direction right", f"{sc['acc']*100:.0f}%",
                  f"{sc['graded']} calls graded")
        st.caption("How often the scan's past up/down calls have actually been "
                   "correct. Expect it near 50% — that's the honest reality. Read "
                   "the ranking as *relative* strength, not certainty.")
    else:
        st.info("First scan — these calls are now logged and get graded over the "
                "next week. The scan's real accuracy will show up here as they come "
                "due, and it updates itself every run.")

    def _row(r, i):
        em = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(r["lean"], "")
        return {"#": i, "Asset": r["name"], "Price": f"${fmtp(r['spot'])}",
                "Lean": f"{em} {r['lean']}", "Conviction": r["conv"],
                "Odds ↑": f"{r['p_up']*100:.0f}%", "~1-wk": f"{r['move_pct']:+.1f}%",
                "Ind": f"{r['ind_bull']}▲/{r['ind_bear']}▼"}

    strong_only = st.toggle(
        "⭐ Show only STRONG buys & sells (hide the coin-flips)", value=False,
        help="Keeps only names with a real signal (moderate conviction or stronger). "
             "This list is often short — sometimes empty — and that's the honest answer.")

    if strong_only:
        buys = sorted([r for r in sc["all"] if mp.strong_trend_signal(r) == "BUY"],
                      key=lambda r: r.get("trend_pct", 0), reverse=True)
        sells = sorted([r for r in sc["all"] if mp.strong_trend_signal(r) == "SELL"],
                       key=lambda r: r.get("trend_pct", 0))
        b1, b2 = st.columns(2)
        with b1:
            st.markdown("#### 🟢 Strong BUYs")
            if buys:
                st.table(pd.DataFrame([_row(r, i + 1) for i, r in enumerate(buys)]
                                      ).set_index("#"))
            else:
                st.info("**No strong buys right now.** Nothing crossed the bar — the "
                        "market isn't handing out clear buy signals today. That's "
                        "normal; strong setups are rare.")
        with b2:
            st.markdown("#### 🔴 Strong SELLs")
            if sells:
                st.table(pd.DataFrame([_row(r, i + 1) for i, r in enumerate(sells)]
                                      ).set_index("#"))
            else:
                st.info("**No strong sells right now.** Nothing crossed the bar today.")
        st.caption(f"Filtered from **{sc.get('n_scanned', 0)}** names — only those "
                   f"with a real signal (moderate conviction or stronger) are shown. "
                   f"Everything weaker is hidden. Still **odds, not certainty** — "
                   f"use a stop and size small.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("#### 🟢 Strongest BUY leans (top 10)")
            if sc["top"]:
                st.table(pd.DataFrame([_row(r, i + 1) for i, r in enumerate(sc["top"])]
                                      ).set_index("#"))
        with c2:
            st.markdown("#### 🔴 Strongest SELL leans (top 10)")
            if sc["bottom"]:
                st.table(pd.DataFrame([_row(r, i + 1) for i, r in enumerate(sc["bottom"])]
                                      ).set_index("#"))
        st.caption("These are the 10 leaning **up** the most and the 10 leaning "
                   "**down** the most, out of everything scanned — ranked even when "
                   "the lean is small. Check the **Conviction** column: "
                   "*strong/moderate* is a real signal; *weak* or *—* is a coin toss. "
                   "Want just the real ones? Flip the **⭐ strong only** switch above.")

        strong = [r for r in sc["all"] if mp.strong_trend_signal(r)]
        if not strong:
            st.warning("⚠️ **No strong buy or sell setups right now.** The names "
                       "above are the *relative* leaders and laggards by next-week "
                       "lean, but none are in a strong, clean trend — no "
                       "high-confidence momentum trades today. Often the smart move "
                       "is to wait.")
        else:
            b = [r["name"] for r in strong if mp.strong_trend_signal(r) == "BUY"]
            s = [r["name"] for r in strong if mp.strong_trend_signal(r) == "SELL"]
            if b:
                st.success("🟢 **Strong BUY setups (uptrend momentum):** " + ", ".join(b))
            if s:
                st.error("🔴 **Strong SELL setups (downtrend momentum):** " + ", ".join(s))
    if sc["errors"]:
        st.caption("No data this run for: " + ", ".join(sc["errors"]) +
                   " — Yahoo can rate-limit a big scan; try again in a minute.")
    st.caption("Ranked by the model's weekly lean on daily data. Leans and odds, "
               "**not advice and not certainty** — short-horizon direction is near "
               "a coin flip, as the track record shows. Open Metals or Stocks for "
               "the full plan (entry/target/stop) on any name.")
    st.stop()

# ===================== 💡 STOCK IDEAS — factor screens =======================
if mode == "Stock Ideas (screens)":
    st.title("💡 Stock Ideas — factor screens")
    st.caption("Runs a basket of stocks through factor screens — momentum, value, "
               "growth, income — the way a screener like Fidelity's does. These are "
               "**idea generators to research, not advice or recommendations**, and "
               "past performance is no guarantee of future results. The last columns "
               "show your model's own technical lean as a cross-check.")
    uni_str = st.text_area("Stocks to screen (comma-separated — edit freely)",
                           value=", ".join(mp.DEFAULT_IDEAS_UNIVERSE), height=90)
    screen = st.radio("Screen", mp.IDEA_SCREENS, horizontal=True)
    go = st.button("💡  Run screens", type="primary", width='stretch')
    ikey = "IDEAS:" + uni_str
    if go or st.session_state.get("ideas_key") != ikey or "ideas" not in st.session_state:
        cfg = cf.load_config(str(cf.HERE / "config.yaml"))
        uni = [t.strip().upper() for t in uni_str.split(",") if t.strip()]
        prog = st.progress(0.0, text="Gathering data… fundamentals are slow (~1–2 min)")

        def _cb(frac, label):
            try:
                prog.progress(min(frac, 1.0), text=f"Reading {label}…")
            except Exception:
                pass
        st.session_state["ideas"] = mp.gather_ideas(cfg, uni, progress=_cb)
        st.session_state["ideas_key"] = ikey
        prog.empty()
    rows = st.session_state["ideas"]
    matches = mp.apply_screen(rows, screen)

    def _mc(v):
        if not v:
            return "—"
        if v >= 1e12:
            return f"${v/1e12:.1f}T"
        if v >= 1e9:
            return f"${v/1e9:.0f}B"
        return f"${v/1e6:.0f}M"
    emolean = lambda l: {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(l, "")

    if matches:
        tbl = []
        for r in matches[:15]:
            row = {"Ticker": r["ticker"], "Price": f"${fmtp(r['spot'])}",
                   "YTD": f"{r['ytd']:+.0f}%" if r.get("ytd") is not None else "—",
                   "Mkt cap": _mc(r.get("market_cap"))}
            if screen == "Momentum + Value":
                row["PEG"] = f"{r['peg']:.2f}" if r.get("peg") else "—"
            elif screen == "Momentum + Growth":
                g = r.get("earnings_growth") or r.get("revenue_growth")
                row["Growth"] = f"{g*100:.0f}%" if g is not None else "—"
            elif screen == "Momentum + Income":
                row["Yield"] = f"{r['div_yield']*100:.1f}%" if r.get("div_yield") else "—"
            row["Model lean"] = f"{emolean(r['lean'])} {r['lean']}"
            row["Odds ↑"] = f"{r['p_up']*100:.0f}%"
            tbl.append(row)
        st.markdown(f"**{screen} — {len(matches)} match(es)** "
                    f"(showing up to 15, sorted like the article)")
        st.table(pd.DataFrame(tbl).set_index("Ticker"))
    else:
        st.info(f"No stocks in your list passed the **{screen}** screen right now. "
                f"That can mean the move isn't there, or (for value/growth/income) "
                f"the free fundamental data was missing for these names. Try the "
                f"**Momentum** screen — it's price-based and the most reliable.")

    errs = [r for r in rows if r.get("error")]
    if errs:
        st.caption("No data this run for: " + ", ".join(r["ticker"] for r in errs) + ".")
    have_f = sum(1 for r in rows if "error" not in r and
                 (r.get("peg") or r.get("div_yield") or r.get("earnings_growth")))
    tot = sum(1 for r in rows if "error" not in r)
    if screen != "Momentum" and tot and have_f < tot * 0.5:
        st.warning(f"Heads-up: fundamentals (PEG / growth / yield) came back for only "
                   f"{have_f} of {tot} names from the free feed, so this screen is "
                   f"thinner than it should be. The **Momentum** screen doesn't need "
                   f"fundamentals and is the most reliable here.")
    st.caption("Screens are idea generators — **not advice, not a recommendation, "
               "not certainty.** Mind concentration risk (results often cluster in "
               "one sector), diversify, and research each name against your own "
               "goals and risk tolerance. Fundamentals come from Yahoo's free data "
               "and may be delayed or incomplete. Open the Stocks tab for the full "
               "plan on any ticker.")
    st.stop()

# ===================== 💼 MY PORTFOLIO — Robinhood CSV =======================
if mode == "My Portfolio (CSV)":
    st.title("💼 My Portfolio")
    if pfm is None:
        st.error("`portfolio.py` is missing from the app files — re-upload **all** "
                 "files (including the new `portfolio.py`) to your repo, then reboot.")
        st.stop()
    _pc1, _pc2 = st.columns([1, 2])
    with _pc1:
        pf_live = st.toggle("🔴 Live updates", value=st.session_state.get("live_mode", False),
                            key="live_mode", help="Refreshes prices & your chart every "
                            "5 minutes while the app is open.")
    if pf_live and HAVE_AUTOREFRESH:
        st_autorefresh(interval=5 * 60 * 1000, key="pf_tick")
        _pc2.caption("🔴 LIVE · updating every 5 min")
    # ---- your holdings seed (from your Robinhood position screenshots) ----
    # ticker, shares, average cost. Used only to PRE-FILL the editable table the
    # first time; after you edit + save, your saved version is the source.
    SEED_HOLDINGS = [
        ("NFLX", 6000, 116.05), ("AAPL", 1000, 166.42), ("QQQI", 2500.014162, 48.30),
        ("SPYI", 2500, 46.80), ("AMZN", 300, 191.26), ("NVDA", 300, 108.40),
        ("QDTE", 1700.813449, 34.52), ("IAUI", 800.00998, 54.87), ("WMT", 300, 109.07),
        ("MCD", 100, 272.45), ("JEPI", 200, 57.74), ("JEPQ", 200, 58.59),
        ("SPHD", 200, 52.38), ("IWMI", 200, 51.89), ("VZ", 200, 46.40),
        ("CSHI", 200, 49.75), ("HYBI", 200, 49.31), ("BNDI", 200, 46.40),
        ("VXUS", 100, 79.66), ("BTCI", 200, 30.71), ("QQQH", 100, 53.54),
        ("SCHD", 200, 32.46), ("PFE", 200, 26.64), ("T", 200, 23.46),
        ("IBIT", 100, 45.64), ("ETHA", 100, 19.84), ("IYRI", 200, 49.50),
        ("VOO", 1, 708.75), ("SPY", 1, 770.80), ("QQQ", 1, 717.89),
        ("VGT", 1, 120.64), ("VOOG", 1, 84.41), ("ITOT", 1, 168.85),
        ("BRK.B", 1, 505.76), ("COPX", 1, 78.87), ("NUKZ", 1, 71.16),
        ("EWZ", 1, 40.20), ("GCOW", 1, 45.79),
    ]
    st.caption("Everything below is driven by **your holdings table** (shares + "
               "average cost, valued at live prices). The Robinhood CSV is "
               "**optional** — add it only for dividend income and realized-gain "
               "history. Never share your login with any app.")
    # restore last saved CSV once per session (optional)
    if "pf" not in st.session_state and not st.session_state.get("pf_restore_tried"):
        st.session_state["pf_restore_tried"] = True
        try:
            storage.pull("pfcsv")
            _pth = mp.STATE_DIR / "pfcsv_data.csv"
            if _pth.exists():
                st.session_state["pf"] = pfm.analyze(pfm.parse_csv(_pth.read_text()))
                st.session_state["pf_name"] = "your saved CSV"
        except Exception:
            pass
    with st.expander("📎 Optional: add Robinhood CSV for dividends & realized gains"):
        up = st.file_uploader("Upload your Robinhood report (CSV)", type=["csv"])
        if up is not None:
            _k = f"{up.name}:{up.size}"
            if st.session_state.get("pf_key") != _k:
                try:
                    _txt = up.getvalue().decode("utf-8", errors="ignore")
                    st.session_state["pf"] = pfm.analyze(pfm.parse_csv(_txt))
                    st.session_state["pf_key"] = _k
                    st.session_state["pf_name"] = up.name
                    try:
                        mp.STATE_DIR.mkdir(parents=True, exist_ok=True)
                        (mp.STATE_DIR / "pfcsv_data.csv").write_text(_txt)
                        storage.push("pfcsv")
                    except Exception:
                        pass
                except Exception as e:
                    st.error(f"Couldn't read that CSV: {e}")
        if "pf" in st.session_state:
            _P0 = st.session_state["pf"]
            st.caption(f"📎 Loaded: **{st.session_state.get('pf_name', 'your CSV')}** — "
                       f"{_P0.get('n_rows', '?')} transactions.")
            if _P0.get("skipped"):
                st.caption(f"(skipped {_P0['skipped']} unreadable line(s))")
            if st.button("🗑️ Forget saved CSV"):
                try:
                    for _f in mp.STATE_DIR.glob("pfcsv_*"):
                        _f.unlink()
                    storage.delete("pfcsv")
                except Exception:
                    pass
                for _kk in ("pf", "pf_key", "pf_name"):
                    st.session_state.pop(_kk, None)
                st.rerun()

    P = st.session_state.get("pf")
    pos = P["positions"] if P else {}
    rz = P["realized"] if P else pd.DataFrame(columns=["date", "instrument", "gain", "term", "class"])
    dv = P["dividends"] if P else pd.DataFrame(columns=["date", "instrument", "amount"])
    now = _dt.datetime.utcnow()

    # ---- YOUR CURRENT HOLDINGS (editable, tucked into an expander) ----
    # The rich Holdings table lower down is the main view; this editor is only
    # for changing shares/cost, so it lives in an expander to avoid two big
    # tables competing for attention.
    with st.expander("✏️ Edit my holdings (shares & average cost)", expanded=False):
        st.caption("Pre-filled from your Robinhood positions. Type over shares or "
                   "average cost, add/remove rows, then **Save**. The live "
                   "Holdings table below updates from this.")
        if "pf_holdings" not in st.session_state:
            saved = None
            try:
                storage.pull("pfhold")
                _hp = mp.STATE_DIR / "pfhold_data.csv"
                if _hp.exists():
                    saved = pd.read_csv(_hp)
            except Exception:
                saved = None
            if saved is not None and len(saved) and "Avg cost" in saved.columns:
                st.session_state["pf_holdings"] = saved
            else:
                st.session_state["pf_holdings"] = pd.DataFrame(
                    [{"Ticker": t, "Shares": s, "Avg cost": a} for t, s, a in SEED_HOLDINGS])
        edited = st.data_editor(
            st.session_state["pf_holdings"], num_rows="dynamic",
            width='stretch', key="pf_hold_editor",
            column_config={
                "Shares": st.column_config.NumberColumn(format="%.4f"),
                "Avg cost": st.column_config.NumberColumn(format="$%.2f",
                            help="Your average cost per share (from Robinhood). Leave 0 if unknown.")})
        _hc1, _hc2 = st.columns([1, 3])
        if _hc1.button("💾 Save holdings", width='stretch'):
            st.session_state["pf_holdings"] = edited
            try:
                mp.STATE_DIR.mkdir(parents=True, exist_ok=True)
                edited.to_csv(mp.STATE_DIR / "pfhold_data.csv", index=False)
                storage.push("pfhold")
                _hc2.success("Saved — remembered across visits, laptop and phone.")
            except Exception:
                _hc2.info("Saved for this session.")

    holdings = {}   # ticker -> (shares, avg_cost)
    for _, r in edited.iterrows():
        t = str(r.get("Ticker", "")).upper().strip()
        try:
            sh = float(r.get("Shares", 0) or 0)
            ac = float(r.get("Avg cost", 0) or 0)
        except Exception:
            continue
        if t and sh > 0:
            holdings[t] = (sh, ac)
    cls_of = {i: p["class"] for i, p in pos.items()}
    price_syms, sym_cls = {}, {}
    _dropped = []
    for t in holdings:
        c = cls_of.get(t, "Crypto" if t in pfm.CRYPTO else "Stocks & ETFs")
        # If the stored class isn't priceable (e.g. an old "Options"/"Futures"/
        # blank tag from saved data), fall back to Stocks & ETFs so the holding
        # still gets a price instead of being silently dropped. Real crypto stays
        # crypto.
        if c not in ("Crypto", "Stocks & ETFs"):
            c = "Crypto" if t in pfm.CRYPTO else "Stocks & ETFs"
        sym_cls[t] = c
        s = pfm.yahoo_symbol(t, c)
        if s:
            price_syms[t] = s
        else:
            _dropped.append((t, c))
    try:
        if _dropped:
            print(f"[MH] DROPPED (no price symbol): {_dropped}", flush=True)
    except Exception:
        pass
    closes = {}
    _live_q = {}
    if price_syms:
        with st.spinner("Fetching live prices for your holdings…"):
            _batch = _cached_closes(tuple(sorted(set(price_syms.values()))))
        closes = {t: _batch.get(s) for t, s in price_syms.items()}
        # Finnhub live quotes keyed by holding ticker (reliable current price).
        _live_q = _cached_live_quotes(tuple(sorted(price_syms.keys())))
        try:
            print(f"[MH] price_syms has {len(price_syms)} tickers; "
                  f"_live_q returned {len(_live_q)}; "
                  f"holdings={len(holdings)}", flush=True)
        except Exception:
            pass
        if feed is not None:
            _n = len(_live_q)
            _tot_syms = len(price_syms)
            if _n >= _tot_syms:
                st.success(f"📡 **All {_n} holdings priced** via Finnhub. "
                           f"(Weekends show Friday's close.)")
            elif _n:
                st.info(f"📡 **{_n} of {_tot_syms} holdings priced** so far. The "
                        f"rest fill in automatically within a minute — Finnhub's "
                        f"rate limit means they load a few per refresh, and each "
                        f"success is kept. Turn on 🔴 Live updates and they'll "
                        f"complete on their own, or reload once.")
            else:
                st.error("📡 No prices returned this refresh. Reload in a minute.")
            # Auto-complete: the disk cache keeps every success, so each rerun
            # fetches ONLY the still-missing tickers — a handful — which stay
            # under the rate limit and succeed. We pause ~4s (to let Finnhub's
            # per-minute window recover) then rerun, until all are priced or we
            # hit the attempt cap.
            if 0 < len(_live_q) < len(price_syms):
                _tries = st.session_state.get("_price_fill_tries", 0)
                if _tries < 8:
                    st.session_state["_price_fill_tries"] = _tries + 1
                    import time as _tt
                    _tt.sleep(4)
                    st.rerun()
            else:
                st.session_state["_price_fill_tries"] = 0
            with st.expander("🔬 Diagnose price feeds (which sources work here?)"):
                st.caption("Runs a live test of each price source from this app's "
                           "server, on a stock (AAPL) and an ETF (QDTE), so we can "
                           "see exactly which feed works in your environment.")
                if st.button("Run feed test", key="feedtest"):
                    with st.spinner("Testing sources…"):
                        _res = feed.source_selftest()
                    for _nm, _sok, _eok, _det in _res:
                        _mark = "✅" if (_sok or _eok) else "❌"
                        st.write(f"{_mark} **{_nm}** — {_det}")
                    _any = any(s or e for _, s, e, _ in _res)
                    if _any:
                        st.success("At least one source works — the app will use "
                                   "it. If some holdings are still stale, they'll "
                                   "fill in on the next reload as each caches.")
                    else:
                        st.error("No source responded from this environment. That "
                                 "points to an outbound-network restriction on the "
                                 "host, not the code — tell me and I'll adapt.")

    total_val = total_cost = 0.0
    stale_val = 0.0            # value of positions running on fallback (no live price)
    win = {"today": 0.0, "week": 0.0, "month": 0.0, "ytd": 0.0}
    rows, hseries, missing = [], {}, []
    for inst, (shares, avgc) in holdings.items():
        c = closes.get(inst)
        cls = sym_cls.get(inst, "Stocks & ETFs")
        cost = shares * avgc
        q = _live_q.get(inst)                     # Finnhub live quote, if any
        # Buy/Hold/Sell from the app's own strong-trend rule (same definition as
        # the top-of-app alert), computed from the price series we already have.
        sig = "Hold"
        if pnl is not None and c is not None and len(c) >= 60:
            sig = pnl.signal_from_read(pnl.read_from_closes(c))

        _lastpx = _last_price_store()
        # Determine the best available price, in priority order:
        #   1) live quote (Finnhub/Stooq)  2) latest close from history
        #   3) last price we ever saw this session  4) average cost (last resort)
        price = None
        today_move = 0.0
        is_stale = False
        if q:
            price = q["price"]
            today_move = q["change"] * shares
            _lastpx[inst] = price                 # remember it
        elif c is not None and len(c) >= 2:
            w = pfm.market_windows(c, now)
            price = w["now"]
            today_move = shares * (price - w["today"])
            _lastpx[inst] = price
        elif inst in _lastpx:
            price = _lastpx[inst]                 # last known market price
            is_stale = True                        # not refreshed this load
        elif avgc:
            price = avgc                           # never priced — cost as proxy
            is_stale = True

        if price is None:
            # Truly no price and no cost: cannot value.
            rows.append({"inst": inst, "class": cls, "shares": shares, "avgc": avgc,
                         "price": None, "value": None, "cost": cost,
                         "tot": None, "totpct": None, "today": None,
                         "signal": sig, "stale": True})
            missing.append(inst)
            continue

        val = shares * price
        total_val += val
        total_cost += cost
        if is_stale:
            stale_val += val
            missing.append(inst)
        else:
            for k in ("week", "month", "ytd"):
                if c is not None and len(c) >= 2:
                    w = pfm.market_windows(c, now)
                    win[k] += shares * (price - w[k])
            win["today"] += today_move
        # P/L is ALWAYS computed from the best price we have — never blank when
        # we have any price at all (that was the bug you spotted).
        rows.append({"inst": inst, "class": cls, "shares": shares, "avgc": avgc,
                     "price": price, "value": val, "cost": cost,
                     "tot": val - cost if avgc else None,
                     "totpct": (val/cost - 1)*100 if cost else None,
                     "today": today_move if not is_stale else 0.0,
                     "signal": sig, "stale": is_stale})
        if c is not None and len(c) >= 2:
            hseries[inst] = c.tail(400) * shares
    rz_sums = (pfm.period_sums(rz, "gain", now) if not rz.empty
               else {k: 0.0 for k in ("today", "week", "month", "ytd", "all")})
    dv_sums = pfm.period_sums(dv, "amount", now)

    st.markdown("### 💰 Right now")
    _tot_unreal = total_val - total_cost
    _live_val = total_val - stale_val
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Portfolio value", f"${total_val:,.0f}",
              (f"{stale_val/total_val*100:.0f}% at cost (no live price)"
               if stale_val and total_val else None), delta_color="off")
    m2.metric("Total gain/loss",
              f"${_tot_unreal:+,.0f}",
              f"{(_tot_unreal/total_cost*100 if total_cost else 0):+.1f}%")
    m3.metric("Today", f"${win['today']:+,.0f}")
    m4.metric("This year (price)", f"${win['ytd']:+,.0f}")
    if missing:
        st.warning(
            f"⚠️ **{len(missing)} position(s) have no live price this refresh** "
            f"({', '.join(missing)}). To keep your total complete, these are valued "
            f"at your **average cost** for now "
            f"(\\${stale_val:,.0f}, {stale_val/total_val*100:.0f}% of the total) — "
            f"so the real total is likely a bit higher. This clears as the feeds "
            f"catch up; gain/loss on these shows \\$0 until a live price returns.")
    st.caption("Value & gains use **live prices** where available; any position "
               "without one falls back to your average cost (flagged above) so it "
               "never silently drops out of the total. 'Today/This year' are price "
               "moves on live-priced holdings. Turn on 🔴 Live updates to refresh "
               "every ~5 min.")
    if hseries:
        _hdf = pd.DataFrame(hseries).ffill()
        _tot = _hdf.sum(axis=1).dropna()
        # The history series only covers holdings that HAVE a price history from
        # the feed; that can be a subset of the portfolio, so its raw level may
        # be far below your true equity. Scale it so its last point equals the
        # real live total — this keeps the day-to-day SHAPE while making the
        # magnitude correct and consistent with the "Right now" value.
        if len(_tot) > 5 and _tot.iloc[-1] and total_val:
            _tot = _tot * (total_val / float(_tot.iloc[-1]))
        if len(_tot) > 5:
            _rng = st.radio("Chart range", ["1W", "1M", "3M", "YTD", "All"],
                            index=2, horizontal=True, key="pf_range")
            _tot.index = pd.to_datetime(_tot.index)
            _t0 = pd.Timestamp(now.date())
            _cut = {"1W": _t0 - pd.Timedelta(days=7),
                    "1M": _t0 - pd.Timedelta(days=30),
                    "3M": _t0 - pd.Timedelta(days=90),
                    "YTD": pd.Timestamp(now.year, 1, 1),
                    "All": _tot.index.min()}[_rng]
            _seg = _tot[_tot.index >= _cut]
            if len(_seg) < 2:
                _seg = _tot
            _chg = float(_seg.iloc[-1] - _seg.iloc[0])
            _pct = (_chg / _seg.iloc[0] * 100) if _seg.iloc[0] else 0
            _green = _chg >= 0
            _col = "#00C805" if _green else "#FF5000"
            _html(f"<span style='font-size:22px;font-weight:700'>"
                  f"${_seg.iloc[-1]:,.0f}</span> &nbsp; "
                  f"<span style='color:{_col};font-weight:600'>"
                  f"{'▲' if _green else '▼'} ${abs(_chg):,.0f} "
                  f"({_pct:+.1f}%) · {_rng}</span>")
            try:
                import altair as alt
                _df = _seg.reset_index()
                _df.columns = ["date", "value"]
                _base = alt.Chart(_df).encode(
                    x=alt.X("date:T", axis=alt.Axis(title=None, format="%b %d",
                                                    labelColor="#9aa", grid=False)),
                    y=alt.Y("value:Q", axis=alt.Axis(title=None, labelColor="#9aa",
                                                     grid=False),
                            scale=alt.Scale(zero=False)))
                _chart = (_base.mark_area(opacity=0.14, color=_col)
                          + _base.mark_line(color=_col, strokeWidth=2))
                st.altair_chart(_chart.properties(height=230), width='stretch')
            except Exception:
                st.line_chart(_seg, height=200)
            st.caption("Value of your **current** holdings over the range, scaled "
                       "so the latest point matches your live total above. The "
                       "**shape** is price history (it doesn't include past "
                       "buys/sells), and the endpoint is your real equity. On "
                       "weekends it holds Friday's close. Turn on 🔴 Live updates "
                       "to refresh every 5 minutes during market hours.")

    st.markdown("### 🧺 By investment type")
    classes = {}
    for r in rows:
        d = classes.setdefault(r["class"], {"value": 0.0, "realized": 0.0, "cash": 0.0})
        if r["value"] is not None:
            d["value"] += r["value"]
    for inst, p in pos.items():        # futures/options net cash from the CSV
        if p["class"] in ("Futures", "Options"):
            classes.setdefault(p["class"], {"value": 0.0, "realized": 0.0,
                                            "cash": 0.0})["cash"] += p.get("net_cash", 0.0)
    if not rz.empty:
        for cls, g in rz.groupby("class")["gain"].sum().items():
            classes.setdefault(cls, {"value": 0.0, "realized": 0.0,
                                     "cash": 0.0})["realized"] = float(g)
    tblc = []
    for cls, d in classes.items():
        rl = d["realized"] + (d["cash"] if cls in ("Futures", "Options") else 0.0)
        tblc.append({"Type": cls, "Value now": f"${d['value']:,.0f}",
                     "Realized (from CSV)": f"${rl:+,.0f}"})
    if tblc:
        st.table(pd.DataFrame(tblc).set_index("Type"))
        st.caption("Value now = your live holdings. Realized = closed-trade cash from "
                   "the CSV (options premium, futures, sold stock). Futures & options "
                   "show as net cash — your copper/gold futures land here.")

    # ---------------- 2026 PROFIT & LOSS STATEMENT ----------------
    if pnl is not None:
      try:
          GRN, RED = "#00C805", "#FF5000"
          _gt = pnl.grand_total()
          _pos = _gt >= 0
          _col = GRN if _pos else RED
          _arrow = "▲" if _pos else "▼"

          st.markdown("## 📊 Your 2026 so far")

          # ---- HERO: the one number, unmissable ----
          _bg = "#0F2E24" if _pos else "#2E1414"
          _html(
              f"""<div style="background:{_bg};border-radius:16px;padding:26px 28px;
                   margin:6px 0 18px 0;">
                <div style="color:#9aa;font-size:14px;letter-spacing:.04em;
                     text-transform:uppercase;">Net profit &amp; loss · Jan 1 – {pnl.AS_OF}</div>
                <div style="color:{_col};font-size:46px;font-weight:800;
                     line-height:1.1;margin-top:4px;">{_arrow} ${abs(_gt):,.0f}</div>
                <div style="color:#c9c9c4;font-size:15px;margin-top:8px;">
                  {"You're up on the year." if _pos else
                   "You're down on the year — driven by stock losses (VKTX, MSTR) "
                   "and gold futures, partly offset by copper futures and dividends."}
                </div>
              </div>""")

          # ---- WHAT DROVE IT: contribution bars, biggest swing first ----
          st.markdown("#### What drove it")
          _drivers = sorted(pnl.summary_lines(), key=lambda l: -abs(l.amount))
          _mx = max(abs(l.amount) for l in _drivers) or 1
          _bar_html = ['<div style="display:flex;flex-direction:column;gap:9px;'
                       'margin:4px 0 6px 0;">']
          for l in _drivers:
              up = l.amount >= 0
              col = GRN if up else RED
              frac = abs(l.amount) / _mx
              w = max(frac * 46, 1.5)          # up to 46% of row width each side
              approx = " ≈" if l.basis != "exact" else ""
              left = f'<div style="flex:0 0 46%;text-align:right;padding-right:8px;">' \
                     + (f'<span style="display:inline-block;height:18px;width:{w}%;'
                        f'background:{RED};border-radius:3px 0 0 3px;vertical-align:middle;">'
                        f'</span>' if not up else '') + '</div>'
              right = f'<div style="flex:0 0 46%;padding-left:8px;">' \
                      + (f'<span style="display:inline-block;height:18px;width:{w}%;'
                         f'background:{GRN};border-radius:0 3px 3px 0;vertical-align:middle;">'
                         f'</span>' if up else '') + '</div>'
              _bar_html.append(
                  f'<div style="display:flex;align-items:center;font-size:13.5px;">'
                  f'{left}{right}</div>'
                  f'<div style="display:flex;font-size:12.5px;color:#6b6a66;'
                  f'margin-top:-6px;">'
                  f'<div style="flex:0 0 46%;text-align:right;padding-right:8px;">'
                  f'{l.label}{approx}</div>'
                  f'<div style="flex:0 0 8%;text-align:center;color:{col};'
                  f'font-weight:700;">${l.amount:+,.0f}</div>'
                  f'<div style="flex:0 0 46%;"></div></div>')
          _bar_html.append('</div>')
          _html("".join(_bar_html))
          st.caption("Green = added to your year · red = took away. Longest bar = "
                     "biggest swing. “≈” means an approximate line (see note below).")

          # ---- the statement, as clean metrics not a monospace dump ----
          st.markdown("#### The lines")
          _lines = pnl.summary_lines()
          for i in range(0, len(_lines), 2):
              cols = st.columns(2)
              for col, l in zip(cols, _lines[i:i+2]):
                  tag = "✅" if l.basis == "exact" else "≈"
                  col.metric(f"{tag} {l.label}", f"${l.amount:+,.0f}")

          st.divider()

          # ---- STOCKS: winners vs losers, ranked ----
          _sales_ok = [s for s in pnl.STOCK_SALES if s.gain is not None]
          _win = sorted([s for s in _sales_ok if s.gain > 0], key=lambda s: -s.gain)
          _los = sorted([s for s in _sales_ok if s.gain < 0], key=lambda s: s.gain)
          st.markdown("#### 📉 The 17 stocks you sold")
          cW, cL = st.columns(2)
          cW.markdown((f"<div style='color:{GRN};font-weight:700;'>Winners "
                       f"(+${sum(s.gain for s in _win):,.0f})</div>").replace("$", "\\$"),
                      unsafe_allow_html=True)
          cW.dataframe(pd.DataFrame(
              [{"Ticker": s.ticker, "Gain": f"+${s.gain:,.0f}"} for s in _win]
              ).set_index("Ticker"), width='stretch', height=min(len(_win)*35+38, 460))
          cL.markdown((f"<div style='color:{RED};font-weight:700;'>Losers "
                       f"(−${abs(sum(s.gain for s in _los)):,.0f})</div>").replace("$", "\\$"),
                      unsafe_allow_html=True)
          cL.dataframe(pd.DataFrame(
              [{"Ticker": s.ticker, "Loss": f"−${abs(s.gain):,.0f}"} for s in _los]
              ).set_index("Ticker"), width='stretch', height=min(len(_los)*35+38, 460))
          st.caption(_md(f"Two positions did the damage: **VKTX −$50,802** and "
                     f"**MSTR −$57,293**, both long-term. Everything reconciles to "
                     f"your Robinhood proceeds. Net realized: "
                     f"**${pnl.STOCK_REALIZED_KNOWN.amount:+,.0f}** "
                     f"(short-term +${pnl.STOCK_ST:,.0f} · long-term ${pnl.STOCK_LT:+,.0f})."))
          with st.expander("See all 17 with cost basis & term"):
              _srows = [{
                  "Ticker": s.ticker, "Shares": f"{s.shares:g}",
                  "Proceeds": f"${s.proceeds:,.0f}",
                  "Cost": f"${s.cost:,.0f}" if s.cost is not None else "—",
                  "Gain/loss": f"${s.gain:+,.0f}" if s.gain is not None else "⏳",
                  "Term": ("ST" if s.st_gain and not s.lt_gain else
                           "LT" if s.lt_gain and not s.st_gain else
                           "ST+LT" if (s.st_gain or s.lt_gain) else "—")}
                  for s in pnl.STOCK_SALES]
              st.dataframe(pd.DataFrame(_srows).set_index("Ticker"),
                           width='stretch')

          st.divider()

          # ---- FUTURES: gold vs copper, the real story ----
          st.markdown("#### 🥇 Futures — gold vs copper")
          _gold = pnl.FUTURES_BY_CONTRACT["Gold (GC + MGC)"]
          _copper = pnl.FUTURES_BY_CONTRACT["Copper (HG + MHG)"]
          fc1, fc2, fc3 = st.columns(3)
          fc1.metric("🥇 Gold (GC+MGC)", f"${_gold:+,.0f}", "big loss", delta_color="off")
          fc2.metric("🟠 Copper (HG+MHG)", f"${_copper:+,.0f}", "big win", delta_color="off")
          fc3.metric("Net futures (Jan–Jul)", f"${pnl.FUTURES_TOTAL.amount:+,.0f}",
                     "copper > gold")
          _html(
              f"""<div style="background:#F6F5EF;border-left:7px solid {RED};
                   border-radius:8px;padding:12px 16px;margin:6px 0;">
                <b>Gold was wildly volatile.</b> You made <span style="color:{GRN};
                font-weight:700;">+$80,489 in January</span>, then gave most of it
                back: <span style="color:{RED};font-weight:700;">−$59,419 in March</span>
                and <span style="color:{RED};font-weight:700;">−$39,504 in May</span>.
                Gold losing months totalled about −$111k; January's win left the
                year at <b>−$30,333</b> net. Copper (+$43,928) is the only reason
                futures finished positive overall.
              </div>""")

          # gold monthly mini-chart
          _gold_by_month = {
              "Jan": 80489, "Feb": -10459, "Mar": -59419, "Apr": -1770,
              "May": -39504, "Jun": 274, "Jul": 56}
          try:
              import altair as alt
              _gdf = pd.DataFrame({"Month": list(_gold_by_month),
                                   "Gold P&L": list(_gold_by_month.values())})
              _gdf["c"] = _gdf["Gold P&L"].apply(lambda v: GRN if v >= 0 else RED)
              _ch = alt.Chart(_gdf).mark_bar().encode(
                  x=alt.X("Month:N", sort=list(_gold_by_month),
                          axis=alt.Axis(title=None, labelColor="#9aa")),
                  y=alt.Y("Gold P&L:Q", axis=alt.Axis(title=None, labelColor="#9aa")),
                  color=alt.Color("c:N", scale=None))
              st.altair_chart(_ch.properties(height=190), width='stretch')
              st.caption("Gold futures realized P&L by month — the January spike, "
                         "then the March and May drawdowns.")
          except Exception:
              pass

          with st.expander("Futures — full month-by-month (each ties to the RHD statement)"):
              _frows = [{"Month": m, "Realized P&L": f"${l.amount:+,.0f}"}
                        for m, l in pnl.FUTURES_MONTHLY.items()]
              st.dataframe(pd.DataFrame(_frows).set_index("Month"),
                           width='stretch')
              st.caption("Each month = Gross P&L + commissions/fees from the RHD "
                         "statement, reconciled to the cash-balance identity and the "
                         "Purchase-and-Sale rows. August pending (~Sep 1).")

          st.info("**Reading the tags:** ✅ exact = reconciled to your Robinhood "
                  "statements. ≈ approx = options (cash-basis; assigned options flow "
                  "into stock cost basis, so no double-count) and crypto "
                  "(mark-to-market — the statements carry no coin cost basis). "
                  "Estimates for planning, not tax filing.")
          st.divider()
      except Exception as _e:
          st.warning('The 2026 P&L panel hit an error and was skipped — the rest of the page is fine. ' + str(_e)[:160])

    st.markdown("### 📋 Holdings")
    _priced = [x for x in rows if x["value"]]
    # 12h-cached dividend + earnings facts for the stock/ETF tickers only.
    _info_syms = tuple(sorted({r["inst"] for r in _priced
                               if r["class"] == "Stocks & ETFs"}))
    _info = _cached_info_bundle(_info_syms) if _info_syms else {}

    tblp = []
    for r in sorted(_priced, key=lambda x: -x["value"]):
        wgt = r["value"] / total_val * 100 if total_val else 0
        info = _info.get(r["inst"], {})
        dr = info.get("div_rate")
        _dr_src = "live"
        if not dr and pnl is not None and hasattr(pnl, "div_per_share"):
            dr = pnl.div_per_share(r["inst"])      # verified fallback table
            _dr_src = "est"
        div_yr = (f"${dr*r['shares']:,.0f}/yr" + (" *" if _dr_src == "est" else "")) if dr else "—"
        exd = info.get("ex_date")
        if not exd and pnl is not None and hasattr(pnl, "next_exdiv_date"):
            exd = pnl.next_exdiv_date(r["inst"])
        exd = exd or "—"
        earn = info.get("earn_date")
        if not earn and pnl is not None and hasattr(pnl, "next_earnings_date"):
            earn = pnl.next_earnings_date(r["inst"])
        earn = earn or "—"
        _is_stale = r.get("stale")
        ev = pnl.event_for(r["inst"]) if (pnl is not None and hasattr(pnl,"event_for")) else ""
        tblp.append({
            "Ticker": r["inst"],
            "Shares": f"{r['shares']:g}",
            "Avg cost": f"${r['avgc']:,.2f}" if r["avgc"] else "—",
            "Price": (f"${r['price']:,.2f} ⚠️" if _is_stale and r["price"]
                      else f"${r['price']:,.2f}" if r["price"] else "—"),
            "Value": f"${r['value']:,.0f}" if r["value"] is not None else "—",
            "P/L $": (f"${r['tot']:+,.0f}" if r["tot"] is not None else "—"),
            "P/L %": (f"{r['totpct']:+.1f}%" if (r["avgc"] and r["totpct"] is not None) else "—"),
            "Today": ("—" if _is_stale else f"${r['today']:+,.0f}"),
            "Wt": f"{wgt:.0f}%",
            "Signal": {"Buy": "🟢 Buy", "Sell": "🔴 Sell"}.get(r.get("signal"), "⚪ Hold"),
            "Div/yr": div_yr,
            "Ex-div": exd,
            "Earnings": earn,
            "Coming up": ev or "—"})
    if tblp:
        # ---- Total equity headline (updates with the cached price refresh) ----
        _eq1, _eq2, _eq3 = st.columns([2, 1, 1])
        _eq_lbl = ("💰 Total equity (holdings)" if stale_val
                   else "💰 Total equity (holdings, live)")
        _eq1.metric(_eq_lbl, f"${total_val:,.0f}",
                    (f"⚠️ ${stale_val:,.0f} at cost" if stale_val
                     else f"${win['today']:+,.0f} today"), delta_color="off")
        _eq2.metric("Unrealized P/L", f"${(total_val-total_cost):+,.0f}",
                    f"{((total_val/total_cost-1)*100 if total_cost else 0):+.1f}%")
        _eq3.metric("Positions", f"{len(tblp)}"
                    + (f" ({len(missing)} stale)" if missing else ""))
        st.dataframe(
            pd.DataFrame(tblp).set_index("Ticker"),
            width='stretch',
            column_config={
                "Signal": st.column_config.TextColumn(width="small"),
                "Coming up": st.column_config.TextColumn(width="large"),
            })
        st.caption("**Live** columns (Price, Value, P/L, Today, Signal) refresh "
                   "with the 5-min price cache. **Div/yr · Ex-div · Earnings** come "
                   "from a separate feed cached **12 h** — that's deliberate, so "
                   "the frequent refresh never trips Yahoo's rate limit. A **\\*** "
                   "on Div/yr means the live feed was unavailable and the value "
                   "comes from a **verified table built from your own 2026 "
                   "payments**. **Signal** = the app's 50/200-day + 3-month trend "
                   "read (🟢 clean uptrend · 🔴 clean downtrend · ⚪ hold); a "
                   "weeks-to-months momentum call, not trade advice. **Coming up** "
                   "is an editable catalog in the app files.")
        _big = [t["Ticker"] for t in tblp if float(t["Wt"].rstrip("%")) >= 30]
        if _big:
            st.warning("⚠️ **Concentration:** " + ", ".join(_big) + " is over 30% of "
                       "your portfolio — one bad day there moves everything. "
                       "Spreading out is the cheapest protection there is.")

    st.markdown("### 💵 Dividends")
    if P is not None:
        d1, d2, d3 = st.columns(3)
        d1.metric("Last 30 days", f"${dv_sums['month']:,.2f}")
        d2.metric("This year", f"${dv_sums['ytd']:,.2f}")
        d3.metric("All time (in file)", f"${dv_sums['all']:,.2f}")
    else:
        st.caption("Add your Robinhood CSV above to see dividends actually received. "
                   "Below is the forward estimate from your current holdings.")
    proj = 0.0
    _proj_est = False
    for inst, (shares, _avgc) in holdings.items():
        if sym_cls.get(inst) != "Stocks & ETFs":
            continue
        rate = (_info.get(inst) or {}).get("div_rate")   # from 12h cache, no new call
        if not rate and pnl is not None and hasattr(pnl, "div_per_share"):
            rate = pnl.div_per_share(inst)               # verified fallback
            if rate:
                _proj_est = True
        if rate:
            proj += float(rate) * shares
    if proj > 0:
        st.markdown(_md(f"**Projected income at current rates:** ~**${proj:,.0f}/year** "
                    f"(≈ ${proj/12:,.0f}/month · ${proj/52:,.0f}/week)."))
    st.caption("Honest note: dividends don't arrive daily — each fund pays weekly, "
               "monthly, or quarterly on its own schedule, so a true 'per-day' "
               "amount would be made up. The weekly/monthly figures are the fair "
               "way to see it. Rates use the live feed where available; values "
               "marked with * (and any fallback here) come from a **verified table "
               "built from your own 2026 payments** so the column isn't blank when "
               "the live feed is throttled.")

    st.markdown("### 🧾 Illinois tax ESTIMATE — not tax advice")
    t1, t2, t3, t4 = st.columns(4)
    fed = t1.selectbox("Your federal bracket %", [10, 12, 22, 24, 32, 35, 37], index=3) / 100
    ltr = t2.selectbox("Long-term rate %", [0, 15, 20], index=1) / 100
    niit = t3.checkbox("Add NIIT 3.8%", value=False,
                       help="Applies if income > ~$200k single / $250k joint")
    ilr = t4.number_input("IL rate %", value=4.95, step=0.05) / 100
    qual = st.checkbox("Treat dividends as qualified (long-term rate)", value=True)
    _st = float(rz.loc[rz.term == "ST", "gain"].sum()) if not rz.empty else 0.0
    _lt = float(rz.loc[rz.term == "LT", "gain"].sum()) if not rz.empty else 0.0
    _fut = sum(p.get("net_cash", 0.0) for p in pos.values()
               if p["class"] in ("Futures", "Options"))
    st.caption(_md(f"From your file: short-term realized **${_st:+,.0f}** · long-term "
               f"realized **${_lt:+,.0f}** · futures/options net **${_fut:+,.0f}** · "
               f"dividends YTD **${dv_sums['ytd']:,.0f}**. (Futures have special "
               f"60/40 tax treatment — ask a CPA; not estimated below.)"))
    tax = pfm.tax_estimate(_st, _lt, dv_sums["ytd"], fed, ltr, niit, ilr, qual)
    e1, e2 = st.columns(2)
    with e1:
        st.markdown(_md("**Estimated tax on YTD realized gains + dividends:**  \n"
                    f"Federal short-term: **${tax['fed_st']:,.0f}**  \n"
                    f"Federal long-term: **${tax['fed_lt']:,.0f}**  \n"
                    f"Federal on dividends: **${tax['fed_div']:,.0f}**  \n"
                    + (f"NIIT: **${tax['fed_niit']:,.0f}**  \n" if niit else "")
                    + f"Illinois (flat): **${tax['il']:,.0f}**  \n"
                    f"**Total ≈ ${tax['total']:,.0f}**"))
    with e2:
        days = max((now - _dt.datetime(now.year, 1, 1)).days, 1)
        st.markdown("**Year-end projection (same pace):**  \n"
                    f"≈ **${tax['total']*365/days:,.0f}** for the full year.")
        st.caption("Straight-line projection — real life won't be straight-line.")
    st.error("**Estimates only.** Your real bill depends on your salary, filing "
             "status, deductions, wash sales, and futures' special rules — none of "
             "which are in a brokerage CSV. Use this for planning; use a CPA for "
             "filing. This is not tax or investment advice.")
    with st.expander("💡 Ways people legally reduce investment taxes (educational)"):
        st.markdown(
            "- **Hold winners 1+ year** — federal drops from your bracket (up to 37%) "
            "to 0/15/20%. Illinois is 4.95% either way, so the lever is federal.\n"
            "- **Tax-loss harvesting** — selling losers offsets gains (plus up to "
            "$3,000 vs ordinary income). Beware the **wash-sale rule**: don't rebuy "
            "the same thing within 30 days.\n"
            "- **Tax-advantaged accounts** — 401(k) / IRA / Roth / HSA shield gains "
            "entirely.\n"
            "- **Time big sales** for lower-income years when you can.\n"
            "- **Big gain? Make estimated payments** — large sales aren't withheld, "
            "and the IRS + Illinois can charge penalties if you wait for April.\n\n"
            "*Educational, not advice — a CPA can tell you which apply to you.*")
    st.stop()

asset_key = stock_ticker = None
if mode == "Metals":
    asset_key = st.selectbox(
        "Choose metal", list(METAL_META.keys()),
        format_func=lambda k: f"{METAL_META[k][1]}  {METAL_META[k][0]}")
    profile = assets.get(asset_key)
    name, icon = METAL_META[asset_key]
else:
    # searchable dropdown — click it and type letters to filter, like Google
    _opts = sorted(mp.TICKER_NAME.keys(), key=lambda t: mp.TICKER_NAME[t].lower())
    _labelf = lambda t: f"{mp.TICKER_NAME[t]}  ·  {t}"
    _default = _opts.index("AAPL") if "AAPL" in _opts else 0
    picked = st.selectbox("Search a company or ticker (type letters to filter)",
                          _opts, index=_default, format_func=_labelf,
                          key="stock_pick")
    stock_ticker = picked
    with st.expander("Not in the list? Type any ticker or name here"):
        typed = st.text_input("e.g. an uncommon symbol or full company name",
                              key="stock_typed").strip()
        if typed:
            _tk, _sug = mp.resolve_symbol(typed)
            if _tk:
                stock_ticker = _tk
                st.success(f"Using **{_tk}** — {mp.ticker_name(_tk)}")
            elif _sug:
                st.warning("Closest matches — tap one:")
                _cc = st.columns(min(4, len(_sug)))
                for _i, (_stk, _snm) in enumerate(_sug):
                    if _cc[_i % len(_cc)].button(f"{_stk} · {_snm}", key=f"tsg_{_stk}",
                                                 width='stretch'):
                        st.session_state["stock_typed"] = _stk
                        st.rerun()
                st.caption(f"(Or continuing with **{picked}** from the dropdown.)")
            else:
                st.info(f"No match for '{typed}'. Using the dropdown pick "
                        f"(**{picked}**).")
    profile = assets.stock_profile(stock_ticker)
    name, icon = stock_ticker, "📈"

unit = profile["unit"]
load_key = f"{mode}:{asset_key or stock_ticker}"

st.title(f"{icon} {name} forecast")
st.caption(f"{profile['blurb']}  ·  forecasts + auto-picks the best-performing "
           f"strategy  ·  grades itself and adapts every run  ·  "
           f"**ranges + odds, not promises and not financial advice.**")
if storage.configured():
    st.caption("☁️ **Cloud memory: ON** — the learning is saved to your private "
               "store, so it persists across restarts and is the same on your "
               "laptop and your phone.")
else:
    st.caption("💾 Cloud memory: off (this device/session only). To make the "
               "learning permanent and reachable from any phone, follow "
               "**DEPLOY.md**.")

# ----------------------------------------------------------------- controls --
dp = float(profile["default_price"])
if profile["kind"] == "stock":
    step, fmt, mn, mx, pdef = 0.01, "%.2f", 0.0, 100000.0, 0.0
elif dp >= 1000:
    step, fmt, mn, mx, pdef = 1.0, "%.1f", 100.0, dp * 4, dp
elif dp >= 100:
    step, fmt, mn, mx, pdef = 0.5, "%.2f", 10.0, dp * 4, dp
elif dp >= 10:
    step, fmt, mn, mx, pdef = 0.01, "%.2f", 1.0, dp * 5, dp
else:
    step, fmt, mn, mx, pdef = 0.0001, "%.4f", 0.1, dp * 6, dp

c1, c2, c3 = st.columns([1.3, 1.1, 1])
with c1:
    price_mode = st.radio("Price to anchor on",
                          ["Auto-fetch live", "Use my price"], index=0)
with c2:
    plabel = (f"My price ({unit}, 0 = auto)" if profile["kind"] == "stock"
              else f"My price ({unit})")
    my_price = st.number_input(plabel, value=pdef, min_value=mn, max_value=mx,
                               step=step, format=fmt, key=f"price_{load_key}")
with c3:
    scen_map = profile["scenarios"]
    scen_keys = ["(none)"] + list(scen_map.keys())
    scenario = st.selectbox(
        "What-if scenario", scen_keys,
        format_func=lambda k: "(none)" if k == "(none)" else scen_map[k]["label"])
use_lgbm = st.checkbox("Use LightGBM (M5-winning method, if installed)", value=True)

run = st.button("🔄  Update prediction now", type="primary", width='stretch')

_res_ts = st.session_state.get("result_ts", 0)
_stale = live and (time.time() - _res_ts) > REFRESH_MIN * 60
need = (run or "result" not in st.session_state
        or st.session_state.get("load_key") != load_key or _stale)
if need:
    with st.spinner(f"Fetching {name} price + {profile['ref_label']} + news, "
                    f"grading past predictions, running the ensemble and "
                    f"strategy comparison…"):
        cfg = cf.load_config(str(cf.HERE / "config.yaml"))
        ovr = my_price if (price_mode == "Use my price" and my_price > 0) else None
        try:
            st.session_state["result"] = mp.run_prediction(
                cfg, asset_key=asset_key, stock_ticker=stock_ticker,
                spot_override=ovr,
                scenario_key=None if scenario == "(none)" else scenario,
                use_lgbm=use_lgbm)
            st.session_state["load_key"] = load_key
            st.session_state["result_ts"] = time.time()
            st.session_state["error"] = None
        except Exception as exc:
            st.session_state["error"] = str(exc)

if st.session_state.get("error"):
    st.error("Couldn't fetch live data — " + st.session_state["error"] +
             "\n\nCheck the ticker and your connection, or switch to **Use my "
             "price**, then click Update again. (Some tickers have thin data.)")
    st.stop()

res = st.session_state["result"]
fcs = res["forecasts"]
news = res["news"]
sig = res["headline_signal"]
calib = res["calibration"]
spot = res["spot"]
ind = res.get("indicators", {"list": [], "bull": 0, "bear": 0, "neutral": 0})
ind_total = len(ind["list"])
by_name = {hf.name: hf for hf, _ in fcs}

# ===================== 🟢 SIMPLE VIEW (default, plain language) ==============
simple = st.toggle("🟢 Simple view (plain words)", value=True,
                   help="Turn this OFF to see all the charts, indicators and details.")
if simple:
    unit_tail = ("per " + unit.split("/")[-1]) if "/" in unit else ""
    # today's move
    prev = res.get("prev_close")
    move_txt = ""
    try:
        if prev:
            chg = (spot / float(prev) - 1) * 100
            move_txt = f"  ({chg:+.1f}% vs yesterday)"
    except Exception:
        pass
    st.header(f"{icon} {name}")
    st.subheader(f"Right now: ${fmtp(spot)} {unit_tail}{move_txt}")

    tf_map = {"Next few hours": "4 hours", "Tomorrow": "Next day",
              "Next week": "Next week"}
    tf = st.radio("Look ahead:", list(tf_map.keys()), index=1, horizontal=True)
    plan = res.get("plans", {}).get(tf_map[tf])

    # ---- 1) the call, in one clear line ----
    if not plan or plan.get("direction") == "WAIT":
        st.markdown("## 🤷 NOT SURE which way this goes")
        st.markdown("The signals don't agree enough to lean either way. When it's "
                    "unclear like this, the smart move is usually to **wait**.")
    else:
        up = plan["direction"] == "LONG"
        prob = plan["prob"] * 100
        arrow = "📈 **UP**" if up else "📉 **DOWN**"
        strength = ("but only *barely* — almost a coin toss" if prob < 55
                    else "a *slight* lean" if prob < 60 else "a *stronger* lean")
        st.markdown(f"## Leaning {arrow}")
        st.progress(min(max((prob - 40) / 40, 0.02), 1.0),
                    text=f"about {prob:.0f}% odds it goes {'up' if up else 'down'} "
                         f"({strength})")

    # ---- 2) WHY — the signals in plain words ----
    st.markdown("#### 🔎 Why")
    plain = {
        "trend_cross": {1: "The recent trend is **up**", -1: "The recent trend is **down**",
                        0: "The trend is flat"},
        "vs200": {1: "Price is **above** its long-term average (healthy)",
                  -1: "Price is **below** its long-term average (weak)",
                  0: "Price is near its long-term average"},
        "rsi": {1: "It's been beaten down lately — could **bounce**",
                -1: "It's run up a lot lately — could **pull back**",
                0: "Not overbought or oversold"},
        "macd": {1: "Momentum is turning **up**", -1: "Momentum is turning **down**",
                 0: "Momentum is flat"},
        "boll": {1: "Price is near the **low** end of its range",
                 -1: "Price is near the **high** end of its range",
                 0: "Price is mid-range"},
    }
    for it in ind["list"]:
        s = it["signal"]
        dot = "🟢" if s > 0 else "🔴" if s < 0 else "⚪"
        txt = plain.get(it["key"], {}).get(s, it.get("text", ""))
        if txt:
            st.markdown(f"{dot} {txt}")
    ups = sum(1 for it in ind["list"] if it["signal"] > 0)
    downs = sum(1 for it in ind["list"] if it["signal"] < 0)
    st.caption(f"Scoreboard of signals: **{ups} point up**, **{downs} point down**, "
               f"{ind_total - ups - downs} neutral.")

    # ---- 📅 next earnings + 📰 latest news ----
    if res.get("earnings_date"):
        st.markdown(f"#### 📅 Next earnings: **{res['earnings_date']}**")
        _edays = None
        try:
            _edays = ( _dt.datetime.strptime(res["earnings_date"], "%b %d, %Y")
                       - _dt.datetime.utcnow()).days
        except Exception:
            pass
        if _edays is not None and 0 <= _edays <= 7:
            st.warning(f"⚠️ **Earnings in ~{_edays} day(s).** Prices often jump or "
                       f"drop hard on the report — a surprise can blow straight "
                       f"through a stop. Many traders size down or wait until after.")
        else:
            st.caption("Prices often swing hard right after earnings — many traders "
                       "avoid holding a surprise over that date, or size down into it.")

    # ---- 🔬 more signals: analysts, options positioning, social buzz ----
    _an, _op, _so = res.get("analyst"), res.get("options_pos"), res.get("social")
    if _an or _op or _so:
        st.markdown("#### 🔬 More signals (context — not crystal balls)")
        if _an:
            _t = _an.get("target")
            _ups = (f" ({(_t/spot-1)*100:+.0f}% vs now)" if (_t and spot) else "")
            _nn = f" · ~{_an['n']} analysts" if _an.get("n") else ""
            st.markdown(f"🧑‍💼 **Wall St analysts:** {_an['rating']}"
                        f"{' · avg target $' + fmtp(_t) + _ups if _t else ''}{_nn}")
        if _op:
            _pc = _op["pc"]
            _read = ("more bets on **down** (put-heavy)" if _pc > 1.3 else
                     "more bets on **up** (call-heavy)" if _pc < 0.7 else
                     "roughly balanced")
            st.markdown(f"🎯 **Options positioning:** put/call ratio "
                        f"**{_pc:.2f}** — {_read} (nearest expiry {_op['expiry']})")
        if _so:
            st.markdown(f"💬 **Social buzz (StockTwits):** {_so['msgs']} recent posts "
                        f"— {_so['bull']} bullish / {_so['bear']} bearish")
        st.caption("These come from free feeds and are often missing or noisy. "
                   "Analysts lag price, options positioning is a rough read (real "
                   "'flow' data is paid), and crowd buzz is famously unreliable — "
                   "use them as context, never as the reason to bet.")

    _news = res.get("news_items") or []
    if _news:
        st.markdown("#### 📰 Latest news")
        for it in _news[:5]:
            title = it.get("title", "")
            link = it.get("link", "")
            if link:
                st.markdown(f"- [{title}]({link})")
            else:
                st.markdown(f"- {title}")
        st.caption("Headlines move stocks in seconds — by the time news reaches "
                   "here it's usually already in the price. Use this to understand "
                   "*why* it's moving, not to beat the move.")

    # ---- 3) the simple plan ----
    if plan and plan.get("direction") != "WAIT":
        st.markdown("#### 📋 If you want to try a trade")
        st.markdown(_md(
            f"- 🟢 **Buy** near **${fmtp(plan['entry'])}**\n"
            f"- 🎯 **Take profit** at **${fmtp(plan['target'])}**\n"
            f"- 🛑 **Get out** at **${fmtp(plan['stop'])}** if it goes the wrong way "
            f"— this caps your loss\n"
            f"- ⏰ If nothing happens by **{plan['grades_on'].strftime('%b %d')}**, "
            f"close it and look again"))
        if not plan.get("ev_positive"):
            st.warning("⚠️ The possible reward isn't really worth the risk here. "
                       "Many people would just **skip this one**.")
        bs = res.get("best_strategy")
        if bs and bs.get("label"):
            friendly = {"Trend (momentum, weekly)": "following the trend",
                        "Mean-reversion (weekly)": "buying dips / selling spikes",
                        "Model ensemble (weekly)": "a blend of signals",
                        "MA crossover (50/200)": "moving-average crossover",
                        "MACD momentum (weekly)": "momentum (MACD)",
                        "RSI mean-reversion (weekly)": "bounce-back (RSI)"}
            nm = friendly.get(bs["label"], bs["label"])
            note = ("which actually made money in past tests" if bs.get("recommended")
                    else "though it hasn't been a clear winner in past tests")
            st.caption(f"The plan leans on **{nm}** — {note}.")

    # ---- 4) reward / trust score ----
    right = calib.get("dir_ok", 0)
    total = calib.get("dir_tot", 0)
    st.markdown("#### 🧠 How much should you trust it?")
    if total == 0:
        st.info("It hasn't checked any of its past guesses yet. As the days pass it "
                "grades itself and keeps score here — so it earns your trust (or "
                "doesn't) over time.")
    else:
        score = 2 * right - total
        pct = right / total * 100
        emoji = "🟢" if pct >= 55 else "🟡" if pct >= 45 else "🔴"
        trust = ("Right more often than wrong lately — but still keep bets small."
                 if pct >= 55 else
                 "About a coin toss lately — take its guesses with a grain of salt."
                 if pct >= 45 else
                 "Wrong more than right lately — be extra careful.")
        st.markdown(f"{emoji} Lately it was right **{right} out of {total}** times.  "
                    f"**Reward score: {score:+d}** (+1 per right call, −1 per wrong).")
        st.caption(trust)
        with st.expander("🔧 The engine was just improved — reset this score?"):
            st.markdown("The old score came from an earlier version that leaned too "
                        "hard on 'it'll bounce back' and got beaten by trends. The "
                        "engine now follows trends instead of fighting them. You can "
                        "wipe the old track record so the **new** engine is measured "
                        "fresh from today.")
            if st.button("♻️ Reset the score for " + name, key=f"reset_{load_key}"):
                mp.reset_learning(res["state_key"])
                st.session_state.pop("result", None)
                st.success("Score reset. It starts keeping fresh score from the next "
                           "predictions. Tap 'Update prediction now' above.")
                st.stop()

    st.info("👉 Want the **strongest buys and sells across many stocks**? Tap "
            "**Top & Bottom (scan)** at the top — it ranks everything so you can see "
            "what's leaning up or down the most right now.")
    st.caption("No app can know the future — this is **odds, not promises.** Only use "
               "money you can afford to lose. Want every chart and number? Turn off "
               "**Simple view** at the top.")
    st.stop()

# -------------------------------------------------------------- signal card --
lean, conv = sig["lean"], sig.get("conviction", "LOW")
if conv == "STRONG" and lean == "BUY":
    fg, bg, label = "#0F6E56", "#E1F5EE", "🟢 STRONG BUY signal"
elif conv == "STRONG" and lean == "SELL":
    fg, bg, label = "#A32D2D", "#FCEBEB", "🔴 STRONG SELL signal"
elif lean == "BUY":
    fg, bg, label = "#0F6E56", "#E1F5EE", "▲ Leaning BUY"
elif lean == "SELL":
    fg, bg, label = "#993C1D", "#FAECE7", "▼ Leaning SELL"
else:
    fg, bg, label = "#5F5E5A", "#F1EFE8", "→ No clear edge — HOLD"

rel = sig["reliability"]
rel_txt = (f"right ~{rel*100:.0f}% of the time at this horizon in backtests"
           if rel is not None else "of unmeasured reliability here")
agree_n = max(sig.get("agree_up", 0), sig.get("agree_down", 0))
target = spot * (1 + sig["move_pct"] / 100)
st.markdown(
    f"""<div style="background:{bg};border-left:7px solid {fg};border-radius:8px;
padding:15px 20px;margin:6px 0 2px;">
<div style="font-size:25px;font-weight:600;color:{fg};">{label}
&nbsp;·&nbsp;{conv.lower()} conviction</div>
<div style="font-size:15px;color:#3d3d3a;margin-top:6px;line-height:1.55;">
Next week the model centers on <b>{sig['move_pct']:+.1f}%</b>
(≈ <b>${fmtp(target)}</b>) from <b>${fmtp(spot)}</b>, with about a
<b>{sig['p_up']*100:.0f}% chance {name} is higher</b> in a week.
{agree_n} of {sig.get('n_near',4)} short-term horizons and
{sig.get('ind_agree',0)} of {ind_total} indicators agree on direction.
This kind of call has been {rel_txt}.</div>
<div style="font-size:12px;color:#5f5e5a;margin-top:8px;">
The model's directional lean, <b>not financial advice</b> — you decide.</div>
</div>""", unsafe_allow_html=True)

# ----------------------------------------------- 📊 simple indicator readout --
if ind["list"]:
    st.subheader("📊 What the indicators say")
    for s in ind["list"]:
        chip = "🟢" if s["signal"] > 0 else "🔴" if s["signal"] < 0 else "⚪"
        track = ""
        g = s.get("graded") or 0
        if g:
            hr = (s.get("hit_recent") if (s.get("recent_n") or 0) >= 5
                  else s.get("hit_all"))
            if hr is not None:
                track = f"  ·  _track record: {hr*100:.0f}% right ({g} graded)_"
        st.markdown(f"{chip}  **{s['name']}** — {s['text']}{track}")
    st.caption(f"{ind['bull']} bullish · {ind['bear']} bearish · "
               f"{ind['neutral']} neutral. A **STRONG** signal only fires when "
               f"the forecast and several of these agree — proven setups are "
               f"about agreement, not any single indicator.")

    sc = ind.get("scorecard", {})
    rated = {k: v for k, v in sc.items() if v.get("graded")}
    if rated:
        st.markdown("**📈 Indicator hit rates on this asset** — how often each "
                    "has been right (graded a week after each call)")
        srows = []
        for v in rated.values():
            srows.append({
                "Indicator": v["name"],
                "All-time": (f"{v['hit_all']*100:.0f}% ({v['graded']})"
                             if v["hit_all"] is not None else "—"),
                "Recent": (f"{v['hit_recent']*100:.0f}% ({v['recent_n']})"
                           if v["hit_recent"] is not None else "—")})
        st.table(pd.DataFrame(srows).set_index("Indicator"))
        if ind.get("graded_now"):
            st.success(f"Just graded {ind['graded_now']} indicator call(s) that "
                       f"came due and updated these rates.")
        st.caption("Small samples are noisy — treat anything under ~20 graded "
                   "as a hint, not proof. Hovering near 50% is normal and honest; "
                   "an indicator that drifts above it *on this asset* is the one "
                   "worth leaning on.")
    else:
        st.caption("📈 Per-indicator hit rates will appear here as past calls "
                   "come due (about a week each) — they build up the more you "
                   "run it, per asset.")

# ===================== 🧠 SELF-IMPROVEMENT SCOREBOARD (the point) ============
st.subheader("🧠 Learning scoreboard — how it's doing and how it's improving")
dacc = (calib["dir_ok"] / calib["dir_tot"] * 100) if calib.get("dir_tot") else None
cov = (calib["cum_in68"] / calib["n_eval"] * 100) if calib.get("n_eval") else None
s1, s2, s3, s4, s5 = st.columns(5)
s1.metric("Predictions logged", res["n_made"], f"{res['n_pending']} pending")
s2.metric("Graded so far", calib.get("n_eval", 0))
s3.metric("Won (right direction)", f"{dacc:.0f}%" if dacc is not None else "—",
          help="Share of graded calls where the predicted up/down was correct.")
s4.metric("Landed in 68% range", f"{cov:.0f}%" if cov is not None else "—",
          help="How often the actual price fell inside the model's 68% band.")
s5.metric("Adjustments made", calib.get("n_adjust", 0),
          help="Times it changed its own model after grading misses.")

hist = [h for h in calib.get("acc_history", []) if h.get("dir_acc") is not None]
if len(hist) >= 2:
    first, last = hist[0]["dir_acc"], hist[-1]["dir_acc"]
    d = last - first
    trend = ("↗ improving" if d > 0.02 else "↘ slightly lower" if d < -0.02
             else "→ about steady")
    st.caption(f"Direction accuracy trend since it started grading: **{trend}** "
               f"({first*100:.0f}% → {last*100:.0f}%).")
    ch = pd.DataFrame({"Direction accuracy %": [h["dir_acc"] * 100 for h in hist]},
                      index=[h["n"] for h in hist])
    ch.index.name = "predictions graded"
    st.line_chart(ch, height=180)

if res["graded_now"]:
    st.success(f"Just graded {len(res['graded_now'])} prediction(s) that came due "
               f"and updated the model.")
if res["learned_note"]:
    st.info("🧠 Latest lesson: " + res["learned_note"])

# Always show what was just logged and exactly when each will be graded, so the
# panel is useful immediately instead of an empty "come back later."
pend_rows = []
for hf, _ in fcs:
    grades_on = hf.times[-1]
    pend_rows.append({"Horizon": hf.name,
                      "Predicted (central)": f"${fmtp(float(hf.median[-1]))}",
                      "Will be graded on (UTC)": grades_on.strftime("%b %d, %H:%M")})
st.markdown("**📝 Forecasts logged this run — each is graded automatically when "
            "its date arrives:**")
st.table(pd.DataFrame(pend_rows).set_index("Horizon"))

if not calib.get("n_eval"):
    st.warning(
        "**Nothing graded yet — here's why, and how to fix it.** The scoreboard "
        "fills in only *after* a forecast's date above arrives **and you reopen "
        "the app** so it can check the result (the soonest is the 4-hour call; the "
        "weekly/monthly ones take longer).\n\n"
        "Most likely cause if it never fills: **the memory lives in the `state` "
        "folder, and unzipping a fresh copy each time resets it to empty.** Two "
        "fixes:\n"
        "- **Best:** deploy it once (see `DEPLOY.md`) so the memory is saved in "
        "the cloud — it then persists forever, fills across visits, and you can "
        "check it from your phone.\n"
        "- **Local:** keep using the *same* folder, don't overwrite its `state` "
        "folder, and reopen it a few hours/days later.")
st.caption("Honest note: *improving* here means better-**calibrated** and "
           "self-correcting — short-horizon direction stays close to a coin "
           "flip for any model. A rising line is good discipline, not a "
           "guarantee of profit.")

# ------------------------------------------------- 📋 simple trade plan ------
st.subheader("📋 Simple plan — where to enter, target, and exit")
_pmap = {"Next few hours": "4 hours", "Next day": "Next day", "Next week": "Next week"}
pchoice = st.radio("Timeframe", list(_pmap.keys()), index=1, horizontal=True)
plan = res.get("plans", {}).get(_pmap[pchoice])

if sig.get("overext_brake"):
    st.warning("⚠️ **Overextension brake on:** price looks stretched "
               "(overbought/oversold) after a run, so the model cut its "
               "conviction. Chasing here is exactly where it gets whipsawed — "
               "like copper just did.")

if not plan or plan.get("direction") == "WAIT":
    st.info(f"**No clean setup for the {pchoice.lower()}.** The model doesn't see "
            f"a directional edge right now — the disciplined move is to wait for "
            f"the next update, not force a trade.")
else:
    d = plan["direction"]
    color = "#0F6E56" if d == "LONG" else "#A32D2D"
    arrow = "▲" if d == "LONG" else "▼"
    dipword = "dip" if d == "LONG" else "bounce"
    st.markdown(
        f"""<div style="background:#F6F5EF;border-left:7px solid {color};
border-radius:8px;padding:14px 18px;">
<div style="font-size:18px;font-weight:600;color:{color};">{arrow} If you trade it: {d}
&nbsp;·&nbsp;{plan['prob']*100:.0f}% odds&nbsp;·&nbsp;{sig['conviction'].lower()} conviction</div>
<table style="font-size:15px;color:#3d3d3a;margin-top:8px;line-height:1.75;">
<tr><td style="padding-right:16px;">📍 <b>Enter</b></td><td>near <b>${fmtp(plan['entry'])}</b> (ideally on a {dipword} toward ${fmtp(plan['dip'])})</td></tr>
<tr><td>🎯 <b>Target</b></td><td><b>${fmtp(plan['target'])}</b> — take profit</td></tr>
<tr><td>🛑 <b>Stop</b></td><td><b>${fmtp(plan['stop'])}</b> — exit here if wrong (caps the loss)</td></tr>
<tr><td>⏰ <b>Time stop</b></td><td>if neither is hit by <b>{plan['grades_on'].strftime('%b %d, %H:%M')} UTC</b>, close & re-assess</td></tr>
<tr><td>⚖️ <b>Reward:risk</b></td><td>{('%.1f : 1' % plan['rr']) if plan['rr'] else '—'}</td></tr>
</table></div>""", unsafe_allow_html=True)
    if plan["ev_positive"]:
        st.caption("Edge check: the odds × reward slightly **beat** the risk here "
                   "— a thin positive edge, still not a promise.")
    else:
        st.warning("**Edge check: roughly break-even or negative** once the ~50/50 "
                   "odds are weighed against the risk. Many disciplined traders "
                   "would skip this one.")
    st.caption("When to get back in: after a stop-out or once the timeframe "
               "passes, run a fresh update — only re-enter if it still leans your "
               "way **and** price is back near the entry.")

    # --- position sizer: how many shares/contracts fit your risk budget? ---
    st.markdown("**📐 Position size — size the trade to your risk (optional)**")
    pa, pb = st.columns(2)
    with pa:
        acct = st.number_input("Your account size ($)", min_value=0.0,
                               value=10000.0, step=500.0, key=f"acct_{load_key}")
    with pb:
        riskpct = st.number_input("Max % of account to risk on this trade",
                                  min_value=0.1, max_value=100.0, value=1.0,
                                  step=0.5, key=f"risk_{load_key}")
    budget = acct * riskpct / 100.0
    rpu = abs(plan["entry"] - plan["stop"])          # risk per unit (price distance)
    if rpu > 0 and budget > 0:
        if res["kind"] == "stock":
            shares = int(budget // rpu)
            if shares == 0:
                st.warning(_md(f"Even **1 share** risks ${rpu:,.2f} to the stop — more "
                           f"than your ${budget:,.0f} budget. This trade is too big "
                           f"for that risk limit; lower the size or skip."))
            else:
                notional = shares * plan["entry"]
                afford = int(acct // plan["entry"]) if plan["entry"] > 0 else 0
                use, note = shares, ""
                if notional > acct:
                    use = min(shares, afford)
                    note = (f" (risk math allows {shares}, but that needs "
                            f"${notional:,.0f}; capped to {afford} to stay "
                            f"unleveraged within your ${acct:,.0f})")
                st.info(_md(f"➡️ About **{use} shares** (~${use*plan['entry']:,.0f}). "
                        f"If the stop hits, you lose roughly **${use*rpu:,.0f}** — "
                        f"~{use*rpu/acct*100:.1f}% of your account.{note}"))
        else:
            cs = res.get("contract_size", 1)
            unitname = res["unit"].split("/")[-1]
            rpc = rpu * cs                            # risk per futures contract
            contracts = int(budget // rpc)
            if contracts == 0:
                st.warning(_md(f"Even **1 contract** risks ~${rpc:,.0f} to the stop "
                           f"(it controls {cs:,} {unitname} of {name}) — more than "
                           f"your ${budget:,.0f} budget. This futures trade is too "
                           f"big for that risk limit; skip it or you're over-risking."))
            else:
                notional = contracts * plan["entry"] * cs
                st.info(_md(f"➡️ About **{contracts} contract(s)** "
                        f"({res['contract_label']}). If the stop hits, you lose "
                        f"roughly **${contracts*rpc:,.0f}** "
                        f"(~{contracts*rpc/acct*100:.1f}% of your account). "
                        f"⚠️ Exposure is ~${notional:,.0f} of {name} — futures are "
                        f"leveraged, and a gap past your stop can lose more."))
    st.caption("Sizing caps your loss *if* the stop fills at that price — gaps and "
               "slippage can exceed it. A common rule is risking 1–2% per trade. "
               "Not advice.")

if res.get("dir_tot", 0) >= 10 and res.get("dir_acc") is not None and res["dir_acc"] < 0.5:
    st.caption(f"⚠️ Reality check: on {name}, the model's directional calls have "
               f"been right only {res['dir_acc']*100:.0f}% lately ({res['dir_tot']} "
               f"graded) — treat this lean with extra skepticism.")
st.caption("Mechanical levels from the model's own range — **not financial "
           "advice**, and the model just got copper's direction wrong. Use the "
           "stop, size small, risk only what you can lose.")

# -------------------------------------------------------------- metrics row --
tone = ("Bullish" if res["combined_score"] > 0.15 else
        "Bearish" if res["combined_score"] < -0.15 else "Neutral")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Anchor price", f"${fmtp(spot)}",
          "live" if price_mode == "Auto-fetch live" else "your price")
m2.metric("News pressure", f"{res['combined_score']:+.2f}", tone)
beta_help = ("market beta — moves with the S&P" if res["kind"] == "stock"
             else "inverse if negative")
m3.metric(f"{res['ref_label']} beta", f"{res['ref_beta']:+.2f}", beta_help)
wk = res["backtests"].get("1 week") or {}
m4.metric("Weekly direction hit-rate",
          f"{wk['directional_acc']*100:.0f}%"
          if wk.get("directional_acc") is not None else "n/a", "backtested")

if res["scenario"]:
    st.warning(f"**Scenario overlay: {res['scenario']['label']}** — "
               f"{res['scenario']['why']}")

if conv == "STRONG":
    plain = ("multiple horizons agree and the expected move is large relative to "
             "the noise — the model's most confident kind of call. Even so, "
             "'strong' means a better-than-usual edge, not a sure thing.")
elif lean == "HOLD":
    plain = ("the up-case and down-case are roughly balanced for the week ahead, "
             "so the model isn't pointing clearly either way.")
else:
    d = "upside" if lean == "BUY" else "downside"
    plain = (f"the model tilts toward {d} for the week, but it's a softer signal. "
             f"Short-horizon calls are close to a coin toss — treat it as a lean.")
st.markdown(f"**In plain terms:** {plain}")

# ----------------------------------------------- 🏆 best strategy callout ----
bs = res["best_strategy"]
if bs:
    call = bs["current_call"]
    cfg_call = {"LONG": ("#0F6E56", "#E1F5EE"), "SHORT": ("#A32D2D", "#FCEBEB")}
    cfo, cbo = cfg_call.get(call, ("#5F5E5A", "#F1EFE8"))
    cap0 = bs["start_capital"]
    if bs["net_total"] <= 0:
        verdict = ("No strategy made money after costs over the backtest — "
                   "**holding (or staying in cash) beat trading.**")
    else:
        tail = (" and **beat buy & hold**." if bs.get("beats_hold")
                else f", but **buy & hold did better** (${bs['buy_hold_net']:,.0f}).")
        verdict = (f"made **${bs['net_total']:,.0f}** ({bs['ret_pct']:+.1f}%) on a "
                   f"${cap0:,.0f} stake{tail}")
    st.markdown(
        f"""<div style="background:{cbo};border-left:7px solid {cfo};border-radius:8px;
padding:13px 18px;margin:8px 0 2px;">
<div style="font-size:18px;font-weight:600;color:{cfo};">
🏆 Best-performing strategy in backtest: {bs['label']}</div>
<div style="font-size:14px;color:#3d3d3a;margin-top:5px;line-height:1.5;">
Over the last ~2 years it {verdict}<br>
Right now this strategy says: <b style="color:{cfo};">{call}</b>
&nbsp;(win rate {bs['win_rate']}%, profit factor {bs.get('profit_factor','—')}).</div>
<div style="font-size:12px;color:#5f5e5a;margin-top:7px;">
This is the strategy that <i>worked best historically</i> — not a recommendation,
and past results don't predict the future.</div>
</div>""", unsafe_allow_html=True)

# ------------------------------------------------------------------- charts --
st.subheader("Six horizon charts")
order = ["4 hours", "Next day", "Next 2 days", "Next week", "Next month", "Next 3 months"]
hist_for = {hf.name: ("hourly" if fr == "hourly" else "daily") for hf, fr in fcs}
rows3 = [st.columns(2), st.columns(2), st.columns(2)]
for i, nm in enumerate(order):
    hf = by_name.get(nm)
    if hf is None:
        continue
    freq = hist_for[nm]
    histser = res["hourly"] if (freq == "hourly" and res["have_hourly"]) else res["daily"]
    fig = mp.fan_chart(hf, histser, spot, unit=unit,
                       hist_points=48 if freq == "hourly" else 55)
    rows3[i // 2][i % 2].pyplot(fig, width='stretch')
st.caption("Green = the model leans up over that window, coral = down. Shaded "
           "bands are the likely range (darker 68%, lighter 95%). Dotted line = "
           "price now. End label = central estimate and % move.")

# ----------------------------------------------------- by-horizon summary ----
def mark(s):
    a = "↑" if s["lean"] == "BUY" else "↓" if s["lean"] == "SELL" else "→"
    return f"{a} {s['lean']} ({s['strength']})"

rows = []
for nm in order:
    hf = by_name.get(nm)
    if hf is None:
        continue
    s = res["signals"][nm]
    e = lambda a: float(a[-1])
    rows.append({"Horizon": nm, "Central": f"${fmtp(e(hf.median))}",
                 "Move": f"{(e(hf.median)/spot-1)*100:+.2f}%",
                 "Likely range (68%)": f"${fmtp(e(hf.lo68))} – ${fmtp(e(hf.hi68))}",
                 "Odds up": f"{hf.p_up*100:.0f}%", "Signal": mark(s)})
st.subheader("At a glance — every horizon")
st.table(pd.DataFrame(rows).set_index("Horizon"))

# ------------------------------------------- strategy comparison / P&L ------
st.subheader("💵 Strategy comparison — did trading actually make money? (after costs)")
pnl = res["pnl"]
prows = []
for nm, v in pnl.items():
    if not v or not v.get("trades"):
        prows.append({"Strategy": nm, "Trades": (v.get("trades", 0) if v else 0),
                      "Win %": "—", "Net P&L": "—", "Return": "—",
                      "Max drawdown": "—", "Profit factor": "—", "Buy & hold": "—"})
        continue
    pf = v["profit_factor"]
    prows.append({
        "Strategy": nm, "Trades": v["trades"], "Win %": f"{v['win_rate']}%",
        "Net P&L": f"${v['net_total']:,.0f}", "Return": f"{v['ret_pct']:+.1f}%",
        "Max drawdown": f"-${v['max_drawdown']:,.0f} ({v['max_dd_pct']:.0f}%)",
        "Profit factor": f"{pf}" if pf is not None else "∞",
        "Buy & hold": f"${v['buy_hold_net']:,.0f}"})
st.table(pd.DataFrame(prows).set_index("Strategy"))

traded = [v for v in pnl.values() if v and v.get("trades")]
if traded:
    best = max(v["net_total"] for v in traded)
    cap0 = next(iter(traded))["start_capital"]
    bh0 = max(v.get("buy_hold_net", 0) for v in traded)
    if best <= 0:
        st.error(f"**Every strategy LOST money** over the backtest after costs, "
                 f"from a ${cap0:,.0f} start. That's the coin-flip-plus-costs "
                 f"reality — holding or cash would have done better.")
    elif best < bh0:
        st.warning(f"The best strategy made money, **but buy & hold beat all of "
                   f"them** (${bh0:,.0f}). Timing added cost and risk without "
                   f"beating just owning it.")
    else:
        st.warning("One strategy beat buy & hold on paper — treat it with heavy "
                   "skepticism: a single historical path, costs are estimates, "
                   "and the live signal differs from this replay. Not a green light.")
    if any(v.get("blew_up") for v in traded):
        st.error("⚠️ At least one strategy drove the account to $0 — a blow-up. "
                 "That's the leverage risk (futures) made real.")
if res["bt_mode"] == "futures":
    st.caption(f"Assumptions: $10,000 start · 1 {profile['contract_label']} "
               f"leveraged futures contract · realistic spread + commission · no "
               f"lookahead. Past simulated results do not predict the future. "
               f"Not financial advice.")
else:
    st.caption("Assumptions: $10,000 start · buy/sell the share **unleveraged**, "
               "full stake each trade · ~5 bps round-trip cost · no lookahead. "
               "Past simulated results do not predict the future. Not financial "
               "advice.")

# ----------------------------------------------------- price path tables -----
st.subheader("Price path breakdown")

def step_table(hf, idxs, fmt_):
    out = []
    for i in idxs:
        if i < len(hf.median):
            out.append({"When": hf.times[i].strftime(fmt_),
                        "Expected": f"${fmtp(hf.median[i])}",
                        "Range (68%)": f"${fmtp(hf.lo68[i])}–${fmtp(hf.hi68[i])}"})
    return pd.DataFrame(out).set_index("When")

t1, t2, t3 = st.columns(3)
with t1:
    st.markdown("**Every 4 hours (next 2 days)**")
    h2 = by_name.get("Next 2 days")
    if h2 is not None:
        st.table(step_table(h2, range(3, 48, 4), "%b %d %H:%M"))
with t2:
    st.markdown("**Each day (next 10 days)**")
    hm = by_name.get("Next month")
    if hm is not None:
        st.table(step_table(hm, range(0, 10), "%b %d"))
with t3:
    st.markdown("**Each week (next 12 weeks)**")
    h3 = by_name.get("Next 3 months")
    if h3 is not None:
        st.table(step_table(h3, range(4, 63, 5), "%b %d"))

# ---------------------------------------------- recent grading detail -------
ev = res["evaluations_recent"]
if ev:
    st.subheader("📋 Most recent graded predictions")
    tbl = [{"Made": r["made"], "Horizon": r["horizon"],
            "Predicted": f"${fmtp(float(r['predicted']))}",
            "Actual": f"${fmtp(float(r['actual']))}",
            "In range": "✓" if r.get("in68") == "1" else "✗",
            "Right direction": "✓" if r.get("correct") == "1" else "✗",
            "Miss": f"{float(r['err_pct']):+.2f}%"} for r in ev]
    st.table(pd.DataFrame(tbl).set_index("Made"))

# --------------------------------------------------- drivers + why + events --
st.subheader(f"What could move {name} — and why it's leaning now")
d1, d2 = st.columns(2)
with d1:
    st.markdown("**Could push it UP ▲**")
    st.markdown("\n".join(f"- {x}" for x in res["drivers_up"]))
with d2:
    st.markdown("**Could push it DOWN ▼**")
    st.markdown("\n".join(f"- {x}" for x in res["drivers_down"]))

w1, w2 = st.columns(2)
with w1:
    st.markdown("**Why the model leans this way (weekly drivers)**")
    wkhf = by_name.get("Next week", fcs[0][0])
    comp = {k: v for k, v in wkhf.components.items() if not k.startswith("_")}
    if comp:
        st.table(pd.DataFrame(
            [{"Signal": k, "Daily nudge": f"{v:+.5f}",
              "Pushes": "up ▲" if v > 1e-6 else "down ▼" if v < -1e-6 else "—"}
             for k, v in comp.items()]).set_index("Signal"))
    if news.headlines:
        st.markdown("**Headlines moving the news score:**")
        for h, c in news.headlines[:4]:
            st.markdown(f"- {'▲' if c > 0 else '▼' if c < 0 else '•'} {h}")
with w2:
    st.markdown("**Scheduled market-movers ahead**")
    if res["events"]:
        erows = [{"When (UTC)": e.when.strftime("%b %d %H:%M"),
                  "In": f"{(e.when-res['now']).total_seconds()/3600:.0f}h",
                  "Event": e.name,
                  "Impact": "●●●" if e.vol_mult >= 1.4 else
                            "●●" if e.vol_mult >= 1.25 else "●"}
                 for e in res["events"]]
        st.table(pd.DataFrame(erows).set_index("When (UTC)"))

st.divider()
with st.expander("⚠️  Read this — limits & disclaimer"):
    st.markdown(
        "- The **range and the odds** are the real output; BUY/SELL is the "
        "model's directional tilt, not a recommendation.\n"
        "- Short-horizon direction is close to a coin flip — see the hit-rates "
        "and the scoreboard. No model, this one included, is near 100% accurate.\n"
        "- The 'best strategy' is whatever scored best *in the backtest*; it is "
        "not advice and often loses to simply buying and holding.\n"
        "- It logs every forecast, grades itself as targets pass, counts its "
        "adjustments, and recalibrates — but it cannot foresee shocks.\n"
        "- **This is a learning tool. It is not financial advice. Do not risk "
        "money you can't afford to lose on it.**")
st.caption(f"Generated {res['now']:%Y-%m-%d %H:%M} UTC · {res['ticker']} · "
           f"anchor ${fmtp(spot)} · "
           f"{'GARCH' if fcs[0][0].vol_method=='garch' else 'EWMA'}"
           f"{' + LightGBM' if res['lgbm_used'] else ''}"
           f"{' + ' + res['ref_label'] if res['ref_loaded'] else ''}")
