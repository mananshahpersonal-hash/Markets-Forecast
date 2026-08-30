# ALL holdings transcribed from BOTH screenshot batches (30 positions).
# (ticker, shares, avg_cost, market_value_shown)
H = [
 # batch 1 (20)
 ("VGT",   1,        120.64,    120.13),
 ("VOOG",  1,         84.41,     84.20),
 ("VOO",   1,        708.75,    707.34),
 ("SPY",   1,        770.80,    769.33),
 ("QQQ",   1,        717.89,    716.25),
 ("ITOT",  1,        168.85,    168.44),
 ("BRK.B", 1,        505.76,    505.00),
 ("COPX",  1,         78.87,     94.24),
 ("NUKZ",  1,         71.16,     65.35),
 ("EWZ",   1,         40.20,     35.53),
 ("GCOW",  1,         45.79,     47.35),
 ("IBIT",  100,       45.64,   4406.00),
 ("ETHA",  100,       19.84,   1842.90),
 ("BTCI",  200,       30.71,   6446.00),
 ("BNDI",  200,       46.40,   9274.00),
 ("HYBI",  200,       49.31,   9799.90),
 ("SPHD",  200,       52.38,  10656.00),
 ("CSHI",  200,       49.75,   9953.82),
 ("IWMI",  200,       51.89,  10420.00),
 ("QQQH",  100,       53.54,   5462.00),
 # batch 2 (10)
 ("JEPI",  200,       57.74,  11575.00),
 ("IAUI",  800.00998, 54.87,  41363.32),
 ("QQQI",  2500.014162,48.30, 136325.77),
 ("SPYI",  2500,      46.80, 134349.75),
 ("QDTE",  1700.813449,34.52, 49425.64),
 ("VXUS",  100,       79.66,   8769.65),
 ("VZ",    200,       46.40,  10000.00),
 ("T",     200,       23.46,   5196.00),
 ("PFE",   200,       26.64,   5592.00),
 ("MCD",   100,      272.45,  26513.00),
]
# QA 1: no duplicate tickers
seen={}; dups=[t for t,*_ in H if (t in seen) or seen.setdefault(t,1) and False]
from collections import Counter
c=Counter(t for t,*_ in H); dups=[t for t,n in c.items() if n>1]
print("duplicate tickers:", dups or "none")
# QA 2: implied price = value/shares is sane, and total return sign = (val - shares*avgcost)
print(f"{'TICKER':<7}{'SHARES':>13}{'AVGCOST':>10}{'MKTVAL':>13}{'IMPL_PX':>10}{'TOT_RET':>12}")
total_val=0; total_cost=0
for t,sh,ac,mv in H:
    px=mv/sh; totret=mv-sh*ac; total_val+=mv; total_cost+=sh*ac
    print(f"{t:<7}{sh:>13}{ac:>10.2f}{mv:>13.2f}{px:>10.2f}{totret:>12.2f}")
print("-"*65)
print(f"{'TOTAL':<7}{'':>13}{'':>10}{total_val:>13.2f}")
print(f"\nPositions: {len(H)}")
print(f"Total market value:  ${total_val:,.2f}")
print(f"Total cost basis:    ${total_cost:,.2f}")
print(f"Total unrealized P/L: ${total_val-total_cost:,.2f}  ({(total_val-total_cost)/total_cost*100:+.2f}%)")
