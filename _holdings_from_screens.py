# Transcribed EXACTLY from the 20 Robinhood screenshots (shares, avg cost, market value)
# Format: ticker, shares, avg_cost, market_value_shown  (I verify shares*price≈value)
H = [
 ("VGT",   1,    120.64, 120.13),
 ("VOOG",  1,     84.41,  84.20),
 ("VOO",   1,    708.75, 707.34),
 ("SPY",   1,    770.80, 769.33),
 ("QQQ",   1,    717.89, 716.25),
 ("ITOT",  1,    168.85, 168.44),
 ("BRK.B", 1,    505.76, 505.00),
 ("COPX",  1,     78.87,  94.24),
 ("NUKZ",  1,     71.16,  65.35),
 ("EWZ",   1,     40.20,  35.53),
 ("GCOW",  1,     45.79,  47.35),
 ("IBIT",  100,   45.64, 4406.00),
 ("ETHA",  100,   19.84, 1842.90),
 ("BTCI",  200,   30.71, 6446.00),
 ("BNDI",  200,   46.40, 9274.00),
 ("HYBI",  200,   49.31, 9799.90),
 ("SPHD",  200,   52.38, 10656.00),
 ("CSHI",  200,   49.75, 9953.82),
 ("IWMI",  200,   51.89, 10420.00),
 ("QQQH",  100,   53.54, 5462.00),
]
print(f"{'TICKER':<7}{'SHARES':>8}{'AVGCOST':>10}{'MKTVAL':>12}{'IMPLIED_PX':>12}  check")
bad=0
for t,sh,ac,mv in H:
    px = mv/sh
    # sanity: implied price should be positive and value = shares*price
    ok = abs(sh*px - mv) < 0.01
    if not ok: bad+=1
    print(f"{t:<7}{sh:>8}{ac:>10.2f}{mv:>12.2f}{px:>12.2f}  {'OK' if ok else 'BAD'}")
print("\nrows:", len(H), "| arithmetic errors:", bad)
tot = sum(mv for _,_,_,mv in H)
print(f"sum of these 20 market values: ${tot:,.2f}")
