# StockFlow data guide

Upload CSV files, one Excel workbook (one tab per sheet) or a zip. Column names are not case-sensitive.
Dates as YYYY-MM-DD. IDs are text: keep leading zeros. Add an optional `config.json` for brand settings.

**Minimum to start:** `stores`, `items`, `sales`, `stock_snapshot`. **Strongly recommended:** `stock_weekly` (tells *no demand* from *no stock*), `inbound`, `lanes` (real freight costs) and `cost_price` in `items` (real margins).

| Sheet | Need | What it is for |
|---|---|---|
| `stores.csv` | required | Every selling location. Outlet stores take end-of-season stock and are never refilled. |
| `warehouses.csv` | optional | Warehouses. If missing, any stock location that is not a store is a warehouse. |
| `items.csv` | required | One row per SKU (style + colour + size). |
| `sales.csv` | required | Sales lines. Alternative: bills + bill_lines (joined on bill_id). Last 12+ weeks; a year or more for season effects. |
| `stock_snapshot.csv` | required | Stock at close on the planning day, every store and warehouse. |
| `stock_weekly.csv` | recommended | Weekly in-stock record. Lets the plan tell 'no demand' from 'no stock' (hidden stockouts). |
| `inbound.csv` | recommended | Deliveries due into warehouses. |
| `lanes.csv` | recommended | Lead time and cost of moving stock. From/To: a location id, city:<name>, zone:<name> or *. Most specific row wins. |
| `calendar.csv` | optional | Peak days and events by region (weddings, festivals, sale days). |
| `min_display.csv` | optional | Minimum pieces each store tier shows per category. |
| `protected_styles.csv` | optional | Styles a store never gives away (e.g. a flagship's best sellers). |
| `managers.csv` | optional | Area managers who receive alerts. |
| `size_sets.csv` | optional | Core sizes per category. If missing they are worked out from sales. |

Customer and staff columns (customer_id, phone, email, salesperson_id…) are not needed and are dropped on load. Sheets such as salespeople, roster or footfall are ignored.

## stores.csv (required)

Every selling location. Outlet stores take end-of-season stock and are never refilled.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `store_id` | required | Store code, used in every other sheet | DEL-01 |
| `city` | required | City | Delhi |
| `region` | required | State or region; joins to calendar.region | Delhi NCR |
| `zone` | required | Group of regions for transport (North, South…) | North |
| `tier` | recommended | Store grade (any labels; best first in settings.tier_order) | A |
| `capacity_units` | recommended | Most pieces the store can hold | 900 |
| `opened_on` | optional | Opening date (YYYY-MM-DD); new stores borrow history from similar stores | 2021-04-01 |
| `store_type` | optional | store (default) or outlet | store |
| `home_warehouse` | optional | Warehouse that normally serves it; default = quickest lane | WH-NORTH |
| `manager_id` | optional | Area manager; joins to managers | AM-01 |

Also accepted: `stock_capacity_units` for `capacity_units`, `state` for `region`, `area_manager_id` for `manager_id`

## warehouses.csv (optional)

Warehouses. If missing, any stock location that is not a store is a warehouse.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `warehouse_id` | required | Warehouse code as used in stock_snapshot.location_id | WH-NORTH |
| `city` | optional | City | Delhi |
| `zone` | optional | Zone | North |

Also accepted: `location_id` for `warehouse_id`

## items.csv (required)

One row per SKU (style + colour + size).

| Column | Need | Meaning | Example |
|---|---|---|---|
| `sku_id` | required | SKU code | KRT-014-MAR-L |
| `style_id` | required | Design code, shared by all its sizes and colours | KRT-014 |
| `style_name` | required | Design name | Silk Kurta Set |
| `category` | required | Category (any labels) | Kurta Set |
| `size` | required | Size label | L |
| `mrp` | required | Tag price incl. tax | 4999 |
| `cost_price` | recommended | Landed cost per piece; needed for true margins | 1850 |
| `colour` | recommended | Colour; a style in one colour is one 'option' for size sets | Maroon |
| `fit` | optional | Fit or cut | Regular |
| `launch_date` | optional | First sale date; not-yet-launched SKUs get no demand | 2026-08-01 |
| `season_end` | optional | Last selling day of the season for this style (YYYY-MM-DD) | 2027-02-28 |
| `is_core_style` | optional | 1 = replenished all year | 1 |
| `is_non_product` | optional | 1 = bag, hanger, gift box (ignored) | 0 |

Also accepted: `color` for `colour`, `is_packaging` for `is_non_product`

## sales.csv (required)

Sales lines. Alternative: bills + bill_lines (joined on bill_id). Last 12+ weeks; a year or more for season effects.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `date` | required | Sale date | 2026-10-30 |
| `store_id` | required | Store | DEL-01 |
| `sku_id` | required | SKU | KRT-014-MAR-L |
| `qty` | required | Pieces (returns as negative) | 1 |
| `net_value` | recommended | Amount paid for the line incl. tax; gives real discount levels | 4749 |

Also accepted: `bill_date` for `date`

## stock_snapshot.csv (required)

Stock at close on the planning day, every store and warehouse.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `as_of_date` | required | Stock date; the plan is for the next day | 2026-10-31 |
| `location_id` | required | Store or warehouse | DEL-01 |
| `sku_id` | required | SKU | KRT-014-MAR-L |
| `on_hand` | required | Pieces on hand | 3 |
| `in_transit` | recommended | Pieces already shipped to this location | 0 |
| `oldest_stock_received_on` | recommended | When the oldest piece arrived; drives ageing | 2026-05-12 |
| `stock_status` | optional | sellable / damaged / alteration / return_qc… (one row per status) | sellable |

## stock_weekly.csv (recommended)

Weekly in-stock record. Lets the plan tell 'no demand' from 'no stock' (hidden stockouts).

| Column | Need | Meaning | Example |
|---|---|---|---|
| `week_ending` | required | Week end date | 2026-10-31 |
| `store_id` | required | Store | DEL-01 |
| `sku_id` | required | SKU | KRT-014-MAR-L |
| `days_in_stock` | required | Days that week with at least one piece (0-7) | 5 |
| `closing_on_hand` | optional | Pieces at week end; cross-checks the snapshot | 2 |

## inbound.csv (recommended)

Deliveries due into warehouses.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `expected_date` | required | Due date | 2026-11-02 |
| `sku_id` | required | SKU | KRT-014-MAR-L |
| `qty` | required | Pieces | 60 |
| `warehouse_id` | optional | Receiving warehouse; default = the only/first warehouse | WH-NORTH |

Also accepted: `location_id` for `warehouse_id`

## lanes.csv (recommended)

Lead time and cost of moving stock. From/To: a location id, city:<name>, zone:<name> or *. Most specific row wins.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `from` | required | Origin | WH-NORTH |
| `to` | required | Destination | zone:North |
| `lead_days` | required | Days door to door | 3 |
| `cost_per_unit` | recommended | Freight per piece | 35 |
| `cost_per_trip` | recommended | Fixed cost per dispatch (vehicle, courier minimum, paperwork) | 900 |

## calendar.csv (optional)

Peak days and events by region (weddings, festivals, sale days).

| Column | Need | Meaning | Example |
|---|---|---|---|
| `date` | required | Date | 2026-11-08 |
| `region` | required | Region; joins to stores.region | Delhi NCR |
| `event` | optional | Event name (blank on normal days) | Diwali |
| `is_peak_day` | optional | 1 = peak selling day | 0 |

Also accepted: `state` for `region`, `festival` for `event`, `is_wedding_date` for `is_peak_day`

## min_display.csv (optional)

Minimum pieces each store tier shows per category.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `tier` | required | Store tier | A |
| `category` | required | Category or display group | Kurta Set |
| `min_display_units` | required | Pieces | 60 |

## protected_styles.csv (optional)

Styles a store never gives away (e.g. a flagship's best sellers).

| Column | Need | Meaning | Example |
|---|---|---|---|
| `store_id` | required | Store | DEL-01 |
| `style_id` | required | Style | KRT-014 |

## managers.csv (optional)

Area managers who receive alerts.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `manager_id` | required | Manager code | AM-01 |
| `name` | required | Name | A. Sharma |
| `stores` | required | Store ids, comma separated | DEL-01, DEL-02 |
| `max_alerts_per_week` | optional | How many alerts they will read | 5 |

Also accepted: `area_manager_id` for `manager_id`

## size_sets.csv (optional)

Core sizes per category. If missing they are worked out from sales.

| Column | Need | Meaning | Example |
|---|---|---|---|
| `category` | required | Category | Kurta Set |
| `size` | required | Size | L |
| `is_core` | required | 1 = must stay together on the shelf | 1 |

## Also accepted

- `bills.csv` (bill_id, bill_date, store_id) + `bill_lines.csv` (bill_id, sku_id, qty, selling_price) instead of `sales.csv`.
- `lead_times.csv` with sentence routes (e.g. 'Warehouse X to store, North zone') instead of `lanes.csv`; freight then comes from the settings.
- Sheet names `warehouse_inbound`, `flagship_protected`, `area_managers` for `inbound`, `protected_styles`, `managers`.

## config.json (optional)

Any of these sections, only the values you want to change:

```json
{
  "brand": {"name": "Your Brand", "currency": "₹", "number_style": "indian"},
  "planning": {"tier_order": ["A", "B", "C"], "approval_value": 100000,
               "category_groups": {"Accessories": ["Stole", "Mala"]}},
  "size_sets": {"core_sizes": {"Kurta Set": ["M", "L", "XL"]}},
  "economics": {"handling_per_unit": 15, "min_trip_units": 4, "default_cost_ratio": 0.4},
  "ageing": {"age_days": 120, "pullback_max_units_per_store": 25, "pullback_max_value_per_store": 150000},
  "season": {"peak_label": "wedding date", "event_label": "festival week"}
}
```

All settings and their defaults are in `schema.py` (DEFAULTS).
