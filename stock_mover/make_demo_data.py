"""Builds a fictional demo pack ("Demo Threads": 9 stores, 2 warehouses, 1 outlet) in data/demo.

Every sheet StockFlow understands is included, so the demo shows every feature: colours, two warehouses,
an outlet, freight costs per lane, stock in alteration/QC, season end dates, a festive calendar.
No real brand, store or person is in it.   Run:  python make_demo_data.py
"""
from pathlib import Path

import numpy as np
import pandas as pd

rng = np.random.default_rng(7)
OUT = Path(__file__).resolve().parent / "data" / "demo"
OUT.mkdir(parents=True, exist_ok=True)
TODAY = pd.Timestamp("2026-10-31")
START = TODAY - pd.Timedelta(days=364)

stores = pd.DataFrame([
    ("JAI-01", "Jaipur", "Rajasthan", "North", "A", 2100, "2018-03-01", "store", "WH-JAI", "AM-01"),
    ("JAI-02", "Jaipur", "Rajasthan", "North", "B", 1200, "2021-07-01", "store", "WH-JAI", "AM-01"),
    ("UDR-01", "Udaipur", "Rajasthan", "North", "B", 1140, "2022-02-01", "store", "WH-JAI", "AM-01"),
    ("IND-01", "Indore", "Madhya Pradesh", "Central", "A", 1950, "2019-09-01", "store", "WH-IND", "AM-02"),
    ("IND-02", "Indore", "Madhya Pradesh", "Central", "B", 1140, "2023-01-15", "store", "WH-IND", "AM-02"),
    ("BHO-01", "Bhopal", "Madhya Pradesh", "Central", "A", 1800, "2020-11-01", "store", "WH-IND", "AM-02"),
    ("NAG-01", "Nagpur", "Maharashtra", "Central", "B", 1260, "2024-04-01", "store", "WH-IND", "AM-03"),
    ("PUN-01", "Pune", "Maharashtra", "West", "A", 2040, "2026-09-20", "store", "WH-IND", "AM-03"),
    ("JAI-OUT", "Jaipur", "Rajasthan", "North", "Outlet", 1500, "2020-01-01", "outlet", "WH-JAI", "AM-01"),
], columns=["store_id", "city", "region", "zone", "tier", "capacity_units", "opened_on", "store_type", "home_warehouse", "manager_id"])
warehouses = pd.DataFrame([("WH-JAI", "Jaipur", "North"), ("WH-IND", "Indore", "Central")], columns=["warehouse_id", "city", "zone"])

cats = {  # category: (sizes, mrp range, share of demand, styles, colours per style)
    "Kurta Set": (["S", "M", "L", "XL", "XXL"], (2499, 6999), .40, 9, 2),
    "Sherwani": (["38", "40", "42", "44", "46"], (14999, 32999), .12, 5, 1),
    "Nehru Jacket": (["S", "M", "L", "XL", "XXL"], (2999, 5999), .18, 5, 2),
    "Lehenga": (["S", "M", "L", "XL"], (9999, 24999), .15, 5, 2),
    "Juttis": (["6", "7", "8", "9", "10"], (1499, 2999), .10, 3, 1),
    "Dupatta": (["FS"], (899, 1999), .05, 3, 2),
}
size_w = {5: [.12, .26, .30, .20, .12], 4: [.2, .35, .3, .15], 1: [1.0]}
colours = ["Maroon", "Ivory", "Teal", "Mustard", "Navy", "Peach", "Emerald", "Rose"]
rows, styles = [], []
for cat, (sizes, (lo, hi), share, n, ncol) in cats.items():
    for i in range(1, n + 1):
        sid = f"{cat[:3].upper()}-{i:02d}"
        mrp = int(rng.integers(lo, hi) // 100 * 100 + 99)
        launch = TODAY - pd.Timedelta(days=int(rng.choice([30, 120, 240, 400, 600])))
        ends = {"KUR-09": -20, "LEH-05": -10, "NEH-05": 30}
        season_end = TODAY + pd.Timedelta(days=ends[sid]) if sid in ends else pd.NaT
        pop = rng.lognormal(0, .6)
        for c in rng.choice(colours, ncol, replace=False):
            for sz, w in zip(sizes, size_w[len(sizes)]):
                sku = f"{sid}-{c[:3].upper()}-{sz}"
                rows.append(dict(sku_id=sku, style_id=sid, style_name=f"{c} {cat} {i}", category=cat, size=sz, colour=c, fit="Regular",
                                 mrp=mrp, cost_price=int(mrp * rng.uniform(.32, .45)), launch_date=launch.date(),
                                 season_end=season_end.date() if pd.notna(season_end) else "", is_core_style=int(i <= n // 2),
                                 is_non_product=0, _w=share * pop * w / n / ncol))
rows.append(dict(sku_id="BAG-01", style_id="BAG", style_name="Carry bag", category="Packaging", size="FS", colour="", fit="", mrp=0,
                 cost_price=5, launch_date="2020-01-01", season_end="", is_core_style=1, is_non_product=1, _w=0))
items = pd.DataFrame(rows)

# calendar: Diwali-ish festival + peak days
days = pd.date_range(START, TODAY + pd.Timedelta(days=120))
cal = []
for reg in stores.region.unique():
    for d in days:
        ev = "Diwali" if d in (pd.Timestamp("2025-10-21"), pd.Timestamp("2026-11-08")) else "Holi" if d == pd.Timestamp("2026-03-04") else ""
        peak = int((d.month in (11, 12, 1, 2) and rng.random() < .22) or (d.month in (4, 5) and rng.random() < .1))
        cal.append((d.date(), reg, ev, peak))
calendar = pd.DataFrame(cal, columns=["date", "region", "event", "is_peak_day"])
lift = calendar.set_index(["region", "date"])

# sales: demand x store strength x season, censored by a simulated in-stock flag
sell = items[items.is_non_product == 0].reset_index(drop=True)
strength = {"A": 1.0, "B": .6, "Outlet": .5}
store_rate = {s: strength[t] * rng.uniform(.8, 1.2) for s, t in zip(stores.store_id, stores.tier)}
store_rate["PUN-01"] *= .9
sales, weekly = [], []
instock = {}
for s in stores.store_id:
    for k in sell.sku_id:
        instock[(s, k)] = rng.random() < .85
for d in pd.date_range(START, TODAY):
    for _, st in stores.iterrows():
        if pd.Timestamp(st.opened_on) > d:
            continue
        L = lift.loc[(st.region, d.date())]
        m = 1 + 1.6 * L.is_peak_day + (0.9 if L.event else 0)
        lam = sell._w.values * 26.0 * store_rate[st.store_id] * m
        if st.store_type == "outlet":
            lam = lam * (pd.to_datetime(sell.season_end, errors="coerce").lt(d).values * 3 + .3)
        q = rng.poisson(lam)
        for j in np.nonzero(q)[0]:
            k = sell.sku_id[j]
            if not instock[(st.store_id, k)]:
                continue
            net = sell.mrp[j] * q[j] * (1 - rng.choice([0, 0, 0, .1, .2]))
            sales.append((d.date(), st.store_id, k, int(q[j]), round(net)))
    if d.dayofweek == 5:  # week end (Saturday): flip some in-stock flags
        for key in instock:
            if rng.random() < .08:
                instock[key] = not instock[key]
sales = pd.DataFrame(sales, columns=["date", "store_id", "sku_id", "qty", "net_value"])
# weekly in-stock history (last 12 weeks): days_in_stock from flags with noise
weeks = pd.date_range(end=TODAY, periods=12, freq="7D")
wk = []
for (s, k), on in instock.items():
    for w in weeks:
        dis = 7 if on else int(rng.choice([0, 0, 1, 2]))
        wk.append((w.date(), s, k, dis))
stock_weekly = pd.DataFrame(wk, columns=["week_ending", "store_id", "sku_id", "days_in_stock"])

# snapshot: stores hold ~3-6 weeks of demand (0 when out of stock), some old stock, some alteration/QC
snap = []
for s, st in stores.set_index("store_id").iterrows():
    for j, k in enumerate(sell.sku_id):
        rate = sell._w[j] * 26.0 * store_rate[s]
        oh = int(rng.poisson(rate * rng.uniform(15, 60))) if instock[(s, k)] else 0
        if st.store_type == "outlet":
            oh = int(rng.poisson(1.5))
        if oh and rng.random() < .15:
            oh += int(rng.integers(2, 6))       # overstock
        age = int(rng.choice([10, 30, 60, 90, 150, 220, 400], p=[.2, .2, .2, .15, .12, .08, .05]))
        if oh:
            snap.append((TODAY.date(), s, k, oh, 0, (TODAY - pd.Timedelta(days=age)).date(), "sellable"))
        if rng.random() < .01:
            snap.append((TODAY.date(), s, k, int(rng.integers(1, 3)), 0, (TODAY - pd.Timedelta(days=40)).date(),
                         rng.choice(["alteration", "damaged", "return_qc"])))
for w in warehouses.warehouse_id:
    for j, k in enumerate(sell.sku_id):
        oh = int(rng.poisson(sell._w[j] * 26 * 3 * rng.uniform(25, 90)))
        if oh:
            snap.append((TODAY.date(), w, k, oh, 0, (TODAY - pd.Timedelta(days=60)).date(), "sellable"))
stock_snapshot = pd.DataFrame(snap, columns=["as_of_date", "location_id", "sku_id", "on_hand", "in_transit", "oldest_stock_received_on", "stock_status"])
inbound = pd.DataFrame([(TODAY.date() + pd.Timedelta(days=int(rng.choice([1, 1, 5, 12]))), k, int(rng.integers(10, 40)), rng.choice(warehouses.warehouse_id))
                        for k in rng.choice(sell.sku_id, 40, replace=False)], columns=["expected_date", "sku_id", "qty", "warehouse_id"])
lanes = pd.DataFrame([
    ("WH-JAI", "zone:North", 2, 30, 700), ("WH-JAI", "zone:Central", 4, 55, 1400), ("WH-JAI", "zone:West", 5, 70, 1800),
    ("WH-IND", "zone:Central", 2, 30, 700), ("WH-IND", "zone:West", 3, 45, 1100), ("WH-IND", "zone:North", 4, 55, 1400),
    ("city:Jaipur", "city:Jaipur", 1, 15, 300), ("city:Indore", "city:Indore", 1, 15, 300),
    ("zone:North", "zone:North", 2, 35, 900), ("zone:Central", "zone:Central", 2, 35, 900),
    ("*", "*", 5, 90, 2500),
], columns=["from", "to", "lead_days", "cost_per_unit", "cost_per_trip"])
min_display = pd.DataFrame([(t, c, n) for t, f in (("A", 1.0), ("B", .6)) for c, n in
                            (("Kurta Set", 60), ("Sherwani", 12), ("Nehru Jacket", 25), ("Lehenga", 15), ("Juttis", 12), ("Dupatta", 6))
                            for n in [int(n * f)]], columns=["tier", "category", "min_display_units"])
protected = pd.DataFrame([("JAI-01", "KUR-01"), ("JAI-01", "SHE-01"), ("IND-01", "KUR-02")], columns=["store_id", "style_id"])
managers = pd.DataFrame([("AM-01", "R. Mehta", "JAI-01, JAI-02, UDR-01, JAI-OUT", 5), ("AM-02", "S. Khan", "IND-01, IND-02, BHO-01", 5),
                         ("AM-03", "P. Kulkarni", "NAG-01, PUN-01", 5)], columns=["manager_id", "name", "stores", "max_alerts_per_week"])

for name, df in dict(stores=stores, warehouses=warehouses, items=items.drop(columns="_w"), sales=sales, stock_snapshot=stock_snapshot,
                     stock_weekly=stock_weekly, inbound=inbound, lanes=lanes, calendar=calendar, min_display=min_display,
                     protected_styles=protected, managers=managers).items():
    df.to_csv(OUT / f"{name}.csv", index=False)
print("demo pack written to", OUT, "·", len(sales), "sales lines,", len(items), "SKUs")
