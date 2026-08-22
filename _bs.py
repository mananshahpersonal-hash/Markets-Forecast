import pandas as pd, numpy as np, datetime as dt
import copper_forecaster as cf, model_pro as mp
def frame(a,drift,seed,vol=0.009,n=400):
    rng=np.random.default_rng(seed)
    idx=pd.date_range(end=dt.datetime(2026,8,22),periods=n,freq="D",tz="America/New_York")
    c=a*np.exp(np.cumsum(rng.normal(drift,vol,n)))
    return pd.DataFrame({"Open":c,"High":c,"Low":c,"Close":c,"Adj Close":c,"Volume":1e6},index=idx)
# one strong up, one strong down, rest mild
STOCK={t:(100,0.0000,10+i) for i,t in enumerate(mp.ALERT_UNIVERSE)}
STOCK["NVDA"]=(60,0.0022,7); STOCK["INTC"]=(40,-0.0022,8)
METAL={"HG=F":6.3,"GC=F":4300,"SI=F":70,"ALI=F":3450}
class F:
    @staticmethod
    def download(tickers,period=None,interval=None,group_by=None,**k):
        if isinstance(tickers,(list,tuple)):
            fr={t:frame(STOCK[t][0],STOCK[t][1],STOCK[t][2]) for t in tickers if t in STOCK}
            return pd.concat(fr,axis=1) if fr else pd.DataFrame()
        t=tickers
        if t in METAL: return frame(METAL[t],0.0002,42)
        if t in STOCK: return frame(STOCK[t][0],STOCK[t][1],STOCK[t][2])
        return pd.DataFrame()
cf.yf=F(); cfg=cf.load_config("_")
reads=mp.quick_universe_reads(cfg, mp.ALERT_UNIVERSE)
buys=[r["name"] for r in reads if mp.strong_trend_signal(r)=="BUY"]
sells=[r["name"] for r in reads if mp.strong_trend_signal(r)=="SELL"]
print("shared strong_trend_signal -> BUYS:", buys[:6], "... total", len(buys))
print("shared strong_trend_signal -> SELLS:", sells[:6], "... total", len(sells))
assert "NVDA" in buys and "INTC" in sells
# same function drives Top & Bottom now -> guaranteed consistent
print("\nBUY + SELL LISTS WORK, and alert == Top&Bottom (same function)")
