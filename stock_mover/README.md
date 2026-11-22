# StockFlow: daily stock moves for multi-store apparel brands

Upload yesterday's sales and stock and get back a short list of **trips** to approve. Each trip has a one-line
reason, what it gains and what it costs to move. StockFlow is built for ethnic and occasion wear brands with about
5 to 100 stores, one or more warehouses and optional outlet stores. It contains no brand names, store grades,
category names or warehouse codes; everything comes from your sheets and an optional `config.json`.

```
pip install -r requirements.txt
python make_demo_data.py            # builds the fictional "Demo Threads" pack in data/demo
streamlit run app.py                # press "Try the demo brand", or upload your own pack
python engine.py --data <folder or workbook> [--config brands/x.json] --out output   # command line / scheduler
```

## What it does each day

| Step | What happens |
|---|---|
| Clean | Sheets are checked and common column names mapped. Customer and staff columns are dropped. Stock records that disagree, negative stock, and damaged, alteration or QC stock are flagged and never moved. |
| Demand | Selling speed counts only the days a size was on the shelf. New stores and new launches borrow from similar stores. Peak days and event weeks come from your calendar. |
| Plan | **Warehouse → store** from any number of warehouses (home warehouse first). **Size-set completion**: an option on display that is missing a core size gets it. **Minimum display** top-ups. **Store → store** transfers, preferring stores that already have a trip to the same destination. **Old stock by option**: broken size runs move together to one store or come back together. **Capped returns** per store (pieces and value), with the rest queued. **End of season**: no refill lands after it; leftovers go to an outlet or the warehouse. |
| Money | Every line is valued: expected extra sales × margin (net of real discount, tax and cost), plus markdown and holding cost avoided, minus freight, handling and a share of the trip cost. Lines and trips that don't pay are dropped. Rule-driven display top-ups always stay. |
| Approve | About 25 decisions cover the day. One-line clubbed reasons: *Refill before it runs out · Stock up for the peak ahead · Complete broken size sets · Fill minimum display · Top up to normal cover · Old stock to faster-selling stores · Reunite broken size runs · Return slow stock · Clear end-of-season / dead stock*. |
| Hand-off | Generic transfer-order CSV, one TO per trip, for any ERP or WMS. Release and receipt tracking, with variance by TO line. |
| Check | 17 rules are re-checked on the final plan, and the run fails if any breaks. |

## Data

See **DATA_GUIDE.md**. You can also open the **Data guide** tab in the app, which has downloadable blank templates.
The minimum is four sheets: stores, items, sales, stock_snapshot.

## Sign-in and roles

Users go in `.streamlit/secrets.toml` (see `secrets.example.toml`). Make a password hash with `python auth.py`.
There are three roles. Viewers see the plan. Approvers can also approve trips, release transfer orders and record
receipts. Admins can also change settings and accept feedback-tuned settings. A user can be limited to some stores.
With no users configured, the app shows an "open demo mode" warning on every page. Configure users before loading
a brand's real data.

## AI answers (optional)

The Ask tab works offline by default. An admin can turn on AI answers with an Anthropic API key after confirming that
sending the plan report is allowed. Only plan figures are sent; customer and staff data is never loaded. Cover this
in the brand's data-processing agreement first.

## Files

`engine.py` planning engine · `schema.py` settings and sheet definitions · `app.py` Streamlit app · `insights.py` ·
`chat.py` · `feedback.py` (run log, ratings, released TOs, receipts in `./workspace`) · `auth.py` · `brands/*.json`
settings profiles · `make_demo_data.py` fictional demo pack.
