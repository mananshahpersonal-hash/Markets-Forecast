"""assets.py — per-asset profiles for the multi-metal forecaster.

Each metal has its OWN: ticker, price unit, futures contract size (for the P&L
backtest), news search terms, scenario overlays, and up/down drivers — because
their economics genuinely differ. Gold is a safe-haven (war = up; rate hikes =
down); copper and aluminium are industrial (recession = down); silver is a
hybrid. The shared engine (ensemble, GARCH, learning loop, P&L) doesn't change —
only these profiles do.
"""
import copper_forecaster as cf

# --- shared macro terms (dollar / Fed / supply) common to all metals ---------
_MACRO_BULL = {
    "rate cut": 0.8, "weaker dollar": 0.7, "dollar falls": 0.7, "stimulus": 0.7,
    "low inventories": 0.7, "supply deficit": 0.9, "deficit": 0.5, "shortage": 0.8,
    "production cut": 0.8, "smelter cut": 0.8, "force majeure": 0.9, "output cut": 0.7,
    "strike": 0.6, "disruption": 0.6,
}
_MACRO_BEAR = {
    "rate hike": 0.8, "hawkish": 0.7, "stronger dollar": 0.7, "dollar rises": 0.7,
    "dollar surge": 0.8, "surplus": 0.8, "oversupply": 0.8, "inventory build": 0.6,
    "rising stockpiles": 0.6, "sell-off": 0.5, "selloff": 0.5, "profit-taking": 0.5,
}

GOLD_BULL = {**_MACRO_BULL, "safe haven": 0.9, "safe-haven": 0.9,
             "central bank buying": 0.8, "central banks": 0.5, "inflation": 0.6,
             "inflation hedge": 0.8, "geopolitical": 0.7, "war": 0.7,
             "escalation": 0.7, "risk-off": 0.7, "recession": 0.6,
             "stagflation": 0.8, "falling yields": 0.7, "lower real yields": 0.8,
             "record high": 0.5, "haven demand": 0.8}
GOLD_BEAR = {**_MACRO_BEAR, "rate hike": 0.9, "higher yields": 0.8,
             "rising real yields": 0.9, "risk-on": 0.6, "ceasefire": 0.5,
             "de-escalation": 0.5, "peace": 0.5, "etf outflows": 0.6}

SILVER_BULL = {**GOLD_BULL, "industrial demand": 0.7, "solar": 0.7,
               "photovoltaic": 0.7, "ev demand": 0.6, "electrification": 0.5,
               "silver squeeze": 1.0, "structural deficit": 0.9}
SILVER_BEAR = {**_MACRO_BEAR, "rate hike": 0.8, "rising real yields": 0.8,
               "recession": 0.5, "demand slump": 0.7, "de-escalation": 0.4}

ALU_BULL = {**_MACRO_BULL, "energy costs": 0.8, "power prices": 0.7,
            "electricity prices": 0.7, "production cap": 0.8, "supply cut": 0.8,
            "china stimulus": 0.8, "stockpiling": 0.6, "sanctions": 0.7,
            "cbam": 0.5, "low-carbon premium": 0.5, "strong demand": 0.6,
            "gulf disruption": 0.8, "strait of hormuz": 0.6}
ALU_BEAR = {**_MACRO_BEAR, "recession": 0.8, "slowdown": 0.7, "weak demand": 0.8,
            "china overproduction": 0.8, "rising output": 0.7,
            "indonesia supply": 0.6, "property crisis": 0.7, "demand slump": 0.8,
            "de-escalation": 0.5}

GOLD_SCENARIOS = {
    "mideast_war": {"label": "Middle East war / risk-off", "score": 0.5,
                    "vol_mult": 1.4, "why": "Gold is the classic safe haven — war and risk-off lift it."},
    "fed_hawkish_surprise": {"label": "Hawkish Fed surprise (rate hike)", "score": -0.5,
                             "vol_mult": 1.4, "why": "Higher real yields raise the opportunity cost of holding non-yielding gold."},
    "fed_dovish_surprise": {"label": "Dovish Fed surprise (cuts)", "score": 0.45,
                            "vol_mult": 1.35, "why": "Lower yields + weaker dollar are strongly bullish for gold."},
    "inflation_surprise": {"label": "Hot inflation print", "score": 0.4, "vol_mult": 1.3,
                           "why": "Gold is bought as an inflation hedge, especially with stagflation fears."},
    "central_bank_buying": {"label": "Central-bank buying wave", "score": 0.4, "vol_mult": 1.2,
                            "why": "Sustained official-sector demand (e.g. PBoC) is a structural bid."},
    "dollar_surge": {"label": "US dollar surge", "score": -0.45, "vol_mult": 1.3,
                     "why": "Gold is priced in dollars and moves inversely to it."},
}

SILVER_SCENARIOS = {
    "mideast_war": {"label": "Middle East war / risk-off", "score": 0.35, "vol_mult": 1.4,
                    "why": "Safe-haven bid, partly offset by silver's industrial side."},
    "fed_dovish_surprise": {"label": "Dovish Fed surprise (cuts)", "score": 0.5, "vol_mult": 1.4,
                            "why": "Silver is high-beta to gold — it amplifies precious-metal rallies."},
    "fed_hawkish_surprise": {"label": "Hawkish Fed surprise", "score": -0.55, "vol_mult": 1.45,
                             "why": "Silver falls harder than gold when yields rise."},
    "industrial_boom_solar": {"label": "Solar / EV demand surge", "score": 0.5, "vol_mult": 1.3,
                              "why": "Half of silver demand is industrial — solar and EVs are big drivers."},
    "silver_squeeze": {"label": "Silver squeeze / deficit panic", "score": 0.7, "vol_mult": 1.6,
                       "why": "A thin physical market + structural deficit can spike prices fast."},
    "recession_scare": {"label": "Recession scare", "score": -0.4, "vol_mult": 1.4,
                        "why": "Industrial demand fears outweigh the safe-haven bid for silver."},
}

ALU_SCENARIOS = {
    "energy_price_spike": {"label": "Energy / power price spike", "score": 0.55, "vol_mult": 1.4,
                           "why": "Smelting is hugely power-intensive — energy costs flow straight into aluminium."},
    "china_stimulus": {"label": "China stimulus", "score": 0.5, "vol_mult": 1.3,
                       "why": "China is the dominant producer and consumer; stimulus lifts demand."},
    "china_overproduction": {"label": "China overproduction / export flood", "score": -0.55, "vol_mult": 1.4,
                             "why": "Rising Chinese (and Indonesian) output is the main bearish risk."},
    "russia_sanctions": {"label": "Russia/Rusal sanctions", "score": 0.5, "vol_mult": 1.5,
                         "why": "Rusal is a top global smelter; sanctions tighten supply sharply."},
    "gulf_supply_disruption": {"label": "Gulf supply disruption", "score": 0.45, "vol_mult": 1.4,
                               "why": "The Persian Gulf is ~9% of global primary aluminium output."},
    "recession_scare": {"label": "Global recession scare", "score": -0.5, "vol_mult": 1.45,
                        "why": "Aluminium is a pure industrial metal — it falls on demand-destruction fears."},
}

ASSETS = {
    "copper": {
        "name": "Copper", "ticker": "HG=F", "unit": "$/lb", "default_price": 6.4285,
        "contract_size": 2500, "contract_label": "micro (2,500 lbs)",
        "spread": 0.0010, "commission_rt": 2.50, "news_query": "copper",
        "bullish": cf.BULLISH_TERMS, "bearish": cf.BEARISH_TERMS,
        "scenarios": cf.SCENARIOS,
        "drivers_up": ["Mine outages/strikes (Chile, Peru), smelter cuts",
                       "China stimulus, strong factory data",
                       "Weaker US dollar / Fed rate cuts",
                       "AI data-center + grid/EV demand, low inventories",
                       "US copper-import tariffs (stockpiling)"],
        "drivers_down": ["Recession / weak global PMIs ('Dr. Copper')",
                         "China property weakness, soft demand",
                         "Stronger dollar / hawkish Fed",
                         "Rising inventories, supply surplus",
                         "Mine restarts, substitution to aluminium"],
        "blurb": "Industrial bellwether — tracks global growth, China, and supply shocks.",
    },
    "gold": {
        "name": "Gold", "ticker": "GC=F", "unit": "$/oz", "default_price": 4300.0,
        "contract_size": 10, "contract_label": "micro (10 oz)",
        "spread": 0.20, "commission_rt": 2.50, "news_query": "gold",
        "bullish": {**cf.EXTRA_BULL, **GOLD_BULL}, "bearish": {**cf.EXTRA_BEAR, **GOLD_BEAR}, "scenarios": GOLD_SCENARIOS,
        "drivers_up": ["Falling real yields / Fed rate cuts",
                       "Safe-haven demand (war, risk-off, crises)",
                       "Inflation / stagflation fears",
                       "Central-bank buying (PBoC, etc.)",
                       "Weaker US dollar"],
        "drivers_down": ["Rising real yields / rate hikes (hawkish Fed)",
                         "Stronger US dollar",
                         "Risk-on / equity rallies, peace deals",
                         "ETF outflows, profit-taking",
                         "Easing inflation"],
        "blurb": "The safe haven — driven by real yields, the dollar, inflation, and fear.",
    },
    "silver": {
        "name": "Silver", "ticker": "SI=F", "unit": "$/oz", "default_price": 70.0,
        "contract_size": 1000, "contract_label": "micro (1,000 oz)",
        "spread": 0.02, "commission_rt": 2.50, "news_query": "silver",
        "bullish": {**cf.EXTRA_BULL, **SILVER_BULL}, "bearish": {**cf.EXTRA_BEAR, **SILVER_BEAR}, "scenarios": SILVER_SCENARIOS,
        "drivers_up": ["Precious-metal rallies (high-beta to gold)",
                       "Solar / EV / industrial demand",
                       "Structural supply deficits",
                       "Falling real yields / weaker dollar",
                       "Squeeze dynamics in a thin physical market"],
        "drivers_down": ["Rising real yields / hawkish Fed",
                         "Stronger dollar",
                         "Recession / weak industrial demand",
                         "Profit-taking after sharp rallies",
                         "Gold weakness dragging it down"],
        "blurb": "Half precious, half industrial — gold's volatile cousin.",
    },
    "aluminium": {
        "name": "Aluminium", "ticker": "ALI=F", "unit": "$/tonne", "default_price": 3450.0,
        "contract_size": 25, "contract_label": "full (25 tonnes)",
        "spread": 1.5, "commission_rt": 5.00, "news_query": "aluminum",
        "bullish": {**cf.EXTRA_BULL, **ALU_BULL}, "bearish": {**cf.EXTRA_BEAR, **ALU_BEAR}, "scenarios": ALU_SCENARIOS,
        "drivers_up": ["Energy / power price spikes (smelting is power-hungry)",
                       "China stimulus, supply caps, smelter cuts",
                       "Sanctions (e.g. Rusal), Gulf supply disruption",
                       "Weaker dollar, low inventories",
                       "CBAM / low-carbon premiums"],
        "drivers_down": ["Recession / weak global manufacturing",
                         "China & Indonesia overproduction / export flood",
                         "China property weakness",
                         "Stronger dollar / hawkish Fed",
                         "Rising warehouse stocks"],
        "blurb": "Energy-intensive industrial metal — driven by power costs, China, and supply.",
    },
}


# =============================================================================
# EQUITY (any stock ticker) — generic factor + technical profile
# =============================================================================

EQUITY_BULL = {
    "beat": 0.8, "tops estimates": 0.9, "earnings beat": 0.9, "raises guidance": 0.9,
    "guidance raise": 0.9, "upgrade": 0.8, "price target raised": 0.7, "buyback": 0.7,
    "record revenue": 0.7, "strong demand": 0.6, "outperform": 0.6, "accelerating": 0.6,
    "rate cut": 0.5, "soft landing": 0.5, "new contract": 0.6, "approval": 0.6,
    "partnership": 0.5, "expansion": 0.5, "all-time high": 0.5, "dividend increase": 0.6,
}
EQUITY_BEAR = {
    "miss": 0.8, "misses estimates": 0.9, "earnings miss": 0.9, "cuts guidance": 0.9,
    "guidance cut": 0.9, "downgrade": 0.8, "price target cut": 0.7, "lawsuit": 0.7,
    "sec probe": 0.8, "investigation": 0.7, "recall": 0.7, "layoffs": 0.5, "weak demand": 0.7,
    "underperform": 0.6, "slowing": 0.6, "rate hike": 0.5, "recession": 0.6, "selloff": 0.5,
    "sell-off": 0.5, "profit warning": 0.9, "dilution": 0.7, "bankruptcy": 1.0, "fraud": 1.0,
}

EQUITY_SCENARIOS = {
    "earnings_beat": {"label": "Earnings beat + raised guidance", "score": 0.6, "vol_mult": 1.5,
                      "why": "A beat-and-raise is the strongest routine bullish catalyst for a stock."},
    "earnings_miss": {"label": "Earnings miss / guide-down", "score": -0.6, "vol_mult": 1.5,
                      "why": "A miss or cut guidance is the strongest routine bearish catalyst."},
    "fed_dovish": {"label": "Dovish Fed / rate cuts", "score": 0.4, "vol_mult": 1.25,
                   "why": "Lower rates lift valuations, especially for growth names."},
    "fed_hawkish": {"label": "Hawkish Fed / rate hikes", "score": -0.4, "vol_mult": 1.3,
                    "why": "Higher rates compress valuations and hit risk appetite."},
    "risk_off": {"label": "Market-wide risk-off", "score": -0.5, "vol_mult": 1.4,
                 "why": "When the whole market sells off, high-beta stocks fall hardest."},
    "sector_tailwind": {"label": "Sector tailwind / rotation in", "score": 0.45, "vol_mult": 1.3,
                        "why": "Money rotating into the sector lifts most names in it."},
}


def stock_profile(ticker: str) -> dict:
    t = (ticker or "").upper().strip()
    return {
        "name": t, "ticker": t, "unit": "$/share", "default_price": 0.0,  # 0 -> auto-fetch
        "contract_size": 100, "contract_label": "100 shares",
        "spread": 0.01, "commission_rt": 0.0, "cost_bps": 5.0,
        "news_query": t, "bullish": EQUITY_BULL, "bearish": EQUITY_BEAR,
        "scenarios": EQUITY_SCENARIOS,
        "ref_ticker": "SPY", "ref_label": "S&P 500", "kind": "stock",
        "state_key": f"stock_{t}",
        "drivers_up": ["Earnings beats / raised guidance",
                       "Analyst upgrades, higher price targets",
                       "Buybacks, dividend hikes, new contracts",
                       "Sector tailwinds, falling rates",
                       "Broad market strength (positive beta)"],
        "drivers_down": ["Earnings misses / cut guidance",
                         "Downgrades, lowered targets",
                         "Lawsuits, probes, recalls, dilution",
                         "Rising rates, sector rotation out",
                         "Market-wide risk-off (high-beta names fall hardest)"],
        "blurb": f"{t} — equity. Transparent factor + technical + news screen.",
    }


# attach shared defaults to the metal profiles
for _k, _p in ASSETS.items():
    _p.setdefault("kind", "metal")
    _p.setdefault("ref_ticker", "DX=F")
    _p.setdefault("ref_label", "US dollar")
    _p.setdefault("cost_bps", 0.0)
    _p.setdefault("state_key", _k)


def get(asset_key: str) -> dict:
    return ASSETS.get(asset_key, ASSETS["copper"])


def resolve(asset_key: str = None, stock_ticker: str = None) -> dict:
    """Return a profile for either a known metal (asset_key) or any stock
    ticker. Stocks get a generated profile with their own learning state."""
    if stock_ticker:
        return stock_profile(stock_ticker)
    return dict(get(asset_key or "copper"))
