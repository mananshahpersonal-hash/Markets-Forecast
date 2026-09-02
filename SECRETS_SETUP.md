# Adding your Finnhub key (one time, ~2 minutes)

The app reads your Finnhub API key from **Streamlit Secrets** — never from the
code, so it never appears on GitHub.

## Steps

1. Go to **share.streamlit.io** and open your app's dashboard.
2. Click the **⋮** menu (or **Manage app**) → **Settings** → **Secrets**.
3. In the Secrets box, paste this one line (with your real key inside the quotes):

   ```
   FINNHUB_KEY = "your_key_here"
   ```

4. Click **Save**. Streamlit restarts the app automatically (~1 min).
5. Open the app → **My Portfolio** tab. Under the Total-equity headline you'll
   see **📡 Live price source: Finnhub ✓** — that confirms it's working.

## Notes

- If you ever see **Yahoo (no Finnhub key)** there, the key isn't being read —
  re-check the Secrets box for typos or missing quotes.
- Free Finnhub keys allow ~60 calls/minute — plenty for this portfolio.
- Rotate the key anytime on the Finnhub dashboard; just update the Secrets box.
- If Finnhub is ever down or a symbol isn't covered, the app automatically falls
  back to Yahoo, then to your verified dividend table — it never breaks.

## TIINGO_KEY — daily price history (needed since Yahoo died)

Yahoo's history API is dead and Finnhub's free tier only serves live quotes,
not history. Tiingo's free tier fills the gap (covers the metal ETFs and all
your stocks, ~1000 requests/day, no card needed):

1. Go to https://www.tiingo.com → **Sign up** (email only, free).
2. After login: **Account → API** — copy your API token.
3. Streamlit: **Manage app → Settings → Secrets**, add a new line:
   `TIINGO_KEY = "paste-your-token-here"`
4. **Save**, then **Reboot** the app.

The 🔬 "Diagnose price feeds" panel will then show
`✅ Tiingo (history) — N daily closes` and the metals forecasts come back.
(TWELVEDATA_KEY and ALPHAVANTAGE_KEY are also supported as spares.)
