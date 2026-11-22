"""What StockFlow needs from a brand: settings (with defaults) and the data sheets to upload.

Everything brand-specific lives here or in a brand config JSON (see brands/). The engine itself has no
brand names, store grades, category names or warehouse codes in it.
"""
import copy
import json
from pathlib import Path

# ----------------------------------------------------------------------------- settings
DEFAULTS = {
    "brand": {
        "name": "Your Brand",
        "currency": "₹",
        "number_style": "indian",          # indian -> L / Cr, international -> K / M
        "primary_color": "#2F5D62",
    },
    "planning": {
        "cover_days": 28,                  # selling days a delivery should cover after it lands
        "donor_min_cover": 30,             # a giving store keeps at least this many days of the style
        "donor_margin": 1.1,               # plan the donor rule with 10% headroom
        "shrink_days": 28,                 # in-stock days before a store's own sales outweigh similar stores
        "week_decay": 0.85,                # weight of each older week of sales
        "history_weeks": 12,
        "inbound_usable_days": 1,          # inbound due within this many days of the run can be allocated
        "approval_value": 100000,          # one line above this (at MRP) needs a person's sign-off
        "trip_approval_value": 500000,     # one trip above this (at MRP) needs a person's sign-off
        "tier_order": [],                  # best tier first; empty = order found in min_display, then A-Z
        "category_groups": {},             # display groups, e.g. {"Accessories": ["Safa", "Mala", "Stole"]}
    },
    "size_sets": {
        "core_share": 0.70,                # core sizes = the fewest sizes making up 70% of a category's sales
        "core_sizes": {},                  # or set them: {"Kurta Set": ["M", "L", "XL"]}
        "fragment_max_units": 4,           # an ageing option this small and missing core sizes is a broken run
        "active_option_per_day": 0.02,     # an option selling at least this fast (pieces/day) is "active"
    },
    "ageing": {
        "age_days": 120,                   # stock older than this is ageing
        "age_cover": 60,                   # ...when the store holds more than this many days of it
        "sell_within": 45,                 # ageing stock goes only where it will sell within this many days
        "pullback_cover": 120,             # ageing stock above this cover, unsellable elsewhere, goes back
        "pullback_max_units_per_store": 25,  # cap per store per run; the rest waits for the next run
        "pullback_max_share_of_stock": 0.05,  # and never more than 5% of a store's pieces at once
        "pullback_max_value_per_store": 150000,  # and never more than this much (at MRP) per store per run
        "eoss_window_days": 21,            # within this many days of season end, stop refilling past the end
    },
    "economics": {
        "default_discount": 0.05,          # average discount off MRP when sales value is not given
        "tax_rate": 0.05,                  # MRP includes this tax; margin is worked out on the net price
        "default_cost_ratio": 0.40,        # cost price = 40% of MRP when cost_price is missing
        "store_holding_rate": 0.24,        # yearly cost of holding stock in a store (capital, space, shrink)
        "wh_holding_rate": 0.12,           # yearly cost of holding it in a warehouse
        "markdown_per_month": 0.04,        # value ageing stock loses each extra month it sits
        "pullback_recovery_uplift": 0.15,  # extra value recovered when slow stock is centralised (re-allocate / outlet / EOSS)
        "handling_per_unit": 15,           # pick, pack and receive, per piece per move
        "horizon_days": 45,                # window used to value a move
        "min_trip_units": 4,               # a store-to-store trip smaller than this is dropped unless it is urgent
        "max_dispatch_trips_per_store": 3,  # store-to-store trips one store packs per day
    },
    "lanes_default": {                     # used when lanes.csv does not cover a route
        "wh_to_store": {"lead_days": 4, "cost_per_unit": 40, "cost_per_trip": 800},
        "store_to_wh": {"lead_days": 4, "cost_per_unit": 40, "cost_per_trip": 800},
        "same_city": {"lead_days": 2, "cost_per_unit": 20, "cost_per_trip": 400},
        "same_zone": {"lead_days": 4, "cost_per_unit": 50, "cost_per_trip": 1500},
        "other": {"lead_days": 7, "cost_per_unit": 110, "cost_per_trip": 3000},
    },
    "stock_status": {
        "sellable": ["", "sellable", "available", "ok", "good", "fresh"],
    },
    "season": {
        "peak_label": "peak day",          # what calendar.is_peak_day means for this brand (e.g. "wedding date")
        "event_label": "event week",       # the 7 days up to a calendar event (e.g. "festival week")
    },
    "privacy": {
        "drop_columns": ["customer_id", "customer_name", "mobile", "phone", "email", "salesperson_id"],
    },
}

# tunable through the Feedback tab: (section, key): (min, max, step, plain name)
TUNABLE = {
    ("planning", "cover_days"): (14, 45, 3, "Days of selling each delivery covers"),
    ("ageing", "age_days"): (60, 240, 15, "Days before stock counts as old"),
    ("ageing", "sell_within"): (21, 75, 7, "Old stock goes only where it sells within (days)"),
    ("ageing", "pullback_cover"): (60, 240, 30, "Bring back old stock above this many days of cover"),
    ("ageing", "pullback_max_units_per_store"): (5, 100, 5, "Most pieces one store returns per run"),
    ("economics", "min_trip_units"): (1, 20, 1, "Smallest store-to-store trip worth making (pieces)"),
}


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None, extra=None):
    cfg = copy.deepcopy(DEFAULTS)
    if path and Path(path).exists():
        cfg = merge(cfg, json.loads(Path(path).read_text(encoding="utf-8")))
    return merge(cfg, extra)


# ----------------------------------------------------------------------------- data sheets
# table: (need, purpose, [(column, need, meaning, example)], aliases)   need: "required" | "recommended" | "optional"
R, REC, OPT = "required", "recommended", "optional"
TABLES = {
    "stores": (R, "Every selling location. Outlet stores take end-of-season stock and are never refilled.", [
        ("store_id", R, "Store code, used in every other sheet", "DEL-01"),
        ("city", R, "City", "Delhi"),
        ("region", R, "State or region; joins to calendar.region", "Delhi NCR"),
        ("zone", R, "Group of regions for transport (North, South…)", "North"),
        ("tier", REC, "Store grade (any labels; best first in settings.tier_order)", "A"),
        ("capacity_units", REC, "Most pieces the store can hold", "900"),
        ("opened_on", OPT, "Opening date (YYYY-MM-DD); new stores borrow history from similar stores", "2021-04-01"),
        ("store_type", OPT, "store (default) or outlet", "store"),
        ("home_warehouse", OPT, "Warehouse that normally serves it; default = quickest lane", "WH-NORTH"),
        ("manager_id", OPT, "Area manager; joins to managers", "AM-01"),
    ], {"stock_capacity_units": "capacity_units", "state": "region", "area_manager_id": "manager_id"}),
    "warehouses": (OPT, "Warehouses. If missing, any stock location that is not a store is a warehouse.", [
        ("warehouse_id", R, "Warehouse code as used in stock_snapshot.location_id", "WH-NORTH"),
        ("city", OPT, "City", "Delhi"),
        ("zone", OPT, "Zone", "North"),
    ], {"location_id": "warehouse_id"}),
    "items": (R, "One row per SKU (style + colour + size).", [
        ("sku_id", R, "SKU code", "KRT-014-MAR-L"),
        ("style_id", R, "Design code, shared by all its sizes and colours", "KRT-014"),
        ("style_name", R, "Design name", "Silk Kurta Set"),
        ("category", R, "Category (any labels)", "Kurta Set"),
        ("size", R, "Size label", "L"),
        ("mrp", R, "Tag price incl. tax", "4999"),
        ("cost_price", REC, "Landed cost per piece; needed for true margins", "1850"),
        ("colour", REC, "Colour; a style in one colour is one 'option' for size sets", "Maroon"),
        ("fit", OPT, "Fit or cut", "Regular"),
        ("launch_date", OPT, "First sale date; not-yet-launched SKUs get no demand", "2026-08-01"),
        ("season_end", OPT, "Last selling day of the season for this style (YYYY-MM-DD)", "2027-02-28"),
        ("is_core_style", OPT, "1 = replenished all year", "1"),
        ("is_non_product", OPT, "1 = bag, hanger, gift box (ignored)", "0"),
    ], {"color": "colour", "is_packaging": "is_non_product"}),
    "sales": (R, "Sales lines. Alternative: bills + bill_lines (joined on bill_id). Last 12+ weeks; a year or more for season effects.", [
        ("date", R, "Sale date", "2026-10-30"),
        ("store_id", R, "Store", "DEL-01"),
        ("sku_id", R, "SKU", "KRT-014-MAR-L"),
        ("qty", R, "Pieces (returns as negative)", "1"),
        ("net_value", REC, "Amount paid for the line incl. tax; gives real discount levels", "4749"),
    ], {"bill_date": "date"}),
    "stock_snapshot": (R, "Stock at close on the planning day, every store and warehouse.", [
        ("as_of_date", R, "Stock date; the plan is for the next day", "2026-10-31"),
        ("location_id", R, "Store or warehouse", "DEL-01"),
        ("sku_id", R, "SKU", "KRT-014-MAR-L"),
        ("on_hand", R, "Pieces on hand", "3"),
        ("in_transit", REC, "Pieces already shipped to this location", "0"),
        ("oldest_stock_received_on", REC, "When the oldest piece arrived; drives ageing", "2026-05-12"),
        ("stock_status", OPT, "sellable / damaged / alteration / return_qc… (one row per status)", "sellable"),
    ], {}),
    "stock_weekly": (REC, "Weekly in-stock record. Lets the plan tell 'no demand' from 'no stock' (hidden stockouts).", [
        ("week_ending", R, "Week end date", "2026-10-31"),
        ("store_id", R, "Store", "DEL-01"),
        ("sku_id", R, "SKU", "KRT-014-MAR-L"),
        ("days_in_stock", R, "Days that week with at least one piece (0-7)", "5"),
        ("closing_on_hand", OPT, "Pieces at week end; cross-checks the snapshot", "2"),
    ], {}),
    "inbound": (REC, "Deliveries due into warehouses.", [
        ("expected_date", R, "Due date", "2026-11-02"),
        ("sku_id", R, "SKU", "KRT-014-MAR-L"),
        ("qty", R, "Pieces", "60"),
        ("warehouse_id", OPT, "Receiving warehouse; default = the only/first warehouse", "WH-NORTH"),
    ], {"location_id": "warehouse_id"}),
    "lanes": (REC, "Lead time and cost of moving stock. From/To: a location id, city:<name>, zone:<name> or *. Most specific row wins.", [
        ("from", R, "Origin", "WH-NORTH"),
        ("to", R, "Destination", "zone:North"),
        ("lead_days", R, "Days door to door", "3"),
        ("cost_per_unit", REC, "Freight per piece", "35"),
        ("cost_per_trip", REC, "Fixed cost per dispatch (vehicle, courier minimum, paperwork)", "900"),
    ], {}),
    "calendar": (OPT, "Peak days and events by region (weddings, festivals, sale days).", [
        ("date", R, "Date", "2026-11-08"),
        ("region", R, "Region; joins to stores.region", "Delhi NCR"),
        ("event", OPT, "Event name (blank on normal days)", "Diwali"),
        ("is_peak_day", OPT, "1 = peak selling day", "0"),
    ], {"state": "region", "festival": "event", "is_wedding_date": "is_peak_day"}),
    "min_display": (OPT, "Minimum pieces each store tier shows per category.", [
        ("tier", R, "Store tier", "A"),
        ("category", R, "Category or display group", "Kurta Set"),
        ("min_display_units", R, "Pieces", "60"),
    ], {}),
    "protected_styles": (OPT, "Styles a store never gives away (e.g. a flagship's best sellers).", [
        ("store_id", R, "Store", "DEL-01"),
        ("style_id", R, "Style", "KRT-014"),
    ], {}),
    "managers": (OPT, "Area managers who receive alerts.", [
        ("manager_id", R, "Manager code", "AM-01"),
        ("name", R, "Name", "A. Sharma"),
        ("stores", R, "Store ids, comma separated", "DEL-01, DEL-02"),
        ("max_alerts_per_week", OPT, "How many alerts they will read", "5"),
    ], {"area_manager_id": "manager_id"}),
    "size_sets": (OPT, "Core sizes per category. If missing they are worked out from sales.", [
        ("category", R, "Category", "Kurta Set"),
        ("size", R, "Size", "L"),
        ("is_core", R, "1 = must stay together on the shelf", "1"),
    ], {}),
}
# old/other names for whole sheets
TABLE_ALIASES = {"warehouse_inbound": "inbound", "flagship_protected": "protected_styles",
                 "area_managers": "managers", "locations": "stores", "skus": "items", "products": "items"}
# sheets that are read but never needed: kept out of the engine on purpose (personal data)
IGNORED = {"salespeople", "roster_daily", "footfall_daily", "customers", "rules", "data_dictionary", "receipts"}


def guide_rows():
    rows = []
    for t, (need, purpose, cols, _) in TABLES.items():
        for c, cneed, meaning, ex in cols:
            rows.append(dict(sheet=f"{t}.csv", sheet_need=need, column=c, column_need=cneed, meaning=meaning, example=ex))
    return rows


def template_csvs():
    """One header row + one example row per sheet, for the downloadable template pack."""
    out = {}
    for t, (_, _, cols, _) in TABLES.items():
        hdr = ",".join(c for c, *_ in cols)
        ex = ",".join(f'"{e}"' if "," in e else e for *_, e in cols)
        out[f"{t}.csv"] = hdr + "\n" + ex + "\n"
    out["bills.csv (alternative to sales)"] = None
    return {k: v for k, v in out.items() if v}
