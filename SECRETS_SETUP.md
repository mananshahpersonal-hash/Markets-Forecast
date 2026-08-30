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
