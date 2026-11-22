# What changed from the hackathon Stock Mover

Before and after numbers come from the same hackathon data pack (20 stores, 1 warehouse, plan for 1 Nov 2026).
The "before" numbers come from the original app's own sample output.

| Downside | Fix | Before → after |
|---|---|---|
| Too many tiny moves | Lines are grouped into trips, and every trip pays a fixed dispatch cost. Lines and trips that cost more than they earn are dropped. Store-to-store trips under 4 pieces are held back unless urgent. Donors that already have a trip to the same store are preferred, and each store packs at most 3 trips a day. | 1,288 lines on 186 routes → **71 trips** (938 lines) |
| No cost of movement | Freight per piece and per trip comes from `lanes.csv` (any from/to: id, city, zone, type), with defaults by route type. Handling per piece is added. | Every line and trip shows gain, cost and net |
| No opportunity cost | Each line is valued as Poisson-expected extra sales × real margin (after discount, tax and cost), minus sales the giving store loses, plus markdown and holding cost avoided on old stock. The Money tab shows what not acting would cost, what is still at risk where no stock exists, and the monthly cost of old stock left in stores. | Model estimate: ₹69.7 L gain for ₹2.3 L moving cost; ₹71.7 L margin still at risk; old stock costs ~₹8.5 L/month |
| Sizes moved one at a time, breaking size sets | Core sizes come per category from sales, or from `size_sets.csv` / config. Options on display that are missing a core size get it first. A giving store never gives its last core size of a selling option. Broken runs of old stock move together to one store that sells the option and lacks those sizes, or come back together. Broken sets left after the plan are reported to buying. | New reasons: "Complete broken size sets", "Reunite broken size runs" |
| Too much pulled back in one go | Returns are capped per store per run: 25 pieces, 5% of stock and ₹1.5 L at MRP (all configurable). The most valuable and oldest go first; the rest is queued for later runs. | 353 pcs / ₹57.3 L in one day → **151 pcs / ₹26.3 L**, at most ₹1.5 L per store; 235 pcs queued |
| No colour, fit, returns, damage or season end | Colour and fit are optional item columns; option = style + colour + fit. `stock_status` keeps alteration, damaged and return-QC stock out of the plan and reports it. `season_end` stops refills that would land after the season. Leftover stock goes to an outlet (`store_type = outlet`) or the warehouse. | New "Clear end-of-season / dead stock" reason; outlets supported |
| Approval was only a CSV | One transfer order per trip in a generic ERP/WMS import format. "Release" logs the orders, and a receipts upload gives variance and late/short status per line on the Track tab. | Approve → release → track |
| No login; data and API privacy | Sign-in with hashed passwords and three roles (viewer / approver / admin), optionally limited to certain stores. An "open demo mode" warning shows when no users are set. Customer and staff columns are dropped on load. AI answers are off by default; an admin turns them on after confirming the data-processing agreement allows it. | |
| Hardcoded for one brand | All brand names, tiers (S++…), categories, the single warehouse and the lead-time sentences are gone. Any number of warehouses, any tier labels (order set in config), category display groups, currency and number style come from settings. Protected styles apply to any store listed. Old sheet and column names are still read. | Same engine runs the fictional demo brand and the hackathon pack |
| Similar reasons repeated line by line | Every line has one of 9 broad reasons. Each trip gets a single clubbed line, e.g. "Refill before it runs out (84 pcs), stock up for the peak ahead (44 pcs), complete broken size sets (2 pcs). Gains ₹3.1 L for ₹7,950 of moving cost." Routine approvals are one decision per reason. | 26 decisions for the whole day |

All 17 rule checks pass on both packs. On the hackathon pack, an independent recheck against the raw CSVs found no store
giving more than it physically holds, no warehouse over-sending, and no protected style given away.

## Not done (needs a live brand)
- Direct API push into a specific ERP or WMS. The CSV format is generic; each ERP needs its own connector.
- Calibrating the economics (holding rates, markdown per month, recovery uplift) against a brand's actual results.
- Replacing the hackathon sample pack: that data belongs to Vedant Fashions and is not in this package. Use the fictional demo pack for sales demos.
