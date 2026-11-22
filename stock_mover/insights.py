"""Plain-language numbers and insights from a StockFlow run (shared by the app and the chatbot)."""
import pandas as pd

import engine as E


def m(R, v):
    return E.money(v, R["cfg"])


def store_summary(R):
    pl, mv = R["pl"], R["moves"]
    st, g = pl.st, pl.g
    now = g.groupby(level="store_id").pos.sum()
    inn = mv[mv.to_id.isin(st.index)].groupby("to_id").qty.sum()
    out = mv[mv.from_id.isin(st.index)].groupby("from_id").qty.sum()
    hs = E.hidden_stockouts(pl, R["weekly"], R["P"]["today"]).groupby("store_id").at_risk.sum()
    df = pd.DataFrame({"city": st.city, "tier": st.tier, "type": st.store_type, "capacity": st.cap.clip(upper=10**8),
                       "pieces_now": now, "coming_in": inn, "going_out": out, "hidden_stockout_value": hs}).fillna(0)
    df["after_plan"] = df.pieces_now + df.coming_in - df.going_out
    df["fill_after"] = (df.after_plan / df.capacity).where(df.capacity < 10**8)
    return df.astype({c: int for c in ["pieces_now", "coming_in", "going_out", "after_plan"]})


def leadership(R):
    """Key numbers, up to five insights, and the decisions that need a person."""
    pl, mv, ex, O, tr = R["pl"], R["moves"], R["issues"], R["opportunity"], R["trips"]
    st, it, cfg = pl.st, pl.it, R["cfg"]
    hs = E.hidden_stockouts(pl, R["weekly"], R["P"]["today"])
    sk = R["skipped"]
    kpis = [
        ("Net gain from the plan", m(R, O["net"]), f"{m(R, O['benefit'])} gained − {m(R, O['move_cost'])} moving cost"),
        ("Trips to dispatch", f"{len(tr)}", f"{len(mv):,} lines · {int(mv.qty.sum()):,} pieces"),
        ("Still at risk", m(R, O["still_at_risk"]), "margin with no safe stock anywhere"),
        ("Old stock idle cost", m(R, O["aged_cost_per_month"]) + "/mo", f"{O['aged_left_pieces']:,} old pieces left in stores"),
    ]
    ins = []
    if len(hs):
        s = hs.groupby("store_id").at_risk.sum().sort_values(ascending=False)
        top = s.index[0]
        ins.append(("#c0392b", f"**{top} ({st.city[top]}) is losing sales quietly.** {int((hs.store_id == top).sum())} sizes have been off "
                               f"its shelves for a month, about {m(R, s.iloc[0])} of sales in the next 30 days. Today's plan refills what stock allows."))
    by = O["by_group"].sort_values("net", ascending=False)
    if len(by):
        b = by.iloc[0]
        ins.append(("#2F5D62", f"**Biggest win: {b.group.lower()}.** {int(b.pieces):,} pieces earn {m(R, b.benefit)} for {m(R, b.move_cost)} of moving cost."))
    if len(sk):
        ins.append(("#7f8c8d", f"**{len(sk)} moves were left out on purpose.** They would cost {m(R, sk.move_cost.sum())} to move and earn only "
                               f"{m(R, sk.benefit.sum())}: mostly single pieces on long routes."))
    short = ex[ex.issue == "Warehouse shortfall"]
    if len(short):
        ins.append(("#e67e22", f"**Warehouses are short of {', '.join(short.sku.head(3))}.** {short.detail.iloc[0]}. "
                               f"{m(R, O['still_at_risk'])} of margin is at risk until supply arrives."))
    old = mv[mv.type.isin([E.T_AGE, E.T_PULL, E.T_OUT])]
    if len(old):
        ins.append(("#8e44ad", f"**{m(R, old.value_mrp.sum())} of old stock is being put to work.** {int(mv[mv.type == E.T_AGE].qty.sum())} pieces go "
                               f"where they sell, {int(mv[mv.type.isin([E.T_PULL, E.T_OUT])].qty.sum())} come back or go to clearance, a few per store at a time."))
    brk = ex[ex.issue == "Broken size sets after plan"]
    if len(brk) and len(ins) < 5:
        ins.append(("#d35400", f"**Broken size runs in {len(brk)} stores.** Selling options are missing core sizes that no warehouse holds; buying should re-order those sizes."))
    decisions = []
    sign = tr[tr.needs_approval]
    if len(sign):
        decisions.append(f"Sign off **{len(sign)} high-value trips** ({m(R, sign.value_mrp.sum())} at MRP) on the Approve tab.")
    for r in short.itertuples():
        decisions.append(f"Buying: speed up **{r.sku}** supply ({r.detail}).")
    hits = ex[ex.issue == "Sold-out hit"]
    if len(hits):
        decisions.append(f"Buying: re-order sold-out hits **{', '.join(hits.sku.head(5))}**. They ran out; they aren't slow.")
    for r in ex[ex.issue == "Dead style"].head(5).itertuples():
        decisions.append(f"Buying: stop re-ordering **{r.sku}**: no sales in the data.")
    for r in ex[ex.issue == "Stock records disagree"].head(5).itertuples():
        decisions.append(f"Operations: recount **{r.sku} at {r.store}**: the stock records disagree.")
    ns = ex[ex.issue == "Non-sellable stock"]
    if len(ns):
        decisions.append(f"Operations: clear alteration / QC / damaged stock in **{ns.store.nunique()} locations** ({m(R, ns.value.sum())} at MRP).")
    return dict(kpis=kpis, insights=ins[:5], decisions=decisions)
