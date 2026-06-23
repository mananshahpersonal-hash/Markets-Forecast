# ▶️ START HERE — one page for metals *and* stocks

Click a button, pick what to forecast, see the prediction. Here's exactly that.

**One unified dashboard now does two things:**
- **Metals** — Copper, Gold, Silver, Aluminium (pick from the list)
- **Stocks** — type **any ticker** (AAPL, MSFT, NVDA, TSLA, …)

Same engine for both: six-horizon forecasts, an auto-picked "best strategy,"
and — the heart of it — a **learning scoreboard** that grades its own past
calls, counts every adjustment it makes, and shows whether it's improving.

## First time only (install Python, ~2 min)
You need **Python 3.10 or newer**. Check in a terminal: `python --version`
(Windows) or `python3 --version` (Mac/Linux). Missing? Install from
**python.org/downloads** — on Windows tick **"Add Python to PATH."**

## Run it
- **Windows:** double-click **`Run_Copper_Forecast.bat`** (it launches the full
  metals+stocks app — the filename is just historical). If SmartScreen warns →
  **More info → Run anyway**.
- **Mac:** double-click **`Run_Copper_Forecast.command`** (first time:
  right-click → Open → Open).
- **Linux:** `./run.sh` in this folder.

First run installs everything automatically, then your browser opens.

## Using it
1. **Pick a mode** at the top: **Metals** or **Stocks**.
2. Metals → choose the metal. Stocks → **type a ticker**.
3. Leave **Auto-fetch live** on, or switch to **Use my price**.
4. Optional: pick a **what-if scenario** (choices change per asset).
5. Hit **🔄 Update prediction now**.

### What you'll see
- A conviction-rated **BUY / SELL / HOLD** read with the odds of going up.
- **📊 What the indicators say** — a plain-English line for each proven
  indicator (trend, RSI, MACD, Bollinger). A STRONG signal only fires when the
  forecast and several indicators agree.
- **🧠 Learning scoreboard:** predictions logged, how many it's **won** (right
  direction), how often prices landed in its range, **how many adjustments it's
  made**, and a chart of whether its accuracy is trending up.
- **🏆 Best-performing strategy** for that asset (trend vs mean-reversion vs the
  ensemble), what it made in backtest, and whether it beat just buying & holding.
- Six forecast charts, a strategy/P&L comparison, per-step price tables, the
  drivers, and the scheduled events that could move it.

Each asset (every metal **and** every ticker) keeps its **own separate track
record** and learns independently. It gets sharper the more days you run it.

---

### Always-on hourly runner?
The separate `copper_forecaster.py` runner can run every hour and text/email you
— see **README.md**. (It's currently wired for copper.)

> Reminder: this is a learning tool. Forecasts are **ranges, not promises**, and
> **not financial advice**. The scoreboard and strategy tables show the real,
> measured performance — read them. Short-horizon direction is close to a coin
> flip for everything here, the "best strategy" often loses to just holding, and
> leverage (futures) can wipe an account out. Risk only what you can afford to lose.
