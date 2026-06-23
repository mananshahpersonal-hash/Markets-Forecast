#!/usr/bin/env python3
"""app.py — one unified forecast dashboard for METALS and STOCKS.

Pick a mode (Metals or Stocks). For metals choose copper/gold/silver/aluminium;
for stocks type any ticker. The engine forecasts six horizons, auto-picks the
strategy that has worked best on that asset, and — above all — grades its own
past calls, counts the adjustments it makes, and shows whether it's improving.

Educational. Ranges and odds, not promises. NOT financial advice."""
import os
import pandas as pd
import streamlit as st

import copper_forecaster as cf
import model_pro as mp
import assets
import storage

st.set_page_config(page_title="Markets forecast", page_icon="📈", layout="wide")


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

METAL_META = {"copper": ("Copper", "🟠"), "gold": ("Gold", "🟡"),
              "silver": ("Silver", "⚪"), "aluminium": ("Aluminium", "⚫")}

# ----------------------------------------------------------- mode + picker ---
mode = st.radio("What do you want to forecast?",
                ["Metals", "Stocks", "Overview (all at once)"], horizontal=True)

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
                   use_container_width=True)
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

asset_key = stock_ticker = None
if mode == "Metals":
    asset_key = st.selectbox(
        "Choose metal", list(METAL_META.keys()),
        format_func=lambda k: f"{METAL_META[k][1]}  {METAL_META[k][0]}")
    profile = assets.get(asset_key)
    name, icon = METAL_META[asset_key]
else:
    stock_ticker = st.text_input("Enter any stock ticker", value="AAPL",
                                 max_chars=8).upper().strip()
    if not stock_ticker:
        st.info("Type a ticker (e.g. AAPL, MSFT, NVDA, TSLA) to begin.")
        st.stop()
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

run = st.button("🔄  Update prediction now", type="primary", use_container_width=True)

need = (run or "result" not in st.session_state
        or st.session_state.get("load_key") != load_key)
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
    rows3[i // 2][i % 2].pyplot(fig, use_container_width=True)
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
