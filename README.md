# Metals price forecaster (educational) — Copper · Gold · Silver · Aluminium

> **Now multi-asset.** The dashboard (`app.py`) covers **metals (copper, gold,
> silver, aluminium) and any stock ticker** — pick a mode at the top — pick the metal from the dropdown at the top. Each metal has
> its own drivers, what-if scenarios, news terms, futures-contract sizing for the
> P&L backtest, and its **own separate self-learning track record**. The engine
> (ensemble, GARCH bands, learning loop, P&L) is shared; only the per-metal
> profiles in `assets.py` differ. The hourly background runner below is currently
> wired for copper.

---

# Copper price forecaster (educational)

A self-recalibrating copper price model that, **once per hour**, pulls price
data, reads the news, checks a calendar of scheduled market-moving events, and
produces probabilistic forecasts + charts for **4 hours, 1 day, 1 week, and
1 month** ahead — then notifies you and learns from how its past forecasts did.

> **Read this first.** No model reliably predicts short-term copper prices —
> not banks, not hedge funds, not this. What this tool produces is an honest
> **range with a confidence level** plus **event-aware risk flags**, not a magic
> point price. The dashed "median" line is the *middle* of the range, not a
> promise. **This is a learning project. It is not financial advice and must not
> be used to make real trades.**

---

## ⭐ Two ways to run this

**Easiest — one click (on-demand dashboard).** See **START_HERE.md**.
Double-click the launcher for your OS → your browser opens → click
**Update prediction now**. This runs the upgraded ensemble model described next.

**Always-on — hourly background runner with phone alerts.** That's
`copper_forecaster.py`, covered in sections 2–3 below.

## What's new in the upgraded model (`app.py` + `model_pro.py`)

The dashboard uses an upgraded engine built on the techniques that actually win
forecasting competitions — combining models, not betting on one:

- **Ensemble of signals**: random-walk baseline + AR(1) **mean reversion** +
  **momentum** + a live **US-dollar-index signal** (copper moves inversely to
  the dollar) + **news/events** + optional **LightGBM** (the M5-winning method,
  used automatically if installed). Combining beats any single model.
- **GARCH(1,1) volatility** for proper time-varying uncertainty (auto-fallback
  to EWMA if the library is absent).
- **Empirical / conformal-style bands** built from copper's *actual* historical
  move distribution, so the ranges respect fat tails instead of a tidy bell curve.
- A **walk-forward backtest** that *measures* the model's real directional
  accuracy and how often prices actually land inside its bands.

### About "accuracy" — the honest version
There is **no ~100%-accurate copper model**, anywhere. The backtest panel in the
dashboard shows the truth, and it's worth internalising:
- **Short-horizon direction is ~50% — a coin flip.** That's not a flaw in this
  model; it's the nature of markets. Anyone claiming reliable intraday direction
  is selling something.
- Directional edge improves modestly at longer horizons.
- The genuinely useful, achievable goal is **well-calibrated ranges** — when the
  model says "68% range," the real price should land inside it ~68% of the time.
  That's what the coverage numbers measure, and it's the right definition of
  "best." Anchored to your entered price (e.g. your HGU26 print).

---

## 1. Quick start (the hourly background runner)

```bash
cd copper_forecaster
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.yaml config.yaml                    # then edit config.yaml
python copper_forecaster.py --once                    # run one cycle
```

Charts land in `output/` and a summary prints to your terminal. The first run
just establishes a baseline; the model starts *learning* from the second run on,
once some forecasts have a real outcome to compare against.

Try a what-if shock:

```bash
python copper_forecaster.py --list-scenarios
python copper_forecaster.py --once --scenario mideast_war
```

---

## 2. Make it run every hour

You asked for hourly, always-on operation. A chat assistant can't be that
process — but **your machine or a cheap cloud box can**, and then it runs forever
without anyone babysitting it. Two ways:

### Option A — a scheduler runs the script (recommended, most reliable)

**Linux / macOS (cron).** `crontab -e`, then add:

```cron
0 * * * * cd /full/path/to/copper_forecaster && /full/path/to/.venv/bin/python copper_forecaster.py --once >> output/cron.log 2>&1
```

**Windows (Task Scheduler).** Create a Basic Task → trigger *Daily*, then in the
trigger's advanced settings tick *Repeat task every 1 hour* for *1 day*. Action
= *Start a program*: point it at your `python.exe` with argument
`copper_forecaster.py --once` and "Start in" = the project folder.

**Cloud (so it runs while your laptop sleeps).** Any always-on box works — a $5
VPS, a Raspberry Pi, or a free-tier scheduler. The pattern is identical: clone
the folder, install requirements, and run `--once` on an hourly cron. (On
serverless platforms, schedule an hourly invocation of the same command.)

### Option B — the script schedules itself

```bash
python copper_forecaster.py --loop
```

This runs immediately, then every hour, until you stop it (`Ctrl+C`). Simple,
but it dies if the terminal closes or the machine reboots — so for true
"always running," use Option A, or wrap this in a service (systemd / `pm2` /
`nohup`).

---

## 3. Get notifications on your phone

### Telegram (easiest, free)
1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts, and
   copy the **bot token** it gives you.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and copy
   the numeric `chat.id`.
3. Put both in `config.yaml` under `notify.telegram` and set `enabled: true`.

You'll then get each hourly summary plus the four charts pushed to your phone,
with a loud **⚠ ALERT** when a high-impact event is near or the news turns
extreme.

### Email
Fill in `notify.email` (for Gmail, create an **app password** — not your normal
password — and use that). Set `enabled: true`.

---

## 4. How to read the output

Each horizon gets a **fan chart**:

- **Solid grey line** — recent actual price history.
- **Dashed green line** — the median (most-central) projected path.
- **Dark green band** — the 68% range (roughly 2-in-3 chance the price ends here).
- **Light green band** — the 95% range (roughly 19-in-20 chance).

The right way to read it: *"there's about a two-thirds chance copper is between
X and Y after this horizon."* The band **widening** the further out you look is
the honest part — uncertainty genuinely grows with time.

The text summary gives you, in plain English: the current price, the **news
pressure** score (−1 bearish … +1 bullish) with the headlines driving it, the
four ranges with their % moves, the **upcoming scheduled events**, and the
model's current **calibration** (how it's adjusted itself based on its track
record).

---

## 5. How the model works (plain English)

Seven steps, every run:

1. **Price data.** 10 years of daily prices (for the week/month view) and ~60
   days of hourly prices (for the 4-hour/1-day view) from Yahoo Finance.

2. **Features.** It measures recent **volatility** (how jumpy copper has been
   lately, via an exponentially-weighted average that leans on recent days) and
   a small, heavily-discounted **trend**.

3. **News + events.** It reads recent copper headlines from news feeds and
   scores them with a transparent keyword map (a *mine strike* or *supply
   deficit* pushes bullish; *recession* or *hawkish Fed* pushes bearish). It
   also checks a **calendar of scheduled events** — Fed decisions, the US copper
   tariff deadline, China policy windows, CPI/jobs/PMI releases — because those
   reliably cause bigger swings even before anyone knows the outcome.

4. **Forecast.** The backbone is a **geometric random walk**: start at today's
   price, let uncertainty fan out as `volatility × √time`. Two adjustments ride
   on top:
   - **News** nudges the *centre* of the range a little in the news direction,
     and more so for the short horizons (news gets "priced in" fast, so its
     pull fades for the week/month views).
   - **Scheduled events** *widen* the range for any window that contains one —
     because the safest thing to say before a Fed meeting is "expect a bigger
     move," not "it'll go up."

5. **Charts** for all four horizons.

6. **Notify** you, with an **ALERT** when something big is imminent.

7. **Learn.** Every forecast is logged. On later runs, once a forecast's target
   time has passed, it's compared to what *actually* happened. If the 68% band
   only contained the real price 40% of the time, the bands were too tight, so
   it **widens** them. If news direction kept being wrong, it **trusts news
   less**. That's the self-evolving loop — modest, bounded, and fully auditable
   in `state/calibration.yaml`.

---

## 6. Why copper moves — the 10-year lesson baked into the model

The model's logic comes from *why* copper actually moved over the past decade.
The recurring drivers, roughly in order of punch:

- **The US dollar / Fed policy.** Copper is priced in dollars, so a stronger
  dollar makes it pricier for everyone else and tends to push the price down —
  and vice versa. This is why **FOMC meetings** are the single biggest scheduled
  event in the calendar. (The Fed cut three times in 2024 and three more in
  2025; a weaker dollar was a big reason metals rallied.)

- **Chinese demand.** China is *over half* of global copper consumption, so
  Chinese factory data (PMIs), the property sector, and stimulus announcements
  swing the price hard. China's **"Two Sessions"** in March is a key policy
  window.

- **Supply shocks.** Copper supply is concentrated (Chile and Peru dominate
  mining). Strikes, weather, and political disruptions cause sharp spikes — e.g.
  the **Cobre Panamá mine closure (late 2023)** removed a big chunk of supply and
  helped drive prices to records in 2024. Smelter cuts do the same.

- **Inventories.** Low warehouse stocks (LME / SHFE / COMEX) = bullish; building
  stockpiles = bearish.

- **Structural demand (the long-term bull story).** Electrification, the power
  grid, EVs, renewables, and — newly dominant — **AI data centres** all need
  enormous amounts of copper. This is why long-horizon forecasts lean slightly
  bullish.

- **Macro risk sentiment.** Copper is nicknamed *"Dr. Copper"* because it tracks
  the global economy — it sells off hard on recession fears and rallies on growth
  optimism.

- **Geopolitics & policy.** Resource nationalism, export bans, war (via oil/
  energy and risk appetite), and **trade policy**. In 2025 the US put a **50%
  tariff on semi-finished copper**, triggering stockpiling; a **June 30, 2026
  review deadline** on a possible refined-copper duty has kept the market on edge
  all year — that's why it's a high-impact date in the calendar.

A live illustration of all this: copper hit a record near **$14,500/tonne in
January 2026**, sold off during the 2026 Iran conflict on growth fears, then
bounced as the conflict de-escalated and oil fell. Macro, supply, geopolitics,
and the dollar — all at once. That's exactly the cocktail the model watches.

---

## 7. What-if scenarios

The `--scenario` flag overlays a hypothetical shock onto the live forecast, so
you can ask *"what happens to my chart if X kicks off?"* Built-in:
`mideast_war`, `china_stimulus`, `major_mine_outage`, `fed_hawkish_surprise`,
`fed_dovish_surprise`, `recession_scare`, `us_copper_tariff`. Each shifts the
range's centre and width by a documented, editable amount (see `SCENARIOS` in
the code).

---

## 8. Limitations — please internalise these

- **Short-horizon point forecasts are essentially noise.** The 4-hour "median"
  is barely distinguishable from "the current price." The *range* is the useful
  output, not the line.
- **Volatility scaling assumes calm-ish markets.** Real crashes have fat tails;
  the 95% band will be breached more than 5% of the time around true shocks.
- **The keyword news scorer is shallow.** It can misread sarcasm, context, or
  novelty. The optional Claude classifier (`anthropic.enabled: true`) reads
  headlines far better.
- **The recurring economic-event dates are approximate placeholders.** Replace
  them with exact dates from an economic calendar for real precision. The FOMC
  dates are real; CPI/jobs/PMI are monthly approximations.
- **It does not model fat tails, regime shifts, positioning, or term structure.**
  Those are the obvious next upgrades.

### Making it better (good learning extensions)
- Swap EWMA volatility for a **GARCH** model (`arch` package) — better at vol
  clustering.
- Pull **real economic-calendar dates** via an API instead of placeholders.
- Add **LME/COMEX inventory** and the **DXY dollar index** as live features.
- Backtest the calibration loop over history and chart your **realised hit
  rate** from `state/evaluations.csv`.

---

## 9. Files

```
copper_forecaster/
├── copper_forecaster.py     # the whole engine (read the top docstring)
├── requirements.txt
├── config.example.yaml      # copy to config.yaml and edit
├── README.md
├── output/                  # charts + logs (created on first run)
└── state/                   # forecast log, evaluations, learned calibration
```
