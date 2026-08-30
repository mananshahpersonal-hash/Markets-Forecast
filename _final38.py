# The 30 already confirmed + 8 new from this batch. (ticker, shares, avg_cost)
CONFIRMED_30 = [
 ("QQQI",2500.014162,48.30),("SPYI",2500,46.80),("QDTE",1700.813449,34.52),
 ("IAUI",800.00998,54.87),("MCD",100,272.45),("JEPI",200,57.74),("SPHD",200,52.38),
 ("IWMI",200,51.89),("VZ",200,46.40),("CSHI",200,49.75),("HYBI",200,49.31),("BNDI",200,46.40),
 ("VXUS",100,79.66),("BTCI",200,30.71),("QQQH",100,53.54),("PFE",200,26.64),("T",200,23.46),
 ("IBIT",100,45.64),("ETHA",100,19.84),("VOO",1,708.75),("SPY",1,770.80),("QQQ",1,717.89),
 ("VGT",1,120.64),("VOOG",1,84.41),("ITOT",1,168.85),("BRK.B",1,505.76),("COPX",1,78.87),
 ("NUKZ",1,71.16),("EWZ",1,40.20),("GCOW",1,45.79),
]
# NEW from this batch (with the previously-missing avg costs!):
NEW_8 = [
 ("NFLX", 6000, 116.05),   # mkt val 489,720 @ $81.62; total return -206,556.22
 ("AAPL", 1000, 166.42),   # mkt val 320,126 @ $320.13; +153,703.55
 ("AMZN", 300,  191.26),   # mkt val 79,866  @ $266.22; +22,486.95
 ("NVDA", 300,  108.40),   # mkt val 65,357.58 @ $217.86; +32,837.73
 ("WMT",  300,  109.07),   # NOTE: this screen shows 300 shares (not 100!) mkt val 30,924 @ $103.08
 ("SCHD", 200,  32.46),    # mkt val 6,983.98 @ $34.92; +492.66
 ("JEPQ", 200,  58.59),    # mkt val 12,036 @ $60.18; +318.19
 ("JEPQ_DUP", 0, 0),       # placeholder to check count logic (removed below)
]
NEW_8 = [x for x in NEW_8 if x[0] != "JEPQ_DUP"]  # only 7 here — need 8th!
ALL = CONFIRMED_30 + NEW_8
from collections import Counter
c = Counter(t for t,*_ in ALL)
dups = [t for t,n in c.items() if n>1]
print("total rows:", len(ALL))
print("unique tickers:", len(c))
print("duplicates:", dups or "none")
print("\nNEW names added:", [t for t,*_ in NEW_8])
# verify each NEW against its screenshot market value & total return
CHK = {"NFLX":(81.62,489720,-206556.22),"AAPL":(320.13,320126,153703.55),
 "AMZN":(266.22,79866,22486.95),"NVDA":(217.86,65357.58,32837.73),
 "WMT":(103.08,30924,-1796.49),"SCHD":(34.92,6983.98,492.66),"JEPQ":(60.18,12036,318.19)}
print("\nQA each new position (recomputed vs screenshot):")
for t,sh,ac in NEW_8:
    px,mv,tr = CHK[t]
    my_mv=sh*px; my_tr=sh*(px-ac)
    print(f"  {t:<6} {sh}sh @${ac}: val ${my_mv:,.0f} (scr ${mv:,.0f}) | ret ${my_tr:,.0f} (scr ${tr:,.0f})  {'OK' if abs(my_mv-mv)<2 else 'CHECK'}")
