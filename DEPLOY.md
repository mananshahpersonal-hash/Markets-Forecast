# 📱 Put it online — reachable from any phone (Wi-Fi or 5G), memory that sticks

Goal: a permanent web link you open on your phone anytime, **with the learning
saved** so the scoreboard keeps getting better across visits — and your PC can
be **off**. All free.

You'll use two free things:
- **Streamlit Community Cloud** — hosts the app and gives you the link.
- **A private GitHub Gist** — the "memory" where the learning state is saved
  (Streamlit's own storage wipes on restart, so we keep memory in the gist).

Total time: ~15 minutes, once.

---

## 1. Get a free GitHub account
If you don't have one: go to **github.com** → Sign up.

## 2. Put this project in a GitHub repo
Easiest (no command line):
1. github.com → **New repository** → name it e.g. `markets-forecast` →
   you can keep it **Private** → Create.
2. On the repo page → **Add file → Upload files** → drag in **all** the files
   from this folder (app.py, model_pro.py, assets.py, storage.py,
   copper_forecaster.py, requirements.txt, config.example.yaml, etc.) →
   **Commit changes**.

## 3. Create the memory gist (where learning is saved)
1. Go to **gist.github.com**.
2. In "Filename" type `placeholder.txt`; in the body type `init`.
3. Click the dropdown next to the button and choose **Create secret gist**.
4. Look at the URL — it ends in a long id, e.g.
   `https://gist.github.com/yourname/`**`3f9a2b7c8d1e4f5a6b7c8d9e0f1a2b3c`**.
   **Copy that id** — that's your `GIST_ID`.

## 4. Make a token that can write only to gists
1. github.com → your photo → **Settings** → **Developer settings** →
   **Personal access tokens** → **Tokens (classic)** → **Generate new token
   (classic)**.
2. Note: "markets-forecast gist". Expiration: your choice (or no expiration).
3. Tick **only** the **`gist`** checkbox. (Nothing else — it can't touch your
   code or account.)
4. **Generate token** and **copy it** (starts with `ghp_…`). You won't see it
   again, so paste it somewhere for the next step.

## 5. Deploy on Streamlit Community Cloud
1. Go to **share.streamlit.io** → sign in with GitHub → **Authorize**.
2. **Create app → Deploy a public app from a repo** (private repos work too).
3. Pick your repo, branch **main**, and **Main file path** = `app.py`.
4. Click **Advanced settings → Secrets** and paste these two lines (with your
   real values, keep the quotes):
   ```toml
   GITHUB_TOKEN = "ghp_your_token_here"
   GIST_ID = "your_gist_id_here"
   ```
5. **Deploy.** First build takes a few minutes (it's installing the packages).

## 6. Open it on your phone
When it's live you'll get a URL like `https://your-app.streamlit.app`.
- Open it in your phone's browser (works on **Wi-Fi or cellular/5G**).
- **Add to Home Screen** so it opens like an app:
  - iPhone (Safari): Share → *Add to Home Screen*.
  - Android (Chrome): ⋮ menu → *Add to Home screen*.

At the top it should now say **"☁️ Cloud memory: ON."** That confirms the
learning is being saved to your gist.

---

## How the "keeps learning" part works now
- Every time you open the app and hit **Update**, it logs its new forecasts to
  the gist, and **grades any past forecasts that have come due** against real
  prices — then updates the scoreboard and saves it back.
- Because grading reads historical prices, it works even if you only visit
  occasionally: when you open it, it catches up on everything due since last
  time. The memory lives in the gist between visits.
- Each metal **and** each stock ticker keeps its **own** saved memory.

### Optional: keep it improving even when you're not looking
Streamlit apps sleep after inactivity. If you want it to wake and self-grade on
a schedule, set up a free pinger (e.g. **cron-job.org**) to load your app URL
once or twice a day. Optional — opening it yourself already does the job.

## Security notes
- The token has **only the `gist` scope** — it can't read or change your code,
  repos, or anything else. You can **revoke** it anytime in GitHub settings.
- Keep the token in **Streamlit Secrets only** — never put it in the repo.
- Make the memory gist **secret** (step 3) so your track record stays private.

## If live prices don't load on the cloud
Yahoo data occasionally rate-limits cloud servers. If a fetch fails, switch the
price control to **"Use my price"**, type the current number, and Update — the
forecast and learning still work.

> Still just a learning tool — ranges and odds, not promises, **not financial
> advice**. Hosting it doesn't make it more accurate; it just makes it reachable
> and lets the calibration accumulate.
