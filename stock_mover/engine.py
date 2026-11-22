"""StockFlow engine: a daily, ready-to-approve list of stock moves for a multi-store apparel brand.

Run:  python engine.py --data DIR [--config brands/x.json] [--out DIR]

Pipeline
  1. load + clean     any brand's sheets (see schema.py); personal data dropped; data issues logged
  2. velocity         sales per day *while in stock*, blended with similar stores
  3. season           peak days and event weeks by region, from the brand's own calendar
  4. plan             A warehouse -> store (any number of warehouses), size-set completion, minimum display,
                      B store -> store, C ageing stock by option (style + colour), broken size runs reunited,
                      D capped pull-backs, end-of-season / dead stock to outlets
  5. value + batch    every line valued (margin gained, markdown and holding avoided, freight and handling),
                      lines grouped into trips, trips that cost more than they earn dropped
  6. validate         every rule re-checked on the final plan
  7. write            Excel approval workbook, transfer orders (CSV), moves, issues
"""
import argparse
import heapq
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import poisson

import schema

HERE = Path(__file__).resolve().parent

T_WH, T_DISP, T_S2S, T_AGE, T_PULL, T_OUT = ("Warehouse -> store", "Min display top-up", "Store -> store",
                                             "Ageing stock", "Pull back to warehouse", "Clearance to outlet")
TYPES = [T_WH, T_DISP, T_S2S, T_AGE, T_PULL, T_OUT]
# broad one-line reasons (the detail stays on every line)
G_URGENT, G_PEAK, G_TOPUP, G_SET, G_DISP = ("Refill before it runs out", "Stock up for the peak ahead",
                                            "Top up to normal cover", "Complete broken size sets", "Fill minimum display")
G_AGE, G_REUNITE, G_PULL, G_CLEAR = ("Old stock to faster-selling stores", "Reunite broken size runs",
                                     "Return slow stock to warehouse", "Clear end-of-season / dead stock")
GROUPS = [G_URGENT, G_PEAK, G_SET, G_DISP, G_TOPUP, G_AGE, G_REUNITE, G_PULL, G_CLEAR]
INF = 10 ** 9


class DataError(Exception):
    """The uploaded data is missing something StockFlow needs."""


def days(x):
    return "over a year" if x > 365 else f"{x:.0f} days"


def money(v, cfg=None):
    b = (cfg or schema.DEFAULTS)["brand"]
    c, v = b["currency"], float(v)
    sign, v = ("-" if v < 0 else ""), abs(v)
    if b.get("number_style", "indian") == "indian":
        s = f"{c}{v / 1e7:.2f} Cr" if v >= 1e7 else f"{c}{v / 1e5:.1f} L" if v >= 1e5 else f"{c}{v:,.0f}"
    else:
        s = f"{c}{v / 1e6:.2f}M" if v >= 1e6 else f"{c}{v / 1e3:.1f}K" if v >= 1e4 else f"{c}{v:,.0f}"
    return sign + s


# ============================================================================= 1. load + clean
def read_tables(source):
    """A folder of CSVs (searched recursively), one Excel workbook (one tab per sheet), or both."""
    source = Path(source)
    norm = lambda s: schema.TABLE_ALIASES.get(s.strip().lower().replace(" ", "_"), s.strip().lower().replace(" ", "_"))
    if source.is_file() and source.suffix.lower() in (".xlsx", ".xlsm"):
        tabs = pd.read_excel(source, sheet_name=None, dtype=str, keep_default_na=False)
        return {norm(k): v for k, v in tabs.items()}
    T = {}
    for f in sorted(source.rglob("*")):
        if f.suffix.lower() == ".csv":
            T.setdefault(norm(f.stem), pd.read_csv(f, dtype=str, keep_default_na=False))
        elif f.suffix.lower() in (".xlsx", ".xlsm"):
            for k, v in read_tables(f).items():
                T.setdefault(k, v)
    return T


def _num(df, col, t, default=None, as_int=True):
    if col not in df:
        if default is None:
            raise DataError(f"Sheet '{t}' is missing column '{col}'.")
        df[col] = default
        return
    v = pd.to_numeric(df[col].replace("", np.nan), errors="coerce")
    if default is None and v.isna().any():
        raise DataError(f"Sheet '{t}', column '{col}': {int(v.isna().sum())} values are blank or not numbers "
                        f"(first at row {int(v.isna().values.argmax()) + 2}).")
    v = v.fillna(default if default is not None else 0)
    df[col] = v.round().astype(int) if as_int else v.astype(float)


def _date(df, col):
    if col in df:
        df[col] = pd.to_datetime(df[col].replace("", np.nan), errors="coerce")


def normalise(T, cfg):
    """Rename alias columns, convert legacy layouts, drop personal data, check required columns."""
    notes = []
    for t, v in list(T.items()):
        v.columns = [str(c).strip().lower().replace(" ", "_") for c in v.columns]
        if t in schema.TABLES:
            v.rename(columns={a: b for a, b in schema.TABLES[t][3].items() if a in v and b not in v}, inplace=True)
        drop = [c for c in cfg["privacy"]["drop_columns"] if c in v]
        if drop:
            v.drop(columns=drop, inplace=True)
            notes.append(f"{t}: personal columns not used and dropped on load ({', '.join(drop)})")
        for c in v.columns:
            if v[c].dtype == object:
                v[c] = v[c].astype(str).str.strip().replace({"nan": "", "NaT": "", "None": ""})
    # sales from bills + bill_lines
    if "sales" not in T and {"bills", "bill_lines"} <= set(T):
        b = T["bills"].rename(columns={"bill_date": "date"})
        bl = T["bill_lines"]
        s = bl.merge(b[["bill_id", "date", "store_id"]], on="bill_id", how="inner")
        if "net_value" not in s and "selling_price" in s:
            s["net_value"] = (pd.to_numeric(s.selling_price, errors="coerce").fillna(0)
                              * pd.to_numeric(s.qty, errors="coerce").fillna(0)).astype(str)
        T["sales"] = s
        notes.append("sales built from bills + bill_lines")
    # legacy lead_times sentences -> lanes and store-to-store defaults
    if "lanes" not in T and "lead_times" in T:
        rows = []
        for r in T["lead_times"].itertuples():
            route, d = str(r.route).lower(), int(float(r.days))
            if route.startswith("warehouse"):
                z = route.split(",")[-1].strip().split(" zone")[0].strip()
                rows.append({"from": "type:warehouse", "to": f"zone:{z}", "lead_days": d})
                rows.append({"from": f"zone:{z}", "to": "type:warehouse", "lead_days": d})
            else:
                key = "same_city" if "same city" in route else "same_zone" if "same zone" in route else "other"
                cfg["lanes_default"][key]["lead_days"] = d
        T["lanes"] = pd.DataFrame(rows, columns=["from", "to", "lead_days"]).astype(str)
        notes.append("lead_times converted to lanes (freight costs from settings)")
    problems = []
    for t, (need, _, cols, _) in schema.TABLES.items():
        if t not in T:
            if need == schema.R:
                problems.append(f"Missing sheet '{t}' ({t}.csv or an Excel tab named {t})"
                                + (" — or bills.csv + bill_lines.csv" if t == "sales" else "") + ".")
            continue
        miss = [c for c, cn, *_ in cols if cn == schema.R and c not in T[t]]
        if miss:
            problems.append(f"Sheet '{t}' is missing column(s): {', '.join(miss)}.")
        elif len(T[t]) == 0 and need == schema.R:
            problems.append(f"Sheet '{t}' has no rows.")
    if problems:
        raise DataError("\n".join(problems))
    return notes


def load(source, cfg):
    T = read_tables(source)
    notes = normalise(T, cfg)
    D = {t: T[t].copy() for t in schema.TABLES if t in T}
    st = D["stores"]
    for c, d in (("tier", "—"), ("store_type", "store"), ("home_warehouse", ""), ("manager_id", ""), ("opened_on", "")):
        if c not in st or (c == "tier" and (st[c] == "").all()):
            st[c] = d
    st["tier"] = st.tier.replace("", "—")
    st["store_type"] = st.store_type.str.lower().replace("", "store")
    _num(st, "capacity_units", "stores", default=INF)
    st.loc[st.capacity_units <= 0, "capacity_units"] = INF
    _date(st, "opened_on")
    it = D["items"]
    for c in ("colour", "fit", "launch_date", "season_end", "is_core_style", "is_non_product", "cost_price"):
        if c not in it:
            it[c] = ""
    _num(it, "mrp", "items", default=0, as_int=False)
    it["cost_price"] = pd.to_numeric(it.cost_price.replace("", np.nan), errors="coerce")
    _num(it, "is_non_product", "items", default=0)
    _date(it, "launch_date")
    _date(it, "season_end")
    s = D["sales"]
    _date(s, "date")
    if s.date.isna().all():
        raise DataError("Sheet 'sales': no readable dates in column 'date' (use YYYY-MM-DD).")
    s.dropna(subset=["date"], inplace=True)
    _num(s, "qty", "sales")
    if "net_value" in s:
        _num(s, "net_value", "sales", default=0, as_int=False)
    sn = D["stock_snapshot"]
    for c, d in (("in_transit", 0), ("oldest_stock_received_on", ""), ("stock_status", ""), ("in_transit_arrives_on", "")):
        if c not in sn:
            sn[c] = d
    _date(sn, "as_of_date")
    _num(sn, "on_hand", "stock_snapshot")
    _num(sn, "in_transit", "stock_snapshot", default=0)
    _date(sn, "oldest_stock_received_on")
    if "stock_weekly" in D:
        w = D["stock_weekly"]
        _date(w, "week_ending")
        _num(w, "days_in_stock", "stock_weekly")
        if "closing_on_hand" in w:
            _num(w, "closing_on_hand", "stock_weekly", default=-1)
    if "inbound" in D:
        _date(D["inbound"], "expected_date")
        _num(D["inbound"], "qty", "inbound")
        if "warehouse_id" not in D["inbound"]:
            D["inbound"]["warehouse_id"] = ""
    if "lanes" in D:
        ln = D["lanes"]
        _num(ln, "lead_days", "lanes")
        for c in ("cost_per_unit", "cost_per_trip"):
            ln[c] = pd.to_numeric(ln[c].replace("", np.nan), errors="coerce") if c in ln else np.nan
    if "calendar" in D:
        c = D["calendar"]
        _date(c, "date")
        for col in ("event", "is_peak_day"):
            if col not in c:
                c[col] = ""
        c["is_peak_day"] = pd.to_numeric(c.is_peak_day.replace("", 0), errors="coerce").fillna(0).astype(int)
    if "min_display" in D:
        _num(D["min_display"], "min_display_units", "min_display")
    if "managers" in D and "max_alerts_per_week" not in D["managers"]:
        D["managers"]["max_alerts_per_week"] = "5"
    D["_notes"] = notes
    return D


# ============================================================================= prep
class Lanes:
    """Lead time and cost for any from -> to pair. Most specific lanes.csv row wins; settings fill the gaps."""

    def __init__(self, D, cfg, loc):
        self.cfg, self.loc, self.cache = cfg, loc, {}
        self.rows = D["lanes"].to_dict("records") if "lanes" in D else []

    def _score(self, spec, a):
        spec = str(spec).strip()
        L = self.loc[a]
        if spec == a:
            return 4
        if spec.lower().startswith("city:") and L["city"] and spec[5:].strip().lower() == L["city"].lower():
            return 3
        if spec.lower().startswith("zone:") and L["zone"] and spec[5:].strip().lower() == L["zone"].lower():
            return 2
        if spec.lower().startswith("type:") and spec[5:].strip().lower() == L["type"]:
            return 1
        if spec == "*":
            return 0.5
        return None

    def _match(self, a, b):
        best, top = None, -1
        for r in self.rows:
            sa, sb = self._score(r["from"], a), self._score(r["to"], b)
            if sa is not None and sb is not None and sa + sb > top:
                best, top = r, sa + sb
        return best

    def relation(self, a, b):
        A, B = self.loc[a], self.loc[b]
        if A["type"] == "warehouse":
            return "wh_to_store"
        if B["type"] == "warehouse":
            return "store_to_wh"
        if A["city"] and A["city"] == B["city"]:
            return "same_city"
        if A["zone"] and A["zone"] == B["zone"]:
            return "same_zone"
        return "other"

    def get(self, a, b):
        if (a, b) not in self.cache:
            d = dict(self.cfg["lanes_default"][self.relation(a, b)])
            r = self._match(a, b) or self._match(b, a)
            if r:
                d["lead_days"] = int(r["lead_days"])
                for c in ("cost_per_unit", "cost_per_trip"):
                    if r.get(c) == r.get(c) and r.get(c) is not None:
                        d[c] = float(r[c])
            self.cache[(a, b)] = d
        return self.cache[(a, b)]

    def lead(self, a, b):
        return int(self.get(a, b)["lead_days"])


def prep(D, cfg):
    ex = []
    pc, ec, sc = cfg["planning"], cfg["economics"], cfg["size_sets"]

    def issue(kind, store, sku, detail, action, sev="High", value=0.0):
        ex.append(dict(severity=sev, issue=kind, store=store, sku=sku, detail=detail, action=action, value=float(value)))

    for n in D.get("_notes", []):
        issue("Data note", "", "", n, "No action needed", "Info")

    # ---- items
    items = D["items"].copy()
    junk = (items.is_non_product == 1) | (items.mrp <= 0)
    if junk.any():
        issue("Non-product rows", "", f"{int(junk.sum())} SKUs", "Bags, covers or zero-price rows", "Left out of planning", "Info")
    items = items[~junk].drop_duplicates("sku_id").set_index("sku_id")
    groups = {c: grp for grp, cats in pc["category_groups"].items() for c in cats}
    items["disp_cat"] = items.category.map(lambda c: groups.get(c, c))
    items["option"] = items.style_id + np.where(items.colour != "", " · " + items.colour, "") + \
        np.where(items.fit != "", " · " + items.fit, "")
    no_cost = items.cost_price.isna()
    if no_cost.any():
        issue("Cost price missing", "", f"{int(no_cost.sum())} SKUs", "No cost_price given",
              f"Assumed {ec['default_cost_ratio']:.0%} of MRP; margins are estimates", "Medium" if no_cost.mean() > .2 else "Info")
    items["cost"] = items.cost_price.fillna(items.mrp * ec["default_cost_ratio"])

    # ---- stores and warehouses
    stores = D["stores"].drop_duplicates("store_id").set_index("store_id")
    stores["cap"] = stores.capacity_units.astype(int)
    for c in ("city", "zone", "region"):
        stores[c] = stores[c].fillna("")
    snap = D["stock_snapshot"].copy()
    today = snap.as_of_date.max()
    if pd.isna(today):
        raise DataError("Sheet 'stock_snapshot': no readable as_of_date.")
    if "warehouses" in D:
        whs = D["warehouses"].drop_duplicates("warehouse_id").set_index("warehouse_id")
        for c in ("city", "zone"):
            whs[c] = whs[c] if c in whs else ""
    else:
        ids = sorted(set(snap.location_id) - set(stores.index))
        whs = pd.DataFrame({"city": "", "zone": ""}, index=pd.Index(ids, name="warehouse_id"))
    if whs.empty:
        raise DataError("No warehouse found: add warehouses.csv, or include warehouse stock in stock_snapshot "
                        "(a location_id that is not a store).")
    loc = {s: dict(type="outlet" if r.store_type == "outlet" else "store", city=r.city, zone=r.zone) for s, r in stores.iterrows()}
    loc |= {w: dict(type="warehouse", city=str(r.city), zone=str(r.zone)) for w, r in whs.iterrows()}
    lanes = Lanes(D, cfg, loc)
    home = {}
    for s, r in stores.iterrows():
        h = r.home_warehouse if r.home_warehouse in whs.index else min(whs.index, key=lambda w: (lanes.lead(w, s), w))
        home[s] = h
    stores["home_wh"] = pd.Series(home)
    stores["wh_lead"] = [lanes.lead(home[s], s) for s in stores.index]
    outlets = set(stores.index[stores.store_type == "outlet"])
    tiers = pc["tier_order"] or (list(dict.fromkeys(D["min_display"].tier)) if "min_display" in D else [])
    tiers += sorted(set(stores.tier) - set(tiers))
    tier_rank = {t: i for i, t in enumerate(tiers)}

    # ---- stock snapshot: status, unknown locations, reconciliation, negatives, age
    snap = snap[snap.as_of_date == today]
    bad_loc = ~snap.location_id.isin(set(stores.index) | set(whs.index))
    if bad_loc.any():
        issue("Unknown location", ", ".join(sorted(snap[bad_loc].location_id.unique())[:5]), "",
              f"{int(bad_loc.sum())} stock rows at locations not in stores/warehouses", "Ignored. Add the location or fix the code.")
    snap = snap[~bad_loc & snap.sku_id.isin(items.index)].copy()
    sellable = {x.lower() for x in cfg["stock_status"]["sellable"]}
    snap["ok"] = snap.stock_status.str.lower().isin(sellable)
    held = snap[~snap.ok]
    nonsell = pd.DataFrame()
    if len(held):
        held = held.assign(value=held.on_hand * held.sku_id.map(items.mrp))
        nonsell = held.groupby(["location_id", "stock_status"]).agg(pieces=("on_hand", "sum"), value=("value", "sum")).reset_index()
        for loc_id, grp in nonsell.groupby("location_id"):
            txt = ", ".join(f"{int(r.pieces)} {r.stock_status}" for r in grp.itertuples())
            issue("Non-sellable stock", loc_id, "", f"{txt} ({money(grp.value.sum(), cfg)} at MRP)",
                  "Not counted as stock and never moved. Clear alterations/QC so it can sell.", "Medium", grp.value.sum())
    sn = snap[snap.ok].groupby(["location_id", "sku_id"]).agg(
        on_hand=("on_hand", "sum"), in_transit=("in_transit", "sum"),
        oldest=("oldest_stock_received_on", "min")).reset_index()
    tr_other = snap[~snap.ok].groupby(["location_id", "sku_id"]).in_transit.sum()   # transit is transit whatever the row
    sn = sn.set_index(["location_id", "sku_id"])
    sn["in_transit"] = sn.in_transit.add(tr_other, fill_value=0).reindex(sn.index).fillna(sn.in_transit)
    sn = sn.reset_index()
    sn["oh"] = sn.on_hand
    sn["suspect"] = False
    wk = D.get("stock_weekly")
    if wk is not None and "closing_on_hand" in wk:
        last = wk[(wk.week_ending == today) & (wk.closing_on_hand >= 0)][["store_id", "sku_id", "closing_on_hand"]]
        sn = sn.merge(last, left_on=["location_id", "sku_id"], right_on=["store_id", "sku_id"], how="left").drop(columns="store_id")
        bad = sn.closing_on_hand.notna() & (sn.closing_on_hand != sn.on_hand)
        for r in sn[bad].itertuples():
            use = max(int(r.closing_on_hand), 0)
            issue("Stock records disagree", r.location_id, r.sku_id,
                  f"Snapshot says {r.on_hand} on hand, weekly count for {today:%d %b} says {int(r.closing_on_hand)}",
                  f"Planned with {use} (the physical count). Not used as a donor. Store to recount.")
        sn.loc[bad, "oh"] = sn.loc[bad, "closing_on_hand"].clip(lower=0)
        sn.loc[bad, "suspect"] = True
    neg = sn.oh < 0
    for r in sn[neg].itertuples():
        issue("Negative stock", r.location_id, r.sku_id, f"On hand is {r.oh}", "Treated as 0. Store to recount.")
    sn.loc[neg, "oh"] = 0
    sn.loc[neg, "suspect"] = True
    sn["oh"] = sn.oh.astype(int)
    sn["age"] = (today - sn.oldest).dt.days
    if sn.oldest.isna().all():
        issue("Stock age missing", "", "", "No oldest_stock_received_on dates", "Ageing moves and pull-backs are switched off", "Medium")

    if "opened_on" in stores:
        for s, r in stores[stores.opened_on > today - pd.Timedelta(days=60)].iterrows():
            issue("New store", s, "", f"Opened {r.opened_on:%d %b %Y}: little sales history",
                  "Demand borrowed from similar stores until its own sales build up", "Info")
    prot = D["protected_styles"].groupby("store_id").style_id.apply(set).to_dict() if "protected_styles" in D else {}
    md = {}
    if "min_display" in D:
        md = D["min_display"].set_index(["tier", "category"]).min_display_units.to_dict()
    mgr = D["managers"].set_index("manager_id") if "managers" in D else pd.DataFrame()

    # ---- sales
    sales = D["sales"]
    sales = sales[sales.sku_id.isin(items.index) & sales.store_id.isin(stores.index)].copy()
    if sales.empty:
        raise DataError("No sales rows match the stores and items sheets (check store_id and sku_id codes).")
    sales = sales[sales.date <= today]
    # realised discount per category -> net price and margin per SKU
    disc = pd.Series(ec["default_discount"], index=items.category.unique())
    if "net_value" in sales and (sales.net_value > 0).any():
        pos = sales[(sales.qty > 0) & (sales.net_value > 0)]
        grossv = (pos.qty * pos.sku_id.map(items.mrp)).groupby(pos.sku_id.map(items.category)).sum()
        netv = pos.net_value.groupby(pos.sku_id.map(items.category)).sum()
        disc = (1 - netv / grossv).clip(0, 0.7).reindex(disc.index).fillna(ec["default_discount"])
    items["discount"] = items.category.map(disc)
    items["net_price"] = items.mrp * (1 - items.discount) / (1 + ec["tax_rate"])
    items["margin"] = items.net_price - items.cost

    # ---- core sizes per category
    core = {}
    if "size_sets" in D:
        ss = D["size_sets"]
        core = {c: set(g.loc[g.is_core.astype(str).isin(["1", "true", "yes", "y"]), "size"]) for c, g in ss.groupby("category")}
    for c, sz in sc["core_sizes"].items():
        core[c] = set(sz)
    recent = sales[sales.date > today - pd.Timedelta(days=365)]
    for c, grp in items.groupby("category"):
        if c in core or grp["size"].nunique() < 3:
            continue
        sh = recent[recent.sku_id.map(items.category) == c].groupby(recent.sku_id.map(items["size"])).qty.sum()
        sh = sh[sh > 0].sort_values(ascending=False)
        if sh.sum() <= 0:
            continue
        cum = (sh.cumsum() / sh.sum())
        k = max(2, int((cum < sc["core_share"]).sum()) + 1)
        core[c] = set(sh.index[:k])
    items["is_core"] = [s in core.get(c, set()) for s, c in zip(items["size"], items.category)]

    return dict(items=items, stores=stores, whs=whs, snap=sn, today=today, prot=prot, md=md, mgr=mgr, lanes=lanes,
                outlets=outlets, tier_rank=tier_rank, core=core, nonsell=nonsell, sales=sales, ex=ex, issue=issue, cfg=cfg)


# ============================================================================= 2. velocity
def velocity(D, P):
    items, stores, today, cfg = P["items"], P["stores"], P["today"], P["cfg"]["planning"]
    sales = P["sales"]
    nweeks = cfg["history_weeks"]
    wk = D.get("stock_weekly")
    if wk is not None:
        wk = wk[wk.sku_id.isin(items.index) & wk.store_id.isin(stores.index)].copy()
        wk = wk[wk.week_ending > today - pd.Timedelta(days=7 * nweeks)]
    if wk is None or wk.empty:
        P["issue"]("No in-stock history", "", "", "stock_weekly not given: zero sales can't be told apart from zero stock",
                   "Selling speed uses plain sales averages; upload stock_weekly to catch hidden stockouts", "Medium")
        ends = pd.date_range(end=today, periods=nweeks, freq="7D")
        s = sales[sales.date > today - pd.Timedelta(days=7 * nweeks)]
        have = set(zip(s.store_id, s.sku_id)) | set(zip(P["snap"].location_id, P["snap"].sku_id))
        have = [k for k in have if k[0] in stores.index]
        wk = pd.DataFrame([(e, a, b, 7) for a, b in have for e in ends], columns=["week_ending", "store_id", "sku_id", "days_in_stock"])
        P["weekly_synthetic"] = True
    wk = wk.rename(columns={"week_ending": "week"})
    end_dow = int(wk.week.dt.dayofweek.mode().iloc[0])
    w0 = wk.week.min() - pd.Timedelta(days=6)
    s = sales[(sales.date >= w0) & (sales.date <= today)].copy()
    s["week"] = s.date + pd.to_timedelta((end_dow - s.date.dt.dayofweek) % 7, unit="D")
    sw = s.groupby(["store_id", "sku_id", "week"]).qty.sum().rename("sold").reset_index()
    f = wk[["store_id", "sku_id", "week", "days_in_stock"]].merge(sw, on=["store_id", "sku_id", "week"], how="outer") \
        .fillna({"sold": 0, "days_in_stock": 0})
    f["sold"] = f.sold.clip(lower=0)
    f["days"] = np.where((f.sold > 0) & (f.days_in_stock == 0), 1, f.days_in_stock)
    f["w"] = cfg["week_decay"] ** ((today - f.week).dt.days // 7)
    f["ws"], f["wd"] = f.w * f.sold, f.w * f.days
    g = f.groupby(["store_id", "sku_id"]).agg(ws=("ws", "sum"), wd=("wd", "sum"), ev=("days", "sum"),
                                              sold12=("sold", "sum")).reset_index()
    grid = pd.MultiIndex.from_product([stores.index, items.index], names=["store_id", "sku_id"]).to_frame(index=False)
    g = grid.merge(g, how="left").fillna({"ws": 0, "wd": 0, "ev": 0, "sold12": 0})
    s28 = s[s.date > today - pd.Timedelta(days=28)].groupby(["store_id", "sku_id"]).qty.sum().clip(lower=0).rename("sold28").reset_index()
    g = g.merge(s28, how="left").fillna({"sold28": 0})
    chain = g.groupby("sku_id").agg(ws=("ws", "sum"), wd=("wd", "sum"))
    chain_rate = (chain.ws / chain.wd).where(chain.wd > 0)
    yr = sales[sales.date > today - pd.Timedelta(days=365)].groupby("sku_id").qty.sum().clip(lower=0)
    fb = (yr / (len(stores) * 365)).reindex(items.index).fillna(0)
    chain_rate = chain_rate.reindex(items.index).fillna(fb)
    g["chain_rate"] = g.sku_id.map(chain_rate)
    g["category"] = g.sku_id.map(items.category)
    g["tier"] = g.store_id.map(stores.tier)
    g["exp"] = g.wd * g.chain_rate
    sc = g.groupby(["store_id", "category"]).agg(num=("ws", "sum"), den=("exp", "sum"), tier=("tier", "first")).reset_index()
    sc["raw"] = (sc.num / sc.den).where(sc.den >= 3)
    tier_med = sc.groupby(["tier", "category"]).raw.median()
    sc["tm"] = [tier_med.get((t, c), 1.0) for t, c in zip(sc.tier, sc.category)]
    sc["tm"] = sc.tm.fillna(1.0)
    sc["m"] = ((sc.num + 3 * sc.tm) / (sc.den + 3)).clip(0.1, 5)
    g = g.merge(sc[["store_id", "category", "m"]], on=["store_id", "category"])
    g["prior"] = g.chain_rate * g.m
    own = (g.ws / g.wd).where(g.wd > 0, 0)
    K = cfg["shrink_days"]
    g["v"] = np.where(g.ev > 0, (own * g.ev + g.prior * K) / (g.ev + K), g.prior)
    launch = g.sku_id.map(items.launch_date)
    g.loc[launch > today, "v"] = 0
    return g[["store_id", "sku_id", "v", "ws", "wd", "ev", "sold12", "sold28", "chain_rate", "m"]], f


# ============================================================================= 3. season
class Season:
    """Day index per region from the brand's calendar: peak days and the 7 days up to an event sell differently."""

    def __init__(self, D, P):
        st, sales, cfg = P["stores"], P["sales"], P["cfg"]["season"]
        self.peak, self.event = cfg["peak_label"], cfg["event_label"]
        self.today, self.idx, self.lift, self.typ = P["today"], {}, {}, {}
        self.base_from = self.today - pd.Timedelta(days=83)
        cal = D.get("calendar")
        self.on = cal is not None and len(cal) > 0
        if not self.on:
            return
        days_ = pd.date_range(sales.date.min(), max(cal.date.max(), self.today + pd.Timedelta(days=90)))
        ss = sales.assign(region=sales.store_id.map(st.region)).groupby(["region", "date"]).qty.sum()
        for region in st.region.unique():
            c = cal[cal.region == region].drop_duplicates("date").set_index("date").reindex(days_)
            peak = c.is_peak_day.fillna(0).eq(1)
            pre = pd.Series(False, index=days_)
            for fd in c.index[c.event.fillna("").ne("")]:
                pre[(days_ >= fd - pd.Timedelta(days=6)) & (days_ <= fd)] = True
            typ = pd.Series("normal", index=days_)
            typ[pre] = self.event
            typ[peak] = self.peak
            typ[peak & pre] = f"{self.peak} + {self.event}"
            units = ss.get(region, pd.Series(dtype=float)).reindex(days_).fillna(0)
            hist = units.index <= self.today
            base = units[hist & (typ == "normal")].mean()
            lift = {t: (units[hist & (typ == t)].mean() / base if (hist & (typ == t)).sum() >= 10 and base > 0 else np.nan)
                    for t in typ.unique()}
            lift["normal"] = 1.0
            for t, fb in ((self.event, "normal"), (self.peak, "normal"), (f"{self.peak} + {self.event}", self.peak)):
                if t in lift and lift[t] != lift[t]:
                    lift[t] = lift.get(fb, 1.0)
            self.lift[region], self.idx[region], self.typ[region] = lift, typ.map(lift), typ

    def mult(self, region, start, n):
        if not self.on or region not in self.idx:
            return 1.0
        i = self.idx[region]
        fwd = i.reindex(pd.date_range(start, periods=n)).fillna(1.0).mean()
        back = i[self.base_from:self.today].mean()
        return float(np.clip(fwd / back, 0.5, 3.0)) if back > 0 else 1.0

    def next_event(self, region, start, n):
        if not self.on or region not in self.typ:
            return ""
        t = self.typ[region].reindex(pd.date_range(start, periods=n)).fillna("normal")
        out = []
        f = t[t.str.contains(self.event, regex=False)]
        w = t[t.str.contains(self.peak, regex=False)]
        if len(f):
            out.append(f"{self.event} from {f.index[0]:%d %b}")
        if len(w):
            out.append(f"{len(w)} {self.peak}s from {w.index[0]:%d %b}")
        return "; ".join(out)


# ============================================================================= 4. plan
class Planner:
    def __init__(self, D, P, vel, season):
        self.P, self.season, self.cfg = P, season, P["cfg"]
        pc, ac, sc = self.cfg["planning"], self.cfg["ageing"], self.cfg["size_sets"]
        it, st, snap = P["items"], P["stores"], P["snap"]
        self.it, self.st, self.lanes, self.outlets = it, st, P["lanes"], P["outlets"]
        self.whs = list(P["whs"].index)
        self.run = P["today"] + pd.Timedelta(days=1)
        self.moves, self.queued = [], []
        stock = snap[snap.location_id.isin(st.index)].set_index(["location_id", "sku_id"])
        g = vel.copy()
        key = pd.MultiIndex.from_arrays([g.store_id, g.sku_id])
        g["oh"] = stock.oh.reindex(key).fillna(0).astype(int).values
        g["transit"] = stock.in_transit.reindex(key).fillna(0).astype(int).values
        g["age"] = stock.age.reindex(key).values
        g["suspect"] = stock.suspect.reindex(key).fillna(False).astype(bool).values
        g["pos"] = g.oh + g.transit
        g["region"] = g.store_id.map(st.region)
        g["L"] = g.store_id.map(st.wh_lead).astype(int)
        H = pc["cover_days"]
        mult_need = {(r, L): season.mult(r, self.run, L + H) for r, L in set(zip(g.region, g.L))}
        mult30 = {r: season.mult(r, self.run, 30) for r in g.region.unique()}
        g["fd"] = g.v * [mult_need[(r, L)] for r, L in zip(g.region, g.L)]
        g["fd30"] = g.v * g.region.map(mult30)
        for c in ("style_id", "option", "disp_cat", "mrp", "size", "is_core", "season_end", "category"):
            g[c] = g.sku_id.map(it[c])
        g = g.rename(columns={"style_id": "style", "disp_cat": "dcat"})
        # season end: no refill that lands after the season, and only up to the end
        left = (g.season_end - self.run).dt.days
        horizon = (g.L + H).astype(float)
        g["eoss"] = left.notna() & (left <= 0)
        g.loc[g.eoss, ["fd", "fd30"]] = 0.0          # season over: regular stores no longer sell it
        capped = left.notna() & (left < horizon)
        horizon = np.where(capped, np.where(left > g.L, left, 0), horizon)
        g["target"] = g.fd * horizon
        g.loc[g.store_id.isin(self.outlets), "target"] = 0
        g["need"] = np.maximum(0, np.floor(g.target - g.pos + 0.5)).astype(int)
        g["base_need"] = g.need
        # size sets: an active option missing a core size gets one of it
        opt = g.groupby(["store_id", "option"]).agg(sizes=("pos", lambda x: int((x > 0).sum())), ofd=("fd30", "sum"))
        g = g.merge(opt, left_on=["store_id", "option"], right_index=True, how="left")
        active = (g.sizes >= 2) & (g.ofd >= sc["active_option_per_day"])
        hole = active & g.is_core & (g.pos == 0) & (g.chain_rate > 0) & ~g.eoss & (g.target > 0) & ~g.store_id.isin(self.outlets)
        g.loc[hole & (g.need < 1), "need"] = 1
        g["set_fill"] = hole & (g.base_need < 1)
        self.g = g.set_index(["store_id", "sku_id"])
        self.sty_pos = g.groupby(["store_id", "style"]).pos.sum().to_dict()
        self.sty_fd30 = g.groupby(["store_id", "style"]).fd30.sum().to_dict()
        self.sty_give = (g.groupby(["store_id", "style"]).sold28.sum() / 28).to_dict()
        self.cat_pos = g.groupby(["store_id", "dcat"]).pos.sum().to_dict()
        self.opt_fd = g.groupby(["store_id", "option"]).fd30.sum().to_dict()
        self.opt_skus = it.groupby("option").apply(lambda x: list(x.index)).to_dict()
        self.row = {k: r for k, r in zip(self.g.index, self.g.to_dict("records"))}   # fast row access
        self.inc, self.out = defaultdict(int), defaultdict(int)
        self.sty_out, self.cat_in, self.cat_out = defaultdict(int), defaultdict(int), defaultdict(int)
        self.room = {s: int(min(st.cap[s], INF) - g[g.store_id == s].pos.sum()) for s in st.index}
        self.trips = defaultdict(set)   # store -> destinations it already ships to today
        # warehouse supply: (warehouse, sku) -> [(date, qty)]
        whs = snap[snap.location_id.isin(self.whs)].groupby(["location_id", "sku_id"]).oh.sum()
        self.supply = defaultdict(list)
        for (w, k), q in whs.items():
            if q > 0:
                self.supply[(w, k)].append((self.run, int(q)))
        if "inbound" in D:
            inb = D["inbound"]
            inb = inb[(inb.expected_date <= self.run + pd.Timedelta(days=pc["inbound_usable_days"])) & inb.sku_id.isin(it.index)].copy()
            inb["warehouse_id"] = inb.warehouse_id.where(inb.warehouse_id.isin(self.whs), self.whs[0])
            for (w, k, d), q in inb.groupby(["warehouse_id", "sku_id", "expected_date"]).qty.sum().items():
                self.supply[(w, k)].append((max(d, self.run), int(q)))
        for v in self.supply.values():
            v.sort()
        self.wh_start = {k: sum(q for _, q in v) for k, v in self.supply.items()}
        self.wh_sku = defaultdict(list)
        for (w, k) in self.supply:
            self.wh_sku[k].append(w)

    # --- helpers
    def lead(self, a, b):
        return self.lanes.lead(a, b)

    def cur(self, s, k):
        return int(self.g.at[(s, k), "pos"]) + self.inc[(s, k)] - self.out[(s, k)]

    def cover(self, s, k, col="fd30"):
        fd = self.g.at[(s, k), col]
        return self.cur(s, k) / fd if fd > 1e-9 else float("inf")

    def opt_sizes_left(self, s, opt, skip=None):
        return sum(1 for k in self.opt_skus[opt] if k != skip and self.cur(s, k) > 0)

    def give_cap(self, d, k, aged=False, whole_option=False):
        """Units store d may give of SKU k without breaking a rule: protected styles, donor cover, minimum display,
        stock it really holds, and (unless the whole option is leaving) one of each core size of an active option."""
        if d in self.outlets:
            return 0
        row = self.row[(d, k)]
        if row["style"] in self.P["prot"].get(d, set()) or self.inc[(d, k)] > 0 or row["suspect"]:
            return 0
        pc = self.cfg["planning"]
        need30 = row["fd30"] * pc["donor_min_cover"]
        keep = math.floor(need30 + 0.5) if aged else math.ceil(need30 - 1e-9)
        if row["is_core"] and not whole_option and not row["eoss"] and self.opt_sizes_left(d, row["option"], skip=k) >= 1 \
                and self.opt_fd.get((d, row["option"]), 0) >= self.cfg["size_sets"]["active_option_per_day"]:
            keep = max(keep, 1)
        g1 = int(row["oh"]) - self.out[(d, k)] - keep
        sfd = max(self.sty_fd30[(d, row["style"])], self.sty_give.get((d, row["style"]), 0))
        spos = self.sty_pos[(d, row["style"])] - self.sty_out[(d, row["style"])]
        g2 = math.floor(spos - pc["donor_min_cover"] * pc["donor_margin"] * sfd + 1e-9) if sfd > 1e-9 and not row["eoss"] else INF
        minimum = self.P["md"].get((self.st.tier[d], row["dcat"]), 0)
        g3 = self.cat_pos[(d, row["dcat"])] + self.cat_in[(d, row["dcat"])] - self.cat_out[(d, row["dcat"])] - minimum
        return max(0, min(g1, g2, g3))

    def add(self, typ, frm, to, k, qty, dispatch, reason, prio, group, mandatory=False):
        if qty <= 0:
            return
        if frm in self.st.index:
            row = self.g.loc[(frm, k)]
            self.out[(frm, k)] += qty
            self.sty_out[(frm, row["style"])] += qty
            self.cat_out[(frm, row["dcat"])] += qty   # freed shelf space is not re-used today: safe if the line is dropped later
            if to in self.st.index:
                self.trips[frm].add(to)
        if to in self.st.index:
            row = self.g.loc[(to, k)]
            self.inc[(to, k)] += qty
            self.cat_in[(to, row["dcat"])] += qty
            self.room[to] -= qty
        self.moves.append(dict(type=typ, from_id=frm, to_id=to, sku_id=k, qty=int(qty), dispatch=dispatch,
                               arrives=dispatch + pd.Timedelta(days=self.lead(frm, to)), priority=prio,
                               reason=reason, group=group, mandatory=mandatory))

    def prio(self, s, k, L):
        c = self.g.at[(s, k), "pos"] / max(self.g.at[(s, k), "fd"], 1e-9)
        return "High" if c < L else "Medium" if c < L + 14 else "Low"

    def ev(self, s, L):
        return self.season.next_event(self.st.region[s], self.run, L + self.cfg["planning"]["cover_days"])

    def refill_group(self, s, k, prio, L):
        if self.g.at[(s, k), "set_fill"]:
            return G_SET
        if prio == "High":
            return G_URGENT
        return G_PEAK if self.ev(s, L) else G_TOPUP

    def can_dispatch(self, d, s):
        return s in self.trips[d] or len(self.trips[d]) < self.cfg["economics"]["max_dispatch_trips_per_store"]

    # --- A. warehouse -> store
    def warehouse(self):
        g = self.g
        H = self.cfg["planning"]["cover_days"]
        needy = g[g.need > 0].reset_index()
        order = needy.assign(val=needy.need * needy.mrp).groupby("sku_id").val.sum().sort_values(ascending=False)
        rank = self.P["tier_rank"]
        for k in order.index:
            ws = [w for w in self.wh_sku.get(k, []) if self.supply[(w, k)]]
            if not ws:
                continue
            cands = needy[needy.sku_id == k]
            heap = [(r.pos / max(r.fd, 1e-9), rank.get(self.st.tier[r.store_id], 99), r.store_id) for r in cands.itertuples()]
            heapq.heapify(heap)
            got = defaultdict(lambda: defaultdict(int))
            while heap:
                c, t, s = heapq.heappop(heap)
                if self.room[s] - sum(got[s].values()) <= 0:
                    continue
                live = [w for w in ws if self.supply[(w, k)]]
                if not live:
                    heapq.heappush(heap, (c, t, s))
                    break
                w = min(live, key=lambda x: (x != self.st.home_wh[s], self.lead(x, s)))
                d, q = self.supply[(w, k)][0]
                got[s][(w, d)] += 1
                self.supply[(w, k)][0] = (d, q - 1)
                if q - 1 == 0:
                    self.supply[(w, k)].pop(0)
                n = sum(got[s].values())
                if n < g.at[(s, k), "need"]:
                    heapq.heappush(heap, ((g.at[(s, k), "pos"] + n) / max(g.at[(s, k), "fd"], 1e-9), t, s))
            short = len(heap)
            for s, by in got.items():
                r = g.loc[(s, k)]
                L = int(r.L)
                tot = sum(by.values())
                cov = r.pos / r.fd if r.fd > 1e-9 else float("inf")
                ev = self.ev(s, L)
                pr = self.prio(s, k, L)
                if r.set_fill:
                    txt = (f"{s} has other sizes of {r.option} on display but no {r['size']}; send 1 to complete the core size set.")
                else:
                    txt = (f"{s} sells {r.fd:.2f}/day; holds {int(r.oh)}" + (f" (+{int(r.transit)} in transit)" if r.transit else "")
                           + f" = {cov:.0f} days vs {L}-day lead" + (f"; {ev}" if ev else "")
                           + f". Send {tot} for {H} days' cover" + (" (warehouse short: shared by lowest cover)" if short else "") + ".")
                for (w, d), q in sorted(by.items(), key=lambda x: x[0][1]):
                    self.add(T_WH, w, s, k, q, d, txt, pr, self.refill_group(s, k, pr, L))

    # --- minimum display (rule-driven: always kept)
    def display_gaps(self):
        gaps = {}
        for (s, c), pos in self.cat_pos.items():
            if s in self.outlets:
                continue
            m = self.P["md"].get((self.st.tier[s], c), 0)
            have = pos + self.cat_in[(s, c)] - self.cat_out[(s, c)]
            if have < m:
                gaps[(s, c)] = m - have
        return gaps

    def display_from_wh(self):
        for (s, c), gap in self.display_gaps().items():
            rows = self.g.loc[s]
            skus = rows[(rows.dcat == c) & ~rows.eoss].sort_values("fd", ascending=False).index
            sent = defaultdict(int)
            m = self.P["md"][(self.st.tier[s], c)]
            while gap > 0 and self.room[s] - sum(sent.values()) > 0:
                progress = False
                for k in skus:
                    if gap == 0:
                        break
                    live = [w for w in self.wh_sku.get(k, []) if self.supply[(w, k)]]
                    if not live:
                        continue
                    w = min(live, key=lambda x: (x != self.st.home_wh[s], self.lead(x, s)))
                    d, q = self.supply[(w, k)][0]
                    self.supply[(w, k)][0] = (d, q - 1)
                    if q - 1 == 0:
                        self.supply[(w, k)].pop(0)
                    sent[(w, k, d)] += 1
                    gap -= 1
                    progress = True
                if not progress:
                    break
            for (w, k, d), q in sent.items():
                self.add(T_DISP, w, s, k, q, d, f"{s} {c} display is below the tier {self.st.tier[s]} minimum of {m}; "
                                                f"topping up with its best-selling sizes.", "High", G_DISP, mandatory=True)

    # --- B. store -> store
    def donors(self, s, k, aged_first=True):
        out = []
        for d in self.st.index:
            if d == s or d in self.outlets or not self.can_dispatch(d, s):
                continue
            cap = self.give_cap(d, k)
            if cap > 0:
                age = self.g.at[(d, k), "age"]
                aged = 0 if (age == age and age >= self.cfg["ageing"]["age_days"]) else 1
                # an existing trip to the same store counts as 2 days closer: fewer, fuller trips
                eff = self.lead(d, s) + (0 if s in self.trips[d] else 2)
                out.append((eff, aged, -self.cover(d, k), d, cap))
        return sorted(out)

    def transfers(self):
        g = self.g
        rows = []
        for (s, k), r in g[g.need > 0].iterrows():
            unmet = int(r.need) - self.inc[(s, k)]
            if unmet > 0 and s not in self.outlets:
                urgent = r.pos / max(r.fd, 1e-9) < r.L
                rows.append((not urgent, -unmet * r.fd * r.mrp, s, k, unmet))
        for _, _, s, k, unmet in sorted(rows):
            for _, _, _, d, _ in self.donors(s, k):
                if unmet <= 0 or self.room[s] <= 0:
                    break
                q = min(unmet, self.room[s], self.give_cap(d, k))
                if q <= 0:
                    continue
                r = g.loc[(s, k)]
                L = self.lead(d, s)
                dc_before = self.cover(d, k)
                pr = self.prio(s, k, L)
                grp = self.refill_group(s, k, pr, L)
                self.add(T_S2S, d, s, k, q, self.run, "", pr, grp)
                sty = r["style"]
                sc = (self.sty_pos[(d, sty)] - self.sty_out[(d, sty)]) / max(self.sty_fd30[(d, sty)], 1e-9)
                cov = r.pos / r.fd if r.fd > 1e-9 else float("inf")
                ev = self.ev(s, L)
                self.moves[-1]["reason"] = (
                    (f"{s} is missing core size {r['size']} of {r.option}; " if r.set_fill else
                     f"Warehouse can't cover it; {s} sells {r.fd:.2f}/day with {cov:.0f} days left" + (f" ({ev})" if ev else "") + "; ")
                    + f"{d} has {days(dc_before)} of this size and keeps {days(sc)} of the style; arrives in {L} days.")
                unmet -= q
        for (s, c), gap in self.display_gaps().items():
            rows_ = self.g.loc[s]
            skus = rows_[(rows_.dcat == c) & ~rows_.eoss].sort_values("fd", ascending=False).index
            m = self.P["md"][(self.st.tier[s], c)]
            for k in skus:
                if gap <= 0 or self.room[s] <= 0:
                    break
                for *_, d, _ in self.donors(s, k):
                    q = min(gap, 1, self.give_cap(d, k), self.room[s])
                    if q > 0:
                        self.add(T_DISP, d, s, k, q, self.run, f"{s} {c} display below the tier {self.st.tier[s]} minimum of {m} "
                                                               f"and no warehouse stock; {d} can spare it.", "High", G_DISP, mandatory=True)
                        gap -= q
                        break

    # --- C + D. ageing by option, broken runs reunited, capped pull-backs, clearance to outlets
    def _clear_dest(self, s, k):
        """Season-ended or dead stock: nearest outlet with room, else the store's warehouse."""
        outs = [o for o in self.outlets if self.room[o] > 0]
        if outs:
            o = min(outs, key=lambda x: self.lead(s, x))
            return o, T_OUT, G_CLEAR
        return self.st.home_wh[s], T_PULL, G_CLEAR

    def ageing(self):
        g, ac, sc = self.g, self.cfg["ageing"], self.cfg["size_sets"]
        if g.age.isna().all():
            return
        aged = g[(g.oh > 0) & ((g.age >= ac["age_days"]) | g.eoss)].reset_index()
        aged = aged[~aged.store_id.isin(self.outlets)]
        pulls = []   # candidate returns, capped per store afterwards
        aged["val"] = aged.oh * aged.mrp
        hot = getattr(self, "hot", set())
        for (s, opt), grp in sorted(aged.groupby(["store_id", "option"]), key=lambda x: -x[1].val.sum()):
            rows = g.loc[s]
            orow = rows[rows.option == opt]
            held = [k for k in orow.index if self.cur(s, k) - int(orow.at[k, "transit"]) > 0]
            eoss = bool(orow.eoss.any())
            ofd = self.opt_fd.get((s, opt), 0)
            opos = sum(self.cur(s, k) for k in orow.index)
            ocov = opos / ofd if ofd > 1e-9 else float("inf")
            if eoss:
                for k in held:
                    q = self.give_cap(s, k, aged=True, whole_option=True)
                    if q > 0:
                        dest, typ, grp_ = self._clear_dest(s, k)
                        pulls.append((2, q * orow.at[k, "mrp"], s, k, q, dest, typ, grp_,
                                      f"Season for {opt} has ended; {q} at {s} go to {dest} for clearance."))
                continue
            core = {k for k in orow.index if orow.at[k, "is_core"]}
            missing_core = [k for k in core if self.cur(s, k) <= 0]
            fragment = bool(core) and missing_core and 0 < opos <= sc["fragment_max_units"] and ocov > ac["age_cover"]
            if fragment:
                best = None
                for r in self.st.index:
                    if r == s or r in self.outlets or self.room[r] <= 0 or not self.can_dispatch(s, r):
                        continue
                    rfd = self.opt_fd.get((r, opt), 0)
                    if rfd < sc["active_option_per_day"]:
                        continue
                    fills = sum(1 for k in held if self.cur(r, k) <= 0)
                    if fills == 0:
                        continue
                    key = (fills, rfd, -self.lead(s, r))
                    if best is None or key > best[0]:
                        best = (key, r)
                if best:
                    r = best[1]
                    sent = 0
                    for k in held:
                        q = min(self.give_cap(s, k, aged=True, whole_option=True), self.room[r])
                        if q > 0 and self.out[(r, k)] == 0:
                            self.add(T_AGE, s, r, k, q, self.run,
                                     f"{s} holds a broken run of {opt} ({opos} pcs, core sizes missing, {days(ocov)} of stock); "
                                     f"{r} sells the option and lacks these sizes, so the run is reunited there.", "Low", G_REUNITE)
                            sent += q
                    if sent:
                        continue
                if ocov > ac["pullback_cover"] and opt.split(" · ")[0] not in hot:
                    for k in held:
                        q = self.give_cap(s, k, aged=True, whole_option=True)
                        if q > 0:
                            pulls.append((1, q * orow.at[k, "mrp"] * (1 + (orow.at[k, "age"] or 0) / 365), s, k, q, self.st.home_wh[s],
                                          T_PULL, G_PULL, f"Broken run of {opt} at {s} ({opos} pcs, {days(ocov)} of stock) that no store "
                                                          f"can use; return the whole run so it can be re-sized centrally."))
                continue
            # healthy set: move aged sizes, one receiver per option where possible
            recip_order = sorted((r for r in self.st.index if r != s and r not in self.outlets),
                                 key=lambda r: -self.opt_fd.get((r, opt), 0))
            for k in [k for k in held if (orow.at[k, "age"] or 0) >= ac["age_days"]]:
                cov = self.cover(s, k)
                if cov <= ac["age_cover"]:
                    continue
                spare = self.give_cap(s, k, aged=True)
                if spare <= 0:
                    continue
                cands = []
                for r in recip_order:
                    if self.out[(r, k)] > 0 or self.room[r] <= 0 or not self.can_dispatch(s, r):
                        continue
                    rr = g.loc[(r, k)]
                    if rr.fd < 1 / 30:
                        continue
                    want = math.ceil(rr.fd * ac["sell_within"]) - self.cur(r, k)
                    if want > 0:
                        cands.append((0 if r in self.trips[s] else 1, -self.opt_fd.get((r, opt), 0), self.lead(s, r), r, want))
                for *_, r, want in sorted(cands):
                    q = min(spare, want, self.room[r], self.give_cap(s, k, aged=True))
                    if q <= 0:
                        continue
                    rr = g.loc[(r, k)]
                    self.add(T_AGE, s, r, k, q, self.run,
                             f"Held {int(orow.at[k, 'age'])} days at {s}, which sells {orow.at[k, 'fd30']:.2f}/day ({days(cov)} of stock); "
                             f"{r} sells {rr.fd:.2f}/day and will clear it within {ac['sell_within']} days.", "Low", G_AGE)
                    spare -= q
                if orow.at[k, "style"] in hot or spare <= 0 or self.cover(s, k) <= ac["pullback_cover"]:
                    continue
                chain = g.xs(k, level="sku_id")
                dead = chain.sold12.sum() == 0 and chain.ev.sum() >= 84
                keep1 = 0 if dead else 1
                q = min(spare, self.give_cap(s, k, aged=True), self.cur(s, k) - int(orow.at[k, "transit"]) - keep1)
                if q < (1 if dead else 2):
                    continue
                if dead:
                    dest, typ, grp_ = self._clear_dest(s, k)
                    why = f"No store has sold it in {self.cfg['planning']['history_weeks']} weeks; send {q} to {dest} for clearance."
                else:
                    dest, typ, grp_ = self.st.home_wh[s], T_PULL, G_PULL
                    why = f"no store can sell it within {ac['sell_within']} days; return {q} to {dest} (keeps 1 on display)."
                    why = f"Held {int(orow.at[k, 'age'])} days at {s} ({days(self.cover(s, k))} of stock) and " + why
                pulls.append((0 if dead else 1, q * orow.at[k, "mrp"] * (1 + orow.at[k, "age"] / 365), s, k, q, dest, typ, grp_, why))
        # cap returns per store: most valuable / oldest first, the rest waits for the next run
        stock_now = self.g.groupby(level="store_id").pos.sum()
        used, used_v = defaultdict(int), defaultdict(float)
        for pr, val, s, k, q, dest, typ, grp_, why in sorted(pulls, key=lambda x: (-x[0] if x[0] == 2 else 0, -x[1])):
            cap = min(ac["pullback_max_units_per_store"], max(1, int(ac["pullback_max_share_of_stock"] * stock_now.get(s, 0))))
            unit = float(self.it.mrp[k])
            vcap = int(max(0.0, ac["pullback_max_value_per_store"] - used_v[s]) // unit) if unit > 0 else q
            take = min(q, cap - used[s], vcap, self.give_cap(s, k, aged=True, whole_option=True))
            if dest in self.st.index:
                take = min(take, self.room[dest])
            if take > 0:
                self.add(typ, s, dest, k, take, self.run, why, "Low", grp_)
                used[s] += take
                used_v[s] += take * unit
            if q - max(take, 0) > 0:
                self.queued.append(dict(store=s, sku_id=k, qty=int(q - max(take, 0)), dest=dest, why=why,
                                        value_mrp=float((q - max(take, 0)) * self.it.mrp[k])))

    def plan(self):
        self.warehouse()
        self.display_from_wh()
        self.transfers()
        self.ageing()
        return self.moves


# ============================================================================= 5. value + batch
def _gain(pos, q, mu):
    """Extra pieces expected to sell when stock goes from pos to pos+q under Poisson demand with mean mu."""
    if q <= 0 or mu <= 0:
        return 0.0
    ks = np.arange(max(pos, 0) + 1, max(pos, 0) + q + 1)
    return float(poisson.sf(ks - 1, mu).sum())


def value_moves(mv, pl):
    """Money on every line: margin gained (net of sales lost at the giver), markdown and holding avoided,
    recovery on returns, freight + handling. Trip fixed cost is shared by pieces."""
    cfg = pl.cfg
    ec, ac, H = cfg["economics"], cfg["ageing"], cfg["planning"]["cover_days"]
    g, it, lanes = pl.g, pl.it, pl.lanes
    run_pos = {}
    out_tot = mv[mv.from_id.isin(pl.st.index)].groupby(["from_id", "sku_id"]).qty.sum().to_dict()
    rows = []
    for m in mv.itertuples():
        k, q = m.sku_id, m.qty
        I = it.loc[k]
        lane = lanes.get(m.from_id, m.to_id)
        var_cost = q * (lane["cost_per_unit"] + ec["handling_per_unit"])
        dest_gain = src_loss = markdown = holding = recovery = 0.0
        src_cover = 0.0
        if m.from_id in pl.st.index:
            r = g.loc[(m.from_id, k)]
            final = int(r.pos) - out_tot.get((m.from_id, k), 0)
            mu = r.fd30 * ec["horizon_days"]
            src_loss = _gain(final, q, mu) if not r.eoss else 0.0
            src_cover = (r.pos / r.fd30) if r.fd30 > 1e-9 else 999
        if m.to_id in pl.st.index and m.to_id not in pl.outlets:
            r = g.loc[(m.to_id, k)]
            p0 = run_pos.get((m.to_id, k), int(r.pos))
            T = (int(r.L) + H) if m.type in (T_WH, T_DISP, T_S2S) else ac["sell_within"]
            T = min(T, ec["horizon_days"] + int(r.L))
            fd = r.fd if m.type in (T_WH, T_DISP, T_S2S) else r.fd30
            dest_gain = _gain(p0, q, fd * T)
            run_pos[(m.to_id, k)] = p0 + q
        units = max(dest_gain - src_loss, 0.0) if m.type != T_DISP else dest_gain - src_loss
        margin_gain = units * I.margin
        if m.type in (T_AGE,):
            months = min(6.0, max(0.0, (min(src_cover, 365) - ac["sell_within"]) / 30))
            markdown = units * I.net_price * ec["markdown_per_month"] * months
            holding = units * I.cost * ec["store_holding_rate"] * min(src_cover, 180) / 365
        if m.type in (T_PULL, T_OUT):
            idle = max(q - src_loss, 0.0)
            recovery = idle * (I.net_price * ec["pullback_recovery_uplift"]
                               + I.cost * (ec["store_holding_rate"] - ec["wh_holding_rate"]) * 180 / 365)
            margin_gain = -src_loss * I.margin
        gross = margin_gain + markdown + holding + recovery
        rows.append(dict(exp_extra_sales=round(units, 2), margin_gain=margin_gain, markdown_avoided=markdown,
                         holding_saved=holding, recovery=recovery, benefit=gross, freight_handling=var_cost,
                         trip_fixed=lane["cost_per_trip"], line_net_var=gross - var_cost))
    return pd.concat([mv.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def batch(mv, pl):
    """Group lines into trips; drop lines and trips that cost more than they earn (rule-driven lines stay)."""
    ec = pl.cfg["economics"]
    mv = mv.copy()
    mv["trip_key"] = mv.from_id + ">" + mv.to_id + ">" + mv.dispatch.dt.strftime("%Y%m%d")
    keep = mv.mandatory | (mv.line_net_var >= 0)
    mv.loc[~keep, "skip_why"] = "Freight and handling cost more than the margin it would add"
    for _ in range(3):
        k = mv[keep]
        trip = k.groupby("trip_key").agg(net=("line_net_var", "sum"), fixed=("trip_fixed", "first"), units=("qty", "sum"),
                                         mand=("mandatory", "any"), high=("priority", lambda x: (x == "High").any()),
                                         s2s=("from_id", lambda x: x.iloc[0] in pl.st.index))
        trip["tnet"] = trip.net - trip.fixed
        drop_net = trip.index[(trip.tnet < 0) & ~trip.mand]
        drop_small = trip.index[trip.s2s & (trip.units < ec["min_trip_units"]) & ~trip.mand & ~trip.high
                                & ~k.groupby("trip_key").to_id.first().isin(pl.st.home_wh.unique()).reindex(trip.index).fillna(False)]
        drop = set(drop_net) | set(drop_small)
        if not drop:
            break
        hit = mv.trip_key.isin(drop) & keep & ~mv.mandatory
        mv.loc[hit & mv.trip_key.isin(drop_net), "skip_why"] = "Whole trip costs more (incl. vehicle/courier cost) than it earns"
        mv.loc[hit & mv.trip_key.isin(set(drop_small) - set(drop_net)), "skip_why"] = \
            f"Trip under {ec['min_trip_units']} pieces and not urgent: not worth a separate dispatch"
        keep &= ~hit
    kept, skipped = mv[keep].copy(), mv[~keep].copy()
    share = kept.qty / kept.groupby("trip_key").qty.transform("sum")
    kept["trip_cost_share"] = kept.trip_fixed * share
    kept["move_cost"] = kept.freight_handling + kept.trip_cost_share
    kept["net_benefit"] = kept.benefit - kept.move_cost
    skipped["move_cost"] = skipped.freight_handling + skipped.trip_fixed * skipped.qty / skipped.groupby("trip_key").qty.transform("sum")
    skipped["net_benefit"] = skipped.benefit - skipped.move_cost
    skipped["policy"] = skipped.skip_why.fillna("").str.startswith("Trip under")
    return kept, skipped


def trips_table(mv, pl, cfg):
    st = pl.st
    pc = cfg["planning"]
    rows = []
    for i, (key, t) in enumerate(sorted(mv.groupby("trip_key"), key=lambda x: (-(x[1].priority == "High").sum(), -x[1].net_benefit.sum())), 1):
        grp = t.groupby("group").qty.sum().reindex(GROUPS).dropna().astype(int)
        why = ", ".join(f"{gname.lower() if j else gname} ({n} pcs)" for j, (gname, n) in enumerate(grp.items()))
        f, to = t.from_id.iloc[0], t.to_id.iloc[0]
        big = bool((t.value_mrp > pc["approval_value"]).any() or t.value_mrp.sum() > pc["trip_approval_value"])
        rows.append(dict(
            trip_key=key, to_number=f"TO-{pl.run:%y%m%d}-{i:03d}", from_id=f, to_id=to,
            dispatch=t.dispatch.iloc[0], arrives=t.arrives.max(), lines=len(t), pieces=int(t.qty.sum()),
            value_mrp=float(t.value_mrp.sum()), benefit=float(t.benefit.sum()), move_cost=float(t.move_cost.sum()),
            net_benefit=float(t.net_benefit.sum()), urgent=int((t.priority == "High").sum()), needs_approval=big,
            main_group=grp.idxmax() if len(grp) else "", reason=f"{why}. Gains {money(t.benefit.sum(), cfg)} for "
                                                               f"{money(t.move_cost.sum(), cfg)} of moving cost."))
    return pd.DataFrame(rows)


# ============================================================================= 6. validate
def validate(m, pl):
    P, g, st, cfg = pl.P, pl.g, pl.st, pl.cfg
    pc, ac = cfg["planning"], cfg["ageing"]
    res = []

    def rule(name, bad, n):
        res.append(dict(rule=name, checked=n, violations=len(bad), status="PASS" if not bad else "FAIL",
                        detail="; ".join(map(str, bad[:5]))))

    rule("Every move has a positive quantity and a different destination", m[(m.qty <= 0) | (m.from_id == m.to_id)].move_id.tolist(), len(m))
    rule("Every move is explained in one line", m[(m.reason.str.len() < 20) | m.reason.str.contains("\n")].move_id.tolist(), len(m))
    out = m[m.from_id.isin(st.index)].groupby(["from_id", "sku_id"]).qty.sum()
    inn = m[m.to_id.isin(st.index)].groupby(["to_id", "sku_id"]).qty.sum()
    rule("Stores only give stock they physically hold (not in transit, not suspect counts)",
         [k for k, q in out.items() if q > g.at[k, "oh"] or g.at[k, "suspect"]], len(out))
    wh = m[m.from_id.isin(pl.whs)].groupby(["from_id", "sku_id"]).qty.sum()
    rule("Warehouses never send more than on hand + inbound due", [k for k, q in wh.items() if q > pl.wh_start.get(k, 0)], len(wh))
    final = g.pos.add(inn.rename_axis(["store_id", "sku_id"]), fill_value=0).sub(out.rename_axis(["store_id", "sku_id"]), fill_value=0)
    gf = g.assign(final=final)
    receivers = set(m[m.to_id.isin(st.index)].to_id)
    tot = gf.groupby(level="store_id").final.sum()
    before = g.groupby(level="store_id").pos.sum()
    rule("No receiving store ends above its capacity",
         [f"{s}: {int(tot[s])}>{st.cap[s]}" for s in receivers if tot[s] > st.cap[s] and tot[s] > before[s]], len(receivers))
    givers = set(m[m.from_id.isin(st.index)].from_id)
    cat = gf.groupby([gf.index.get_level_values(0), "dcat"]).final.sum()
    catb = g.groupby([g.index.get_level_values(0), "dcat"]).pos.sum()
    rule("Giving stores stay at or above minimum display per category",
         [f"{s} {c}" for (s, c), v in cat.items() if s in givers and v < P["md"].get((st.tier[s], c), 0) and v < catb[(s, c)]], len(givers))
    rule("Protected styles are never given away",
         [f"{r.from_id} {r.sku_id}" for r in m.itertuples() if r.from_id in st.index
          and g.at[(r.from_id, r.sku_id), "style"] in P["prot"].get(r.from_id, set())], int(m.from_id.isin(P["prot"]).sum()))
    s2s = m[(m.type == T_S2S)]
    sty = gf.groupby([gf.index.get_level_values(0), "style"]).agg(final=("final", "sum"), fd30=("fd30", "sum"))
    bad = []
    for d, sku in set(zip(s2s.from_id, s2s.sku_id)):
        r = sty.loc[(d, g.at[(d, sku), "style"])]
        if r.fd30 > 1e-9 and r.final / r.fd30 < pc["donor_min_cover"] - 1e-6:
            bad.append(f"{d} {sku}: {r.final / r.fd30:.1f} days")
    rule(f"No store-to-store refill leaves the giver under {pc['donor_min_cover']} days of style cover", bad, len(s2s))
    repl = m[m.type == T_WH]
    over = repl.groupby(["to_id", "sku_id"]).qty.sum()
    rule("Stock in transit is counted: nothing is sent beyond need",
         [f"{s} {k}" for (s, k), q in over.items() if q > g.at[(s, k), "need"]], len(repl))
    both = set(out.index) & set(inn.index)
    rule("No store both gives and receives the same SKU", sorted(both), len(out) + len(inn))
    big = m[m.value_mrp > pc["approval_value"]]
    rule(f"Lines above {money(pc['approval_value'], cfg)} at MRP are flagged for approval", big[big.approval == ""].move_id.tolist(), len(big))
    opt = m[~m.mandatory]
    tr = m.groupby("trip_key").agg(net=("net_benefit", "sum"), mand=("mandatory", "any"))
    rule("Every optional line pays for its own freight and handling", opt[opt.line_net_var < -1e-6].move_id.tolist(), len(opt))
    rule("Every trip without a rule-driven line earns more than it costs", tr[(tr.net < -1e-6) & ~tr.mand].index.tolist(), len(tr))
    back = m[m.type.isin([T_PULL, T_OUT]) & m.from_id.isin(st.index)].groupby("from_id").qty.sum()
    bv = m[m.type.isin([T_PULL, T_OUT]) & m.from_id.isin(st.index)].groupby("from_id").value_mrp.sum()
    rule(f"Returns stay within {ac['pullback_max_units_per_store']} pieces and {money(ac['pullback_max_value_per_store'], cfg)} per store per run",
         [f"{s}: {q}" for s, q in back.items() if q > ac["pullback_max_units_per_store"]]
         + [f"{s}: {v:.0f}" for s, v in bv.items() if v > ac["pullback_max_value_per_store"] + 1e-6], len(back))
    bad = []
    fin_opt = gf.reset_index()
    for r in s2s.itertuples():
        row = g.loc[(r.from_id, r.sku_id)]
        if not row.is_core:
            continue
        o = fin_opt[(fin_opt.store_id == r.from_id) & (fin_opt.option == row.option)]
        active = o.fd30.sum() >= cfg["size_sets"]["active_option_per_day"]
        if active and (o.final > 0).sum() >= 1 and gf.at[(r.from_id, r.sku_id), "final"] <= 0 and row.oh > 0:
            bad.append(f"{r.from_id} {r.sku_id}")
    rule("Refills never strip the last core size from a giver's active option", bad, len(s2s))
    rule("Nothing is refilled after its season ends", m[m.type.isin([T_WH, T_S2S, T_DISP]).values &
                                                       np.array([bool(g.at[(t, k), "eoss"]) if t in st.index else False for t, k in zip(m.to_id, m.sku_id)], dtype=bool)].move_id.tolist(), len(m))
    rule("Outlets are never refilled or used as donors",
         m[(m.to_id.isin(pl.outlets) & (m.type != T_OUT)) | m.from_id.isin(pl.outlets)].move_id.tolist(), len(m))
    return pd.DataFrame(res)


# ============================================================================= 7. findings, alerts, approval, write
def sold_out_hits(P, weekly):
    today, it, sales = P["today"], P["items"], P["sales"]
    s = sales[sales.date > today - pd.Timedelta(days=270)].copy()
    if s.empty:
        return pd.DataFrame()
    s["style"] = s.sku_id.map(it.style_id)
    s["m"] = s.date.dt.to_period("M")
    by = s.groupby(["style", "m"]).qty.sum().unstack(fill_value=0)
    share = by / s.groupby("m").qty.sum()
    rec = s[s.date > today - pd.Timedelta(days=60)]
    rshare = rec.groupby("style").qty.sum() / max(rec.qty.sum(), 1)
    w = weekly[weekly.week > today - pd.Timedelta(days=28)]
    instock = w.groupby(w.sku_id.map(it.style_id)).days.mean() / 7
    snap = P["snap"]
    stock = snap.groupby(snap.sku_id.map(it.style_id)).oh.sum() + snap.groupby(snap.sku_id.map(it.style_id)).in_transit.sum()
    floor = max(10, int(0.002 * s.qty.sum() / 9))
    rows = []
    for sty in by.index:
        peak_m, peak = by.loc[sty].idxmax(), int(by.loc[sty].max())
        if peak < floor or share.loc[sty, peak_m] <= 0:
            continue
        ratio = rshare.get(sty, 0) / share.loc[sty, peak_m]
        on_shelf = instock.get(sty, 1.0)
        if ratio <= 0.35 and on_shelf < 0.45:
            rows.append(dict(style=sty, name=it[it.style_id == sty].style_name.iloc[0], peak=peak, peak_m=peak_m,
                             now=int(by.loc[sty].get(today.to_period("M"), 0)), on_shelf=on_shelf, stock=int(stock.get(sty, 0))))
    return pd.DataFrame(rows)


def hidden_stockouts(pl, weekly, today):
    if getattr(pl, "synthetic", False):
        return pd.DataFrame(columns=["store_id", "sku_id", "fd30", "at_risk", "mrp"])
    recent = weekly[weekly.week > today - pd.Timedelta(days=28)].groupby(["store_id", "sku_id"]).days.sum()
    g = pl.g
    hs = g[(g.oh == 0) & (g.fd30 >= 0.05) & ~g.eoss].copy()
    hs["days4w"] = recent.reindex(hs.index).fillna(0).values
    hs = hs[hs.days4w == 0].reset_index()
    hs["at_risk"] = hs.fd30 * 30 * hs.sku_id.map(pl.it.net_price)
    return hs


def findings(P, pl, weekly, mv, skipped):
    issue, g, it, st, cfg = P["issue"], pl.g, pl.it, pl.st, pl.cfg
    for r in getattr(pl, "hits", pd.DataFrame()).itertuples():
        issue("Sold-out hit", "", r.style, f"{r.name}: sold {r.peak} in {r.peak_m.strftime('%b %Y')} but only {r.now} this month; "
                                           f"shelves were empty {1 - r.on_shelf:.0%} of the time; {r.stock} pieces left chain-wide",
              "Not a slow seller: re-order, and it is never pulled back as dead stock", "High")
    hs = hidden_stockouts(pl, weekly, P["today"])
    for s, grp in hs.groupby("store_id"):
        issue("Hidden stockout", s, f"{len(grp)} SKUs", f"{len(grp)} sizes that should sell {grp.fd30.sum() * 30:.0f} pieces in 30 days "
                                                       f"had no stock for 4 weeks (~{money(grp.at_risk.sum(), cfg)} of sales at risk)",
              "Selling speed rebuilt from in-stock days and similar stores; refilled where stock exists", "High", grp.at_risk.sum())
    w = weekly[weekly.week > P["today"] - pd.Timedelta(days=84)]
    held = w[w.store_id.isin(st.index)].groupby("store_id").days_in_stock.mean() if "days_in_stock" in w else pd.Series(dtype=float)
    sold = g.groupby(level="store_id").fd30.sum()
    for s in st.index:
        if st.cap[s] >= INF:
            continue
        fill = g.loc[s].pos.sum() / st.cap[s]
        if held.get(s, 7) < 5 and fill < 0.6 and not getattr(pl, "synthetic", False):
            issue("Under-stocked store", s, "", f"Only {fill:.0%} of capacity used; sizes on the shelf {held[s]:.1f} of 7 days, "
                                                f"yet it sells {sold[s]:.1f} pieces/day", "Priority refill in this plan", "High")
        if fill > 1:
            issue("Over capacity", s, "", f"Records show {fill:.0%} of capacity", "Nothing more is sent until resolved", "High")
    # broken size sets left after the plan
    fin = g.pos.copy()
    for r in mv.itertuples():
        if r.to_id in st.index:
            fin[(r.to_id, r.sku_id)] += r.qty
        if r.from_id in st.index:
            fin[(r.from_id, r.sku_id)] -= r.qty
    gg = g.assign(final=fin).reset_index()
    gg = gg[~gg.store_id.isin(pl.outlets)]
    act = gg.groupby(["store_id", "option"]).agg(sizes=("final", lambda x: (x > 0).sum()), ofd=("fd30", "sum"))
    core_miss = gg[gg.is_core & (gg.final <= 0)].groupby(["store_id", "option"]).size()
    brk = act[(act.sizes >= 2) & (act.ofd >= cfg["size_sets"]["active_option_per_day"])].join(core_miss.rename("miss"), how="inner")
    for s, grp in brk.reset_index().groupby("store_id"):
        issue("Broken size sets after plan", s, f"{len(grp)} options", f"{len(grp)} selling options still miss a core size "
                                                                       f"(e.g. {', '.join(grp.option.head(3))})",
              "No stock of those sizes anywhere it can safely come from; flag to buying", "Medium")
    sold_all = P["sales"].groupby("sku_id").qty.sum()
    for sty, grp in it.groupby("style_id"):
        stock = int(g[g["style"] == sty].oh.sum())
        if sold_all.reindex(grp.index).fillna(0).sum() == 0 and stock > 0:
            issue("Dead style", "", sty, f"{grp.style_name.iloc[0]}: no sales in the data, {stock} pieces in stores",
                  "Cleared to outlet / warehouse in capped steps; stop buying", "Medium", stock * grp.mrp.mean())
        ld = grp.launch_date.iloc[0]
        if ld == ld and ld > P["today"] - pd.Timedelta(days=60):
            issue("New launch", "", sty, f"{grp.style_name.iloc[0]} launched {ld:%d %b %Y}: little sales history",
                  "Not treated as slow; speed measured only on days in stock", "Info")
    need = g.groupby("dcat").need.sum()
    whc = pd.Series({k: v for (w, k), v in pl.wh_start.items()}).groupby(level=0).sum()
    whc = whc.groupby(whc.index.map(it.disp_cat)).sum() if len(whc) else whc
    for c in need.index:
        if need[c] > 2 * whc.get(c, 0) and need[c] >= 20:
            issue("Warehouse shortfall", ", ".join(pl.whs), c, f"Stores need {int(need[c])} {c} pieces for the next 4-5 weeks; "
                                                               f"warehouses have {int(whc.get(c, 0))}",
                  "Shared by who runs out first plus safe store-to-store moves; chase suppliers", "High")
    for (s, c), gap in pl.display_gaps().items():
        issue("Below minimum display after plan", s, c, f"Still {gap} pieces short of the minimum", "No safe source; flag to buying", "High")
    eco = skipped[~skipped.policy] if len(skipped) else skipped
    pol = skipped[skipped.policy] if len(skipped) else skipped
    if len(eco):
        issue("Not worth moving", "", f"{len(eco)} lines", f"{int(eco.qty.sum())} pieces would cost {money(eco.move_cost.sum(), cfg)} to move "
                                                          f"and earn only {money(eco.benefit.sum(), cfg)}",
              "Left where they are; see 'Not worth moving'", "Info", eco.move_cost.sum() - eco.benefit.sum())
    if len(pol):
        issue("Small trips held back", "", f"{pol.trip_key.nunique()} trips", f"{int(pol.qty.sum())} pieces on store-to-store trips under "
                                                                            f"{cfg['economics']['min_trip_units']} pieces (net {money(pol.net_benefit.sum(), cfg)})",
              "Held back to keep dispatches few; lower 'smallest trip' in Settings to include them", "Info")
    if pl.queued:
        q = pd.DataFrame(pl.queued)
        issue("Returns queued", ", ".join(sorted(q.store.unique())[:4]), f"{len(q)} lines",
              f"{int(q.qty.sum())} more slow pieces ({money(q.value_mrp.sum(), cfg)}) are over today's return cap",
              "They go in the next runs, a few per store at a time", "Info")


def manager_alerts(P, mv, ex, trips):
    mgr, st, cfg = P["mgr"], P["stores"], P["cfg"]
    rows = []
    if mgr.empty:
        return pd.DataFrame(columns=["manager", "rank", "alert"])
    for a, ar in mgr.iterrows():
        cand = []
        for s in [x.strip() for x in str(ar.stores).split(",") if x.strip()]:
            hi = mv[(mv.to_id == s) & (mv.priority == "High")]
            if len(hi):
                cand.append((hi.value_mrp.sum(), f"{s}: {hi.sku_id.nunique()} sizes about to run out; {int(hi.qty.sum())} pieces "
                                                 f"arriving by {hi.arrives.max():%d %b}. Please receive and display."))
            outs = trips[(trips.from_id == s)]
            if len(outs):
                cand.append((outs.value_mrp.sum() * 0.8, f"{s}: pack {int(outs.pieces.sum())} pieces in {len(outs)} trips today "
                                                         f"({', '.join(outs.to_number.head(3))})."))
            for e in ex[(ex.store == s) & (ex.severity == "High")].itertuples():
                cand.append((5e7, f"{s}: {e.issue}: {e.detail} -> {e.action}"))
        n = int(float(ar.get("max_alerts_per_week", 5) or 5))
        for i, (_, txt) in enumerate(sorted(cand, key=lambda x: -x[0])[:n], 1):
            rows.append(dict(manager=f"{a} {ar.get('name', '')}".strip(), rank=i, alert=txt))
    return pd.DataFrame(rows, columns=["manager", "rank", "alert"])


def approval_pack(trips, cfg):
    """About a dozen decisions instead of every line: urgent trips, sign-offs, then one decision per broad reason."""
    rows = []
    urgent = trips[(trips.urgent > 0) & ~trips.needs_approval]
    for f, t in sorted(urgent.groupby("from_id"), key=lambda x: -x[1].net_benefit.sum()):
        if len(t) == 1:
            r = t.iloc[0]
            dec, det = f"{r.to_number}: {r.from_id} → {r.to_id}", r.reason
        else:
            dec = f"Urgent refills from {f} to {len(t)} stores"
            det = (f"{', '.join(t.to_id)}; {int(t.urgent.sum())} sizes run out before a normal delivery; gains {money(t.benefit.sum(), cfg)} "
                   f"for {money(t.move_cost.sum(), cfg)} moving cost")
        rows.append(dict(step="1 · Urgent trips", decision=dec, trips=len(t), pieces=int(t.pieces.sum()), value_mrp=t.value_mrp.sum(),
                         net_benefit=t.net_benefit.sum(), detail=det, to_numbers=",".join(t.to_number)))
    big = trips[trips.needs_approval]
    for r in big.sort_values("value_mrp", ascending=False).itertuples():
        rows.append(dict(step="2 · Sign-off (high value)", decision=f"{r.to_number}: {r.from_id} → {r.to_id}", trips=1, pieces=r.pieces,
                         value_mrp=r.value_mrp, net_benefit=r.net_benefit, detail=r.reason, to_numbers=r.to_number))
    rest = trips.drop(index=urgent.index.union(big.index))
    for gname in GROUPS:
        t = rest[rest.main_group == gname]
        if len(t):
            rows.append(dict(step="3 · Routine", decision=gname, trips=len(t), pieces=int(t.pieces.sum()), value_mrp=t.value_mrp.sum(),
                             net_benefit=t.net_benefit.sum(), detail=f"{len(t)} trips, {t.from_id.nunique()} senders → {t.to_id.nunique()} "
                                                                     f"receivers; gains {money(t.benefit.sum(), cfg)} for "
                                                                     f"{money(t.move_cost.sum(), cfg)} moving cost",
                             to_numbers=",".join(t.to_number)))
    return pd.DataFrame(rows, columns=["step", "decision", "trips", "pieces", "value_mrp", "net_benefit", "detail", "to_numbers"])


def transfer_orders(mv, trips):
    """Generic transfer-order lines any ERP/WMS can import (one TO per trip)."""
    t = mv.merge(trips[["trip_key", "to_number"]], on="trip_key")
    t = t.sort_values(["to_number", "sku_id"])
    t["line_no"] = t.groupby("to_number").cumcount() + 1
    return pd.DataFrame({"to_number": t.to_number, "line_no": t.line_no, "from_location": t.from_id, "to_location": t.to_id,
                         "sku_id": t.sku_id, "qty": t.qty, "ship_date": t.dispatch.dt.strftime("%Y-%m-%d"),
                         "expected_receipt": t.arrives.dt.strftime("%Y-%m-%d"), "move_type": t.type, "reason_group": t.group,
                         "move_id": t.move_id})


def opportunity(R):
    """What the plan is worth, what it costs, and what is still being lost."""
    pl, mv, sk, cfg = R["pl"], R["moves"], R["skipped"], R["cfg"]
    ec, ac = cfg["economics"], cfg["ageing"]
    g, it = pl.g, pl.it
    fin = g.pos.copy()
    inc = mv[mv.to_id.isin(pl.st.index)].groupby(["to_id", "sku_id"]).qty.sum()
    for k, q in inc.items():
        fin[k] += q
    still = g.assign(final=fin)
    still = still[(still.target > still.final + 0.5) & ~still.eoss]
    lost = 0.0
    for (s, k), r in still.iterrows():
        mu = r.fd * (r.L + cfg["planning"]["cover_days"])
        lost += _gain(int(r.final), int(round(r.target - r.final)), mu) * it.margin[k]
    out_ = mv[mv.from_id.isin(pl.st.index) & mv.type.isin([T_AGE, T_PULL, T_OUT])].groupby(["from_id", "sku_id"]).qty.sum()
    aged = g[(g.oh > 0) & (g.age >= ac["age_days"])].copy()
    aged["left"] = aged.oh - out_.reindex(aged.index).fillna(0)
    aged = aged[aged.left > 0]
    aged_month = float((aged.left * (aged.sku_id.map(it.cost) if "sku_id" in aged else aged.index.get_level_values(1).map(it.cost))
                        * ec["store_holding_rate"] / 12).sum()
                       + (aged.left * aged.index.get_level_values(1).map(it.net_price) * ec["markdown_per_month"]).sum())
    by = mv.groupby("group").agg(lines=("qty", "size"), pieces=("qty", "sum"), benefit=("benefit", "sum"),
                                 move_cost=("move_cost", "sum"), net=("net_benefit", "sum")).reindex(GROUPS).dropna(how="all").reset_index()
    return dict(benefit=float(mv.benefit.sum()), move_cost=float(mv.move_cost.sum()), net=float(mv.net_benefit.sum()),
                skipped_cost=float(sk[~sk.policy].move_cost.sum()) if len(sk) else 0.0,
                skipped_benefit=float(sk[~sk.policy].benefit.sum()) if len(sk) else 0.0,
                held_small=int(sk.policy.sum()) if len(sk) else 0, held_small_net=float(sk[sk.policy].net_benefit.sum()) if len(sk) else 0.0,
                still_at_risk=lost, aged_cost_per_month=aged_month, aged_left_pieces=int(aged.left.sum()), by_group=by)


def write(out_dir, R):
    pl, mv, ex, val, alerts, trips, cfg = R["pl"], R["moves"], R["issues"], R["checks"], R["alerts"], R["trips"], R["cfg"]
    out_dir.mkdir(parents=True, exist_ok=True)
    run = pl.run
    O = R["opportunity"]
    cols = ["move_id", "to_number", "priority", "approval", "group", "type", "from_id", "to_id", "style_name", "colour", "size",
            "sku_id", "qty", "value_mrp", "benefit", "move_cost", "net_benefit", "dispatch", "arrives", "reason"]
    view = mv.merge(trips[["trip_key", "to_number"]], on="trip_key")[cols].copy()
    for c in ("dispatch", "arrives"):
        view[c] = view[c].dt.strftime("%d %b")
    for c in ("benefit", "move_cost", "net_benefit"):
        view[c] = view[c].round(0)
    tv = trips.drop(columns=["trip_key"]).copy()
    for c in ("dispatch", "arrives"):
        tv[c] = tv[c].dt.strftime("%d %b")
    tv = tv.round(0)
    tv.insert(0, "approve (Y/N)", "Y")
    sk = R["skipped"]
    skv = sk[["type", "group", "from_id", "to_id", "style_name", "size", "sku_id", "qty", "value_mrp", "benefit", "move_cost",
              "net_benefit", "skip_why", "reason"]].round(0) if len(sk) else pd.DataFrame()
    g = pl.g.reset_index()
    vel = g[(g.pos > 0) | (g.fd30 > 0.01)][["store_id", "sku_id", "option", "dcat", "size", "is_core", "v", "fd", "fd30", "oh", "transit",
                                             "pos", "age", "L", "target", "need", "sold12", "ev"]].round(3)
    xl = out_dir / f"stockflow_plan_{run:%Y-%m-%d}.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl") as w:
        head = pd.DataFrame({f"StockFlow · {cfg['brand']['name']}": [
            f"Plan for {run:%a %d %b %Y} (stock as of close {pl.P['today']:%d %b %Y})",
            f"{len(trips)} trips, {len(mv)} lines, {int(mv.qty.sum())} pieces, {money(mv.value_mrp.sum(), cfg)} at MRP",
            f"Expected gain {money(O['benefit'], cfg)} for {money(O['move_cost'], cfg)} of moving cost: net {money(O['net'], cfg)}",
            f"Left out as not worth moving: {len(sk) - O['held_small']} lines (would cost {money(O['skipped_cost'], cfg)} to earn {money(O['skipped_benefit'], cfg)}); "
            f"{O['held_small']} lines on tiny store-to-store trips held back",
            f"Still at risk after the plan (no safe stock anywhere): {money(O['still_at_risk'], cfg)} of margin",
            f"Old stock still in stores costs about {money(O['aged_cost_per_month'], cfg)} a month to keep",
            f"Rule checks: {(val.status == 'PASS').sum()} of {len(val)} passed · data issues: {len(ex)}"]})
        head.to_excel(w, sheet_name="Summary", index=False)
        O["by_group"].round(0).to_excel(w, sheet_name="Summary", index=False, startrow=10)
        approval_pack(trips, cfg).round(0).drop(columns="to_numbers").assign(**{"approve (Y/N)": "Y"}).to_excel(w, sheet_name="Approve first", index=False)
        tv.to_excel(w, sheet_name="Trips", index=False)
        view.to_excel(w, sheet_name="Moves", index=False)
        R["orders"].to_excel(w, sheet_name="Transfer orders", index=False)
        skv.to_excel(w, sheet_name="Not worth moving", index=False)
        pd.DataFrame(pl.queued).to_excel(w, sheet_name="Returns queued", index=False)
        ex.to_excel(w, sheet_name="Data issues", index=False)
        alerts.to_excel(w, sheet_name="Manager alerts", index=False)
        val.to_excel(w, sheet_name="Rule checks", index=False)
        vel.to_excel(w, sheet_name="Velocity", index=False)
        for ws in w.book.worksheets:
            if ws.title != "Summary":
                ws.freeze_panes = "A2"
                ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 100)
    view.to_csv(out_dir / f"moves_{run:%Y-%m-%d}.csv", index=False)
    R["orders"].to_csv(out_dir / f"transfer_orders_{run:%Y-%m-%d}.csv", index=False)
    ex.to_csv(out_dir / f"data_issues_{run:%Y-%m-%d}.csv", index=False)
    val.to_csv(out_dir / f"rule_checks_{run:%Y-%m-%d}.csv", index=False)
    return xl


def run(source, out_dir=None, cfg=None):
    cfg = cfg or schema.load_config()
    D = load(source, cfg)
    P = prep(D, cfg)
    vel, weekly = velocity(D, P)
    season = Season(D, P)
    pl = Planner(D, P, vel, season)
    pl.synthetic = P.get("weekly_synthetic", False)
    pl.hits = sold_out_hits(P, weekly) if not pl.synthetic else pd.DataFrame()
    pl.hot = set(pl.hits["style"]) if len(pl.hits) else set()
    cols = ["type", "from_id", "to_id", "sku_id", "qty", "dispatch", "arrives", "priority", "reason", "group", "mandatory"]
    mv = pd.DataFrame(pl.plan(), columns=cols)
    it = P["items"]
    mv["style_name"] = mv.sku_id.map(it.style_name)
    mv["colour"] = mv.sku_id.map(it.colour)
    mv["size"] = mv.sku_id.map(it["size"])
    mv["value_mrp"] = (mv.qty * mv.sku_id.map(it.mrp)).astype(float)
    mv = value_moves(mv, pl)
    mv, skipped = batch(mv, pl)
    mv["approval"] = np.where(mv.value_mrp > cfg["planning"]["approval_value"], "Needs approval", "")
    order = {"High": 0, "Medium": 1, "Low": 2}
    mv = mv.sort_values(["type", "priority", "value_mrp"], key=lambda c: c.map(order) if c.name == "priority" else c,
                        ascending=[True, True, False]).reset_index(drop=True)
    mv.insert(0, "move_id", [f"M{i + 1:05d}" for i in range(len(mv))])
    trips = trips_table(mv, pl, cfg)
    if len(trips):
        mv.loc[mv.trip_key.isin(trips[trips.needs_approval].trip_key) & (mv.approval == ""), "approval"] = "Trip sign-off"
    val = validate(mv, pl)
    findings(P, pl, weekly, mv, skipped)
    ex = pd.DataFrame(P["ex"])
    if len(ex):
        ex = ex.sort_values("severity", key=lambda c: c.map({"High": 0, "Medium": 1, "Info": 2})).reset_index(drop=True)
    else:
        ex = pd.DataFrame(columns=["severity", "issue", "store", "sku", "detail", "action", "value"])
    R = dict(D=D, P=P, sales=P["sales"], weekly=weekly, season=season, pl=pl, moves=mv, skipped=skipped, trips=trips,
             issues=ex, checks=val, cfg=cfg, warehouses=pl.whs)
    R["alerts"] = manager_alerts(P, mv, ex, trips)
    R["orders"] = transfer_orders(mv, trips) if len(trips) else pd.DataFrame()
    R["opportunity"] = opportunity(R)
    R["xlsx"] = write(Path(out_dir), R) if out_dir else None
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=str(HERE / "output"))
    a = ap.parse_args()
    cfg = schema.load_config(a.config)
    R = run(a.data, a.out, cfg)
    mv, val, O, tr = R["moves"], R["checks"], R["opportunity"], R["trips"]
    print(f"StockFlow plan for {R['pl'].run:%d %b %Y} · {cfg['brand']['name']}")
    print(mv.groupby("type").agg(lines=("qty", "size"), pieces=("qty", "sum"), net=("net_benefit", "sum")).round(0).to_string())
    print(f"trips {len(tr)} · single-piece lines {int((mv.qty == 1).sum())} · skipped {len(R['skipped'])} · queued returns {len(R['pl'].queued)}")
    print(f"benefit {O['benefit']:.0f} cost {O['move_cost']:.0f} net {O['net']:.0f} still at risk {O['still_at_risk']:.0f}")
    print(val[["rule", "checked", "violations", "status"]].to_string(index=False))
    print(f"issues {len(R['issues'])} -> {R['xlsx']}")
    if (val.status != "PASS").any():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
