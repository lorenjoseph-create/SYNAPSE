"""StockFlow app: upload a brand's data pack, get the day's stock moves, their money, and the trips to approve.

Start:  streamlit run app.py
"""
import io
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import chat
import engine as E
import feedback as fb
import insights as ins
import schema

HERE = Path(__file__).parent
BRANDS = sorted((HERE / "brands").glob("*.json"))
DEMO = HERE / "data" / "demo"
st.set_page_config(page_title="StockFlow · stock planning for apparel retail", page_icon="📦", layout="wide",
                   initial_sidebar_state="collapsed")


def css(color):
    st.markdown(f"""<style>
:root{{--p:{color};--line:#e4e1dc;--grey:#5f6368}}
.block-container{{padding-top:2.6rem;padding-bottom:4rem;max-width:1180px}}
.sf-head{{display:flex;align-items:center;gap:.7rem;padding:.4rem 0 .8rem;margin-bottom:1rem;border-bottom:1px solid var(--line)}}
.sf-head .nm{{font-weight:700;font-size:1.4rem}} .sf-head .nm span{{color:var(--p)}}
.sf-head .tag{{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--grey)}}
.sf-head .for{{margin-left:auto;font-size:.8rem;color:var(--grey)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.6rem;margin:.3rem 0 1rem}}
.kpi{{border:1px solid var(--line);border-top:3px solid var(--p);border-radius:6px;padding:.75rem .9rem;background:#fff}}
.kpi .l{{font-size:.68rem;color:var(--grey);text-transform:uppercase;letter-spacing:.1em;font-weight:600}}
.kpi .v{{font-size:1.5rem;font-weight:700;margin:.15rem 0;font-variant-numeric:tabular-nums}}
.kpi .s{{font-size:.76rem;color:var(--grey)}}
.ins{{border:1px solid var(--line);border-left:4px solid var(--c);padding:.6rem .9rem;margin:.5rem 0;border-radius:6px;background:#fff;line-height:1.5}}
.warn{{background:#fff4e5;border:1px solid #f0c27b;border-radius:6px;padding:.5rem .8rem;font-size:.85rem;margin-bottom:.8rem}}
.stTabs [data-baseweb="tab-list"]{{overflow-x:auto;flex-wrap:nowrap}}
@media (max-width:640px){{.block-container{{padding-left:.7rem;padding-right:.7rem}}.kpis{{grid-template-columns:1fr 1fr}}.kpi .v{{font-size:1.15rem}}}}
</style>""", unsafe_allow_html=True)


def kpi_cards(items):
    st.markdown('<div class="kpis">' + "".join(f'<div class="kpi"><div class="l">{l}</div><div class="v">{v}</div><div class="s">{s}</div></div>'
                                                for l, v, s in items) + "</div>", unsafe_allow_html=True)


def insight(text, color):
    import re
    st.markdown(f'<div class="ins" style="--c:{color}">{re.sub(r"[*][*](.+?)[*][*]", r"<b>\1</b>", text)}</div>', unsafe_allow_html=True)


def plot(fig, h=320, legend=True):
    fig.update_layout(height=h, margin=dict(l=4, r=4, t=44, b=4), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", yanchor="top", y=-0.2, x=0, title="") if legend else None, showlegend=legend)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def header(cfg):
    st.markdown(f'<div class="sf-head"><div style="font-size:1.9rem">📦</div><div><div class="nm">Stock<span>Flow</span></div>'
                f'<div class="tag">Daily stock planning for apparel retail</div></div>'
                f'<div class="for">{cfg["brand"]["name"]}</div></div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------------- sign in
USERS = auth.users(st.secrets if hasattr(st, "secrets") else None)
if USERS and "user" not in st.session_state:
    css("#2F5D62")
    header(schema.DEFAULTS)
    with st.form("login"):
        u = st.text_input("User name")
        p = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            rec = USERS.get(u.strip())
            if rec and auth.check_password(p, rec.get("password", "")):
                st.session_state.user, st.session_state.role = u.strip(), rec.get("role", "viewer")
                st.session_state.scope = [s.strip() for s in str(rec.get("stores", "")).split(",") if s.strip()]
                st.rerun()
            st.error("User name or password not recognised.")
    st.stop()
USER = st.session_state.get("user", "demo")
ROLE = st.session_state.get("role", "admin" if not USERS else "viewer")
OPEN = not USERS


# ----------------------------------------------------------------------------- data in
def unpack(uploads, dest):
    cfg_over = None
    for f in uploads:
        name, data = Path(f.name).name, f.getvalue()
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for m in z.infolist():
                    p = Path(m.filename)
                    if m.is_dir() or p.is_absolute() or ".." in p.parts:
                        continue
                    if p.suffix.lower() in (".csv", ".xlsx", ".xlsm"):
                        out = dest / "zip" / p
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(z.read(m))
                    elif p.name.lower() == "config.json":
                        cfg_over = json.loads(z.read(m))
        elif name.lower().endswith((".csv", ".xlsx", ".xlsm")):
            (dest / name).write_bytes(data)
        elif name.lower() == "config.json" or name.lower().endswith(".json"):
            cfg_over = json.loads(data)
    return cfg_over


def template_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, txt in schema.template_csvs().items():
            z.writestr(f"stockflow_template/{n}", txt)
        z.writestr("stockflow_template/config.json", json.dumps({k: schema.DEFAULTS[k] for k in ("brand", "planning", "economics", "season")}, indent=2))
        z.writestr("stockflow_template/DATA_GUIDE.csv", pd.DataFrame(schema.guide_rows()).to_csv(index=False))
    return buf.getvalue()


def data_guide():
    rows = pd.DataFrame(schema.guide_rows())
    need = {"required": "🔴 required", "recommended": "🟠 recommended", "optional": "⚪ optional"}
    sheets = rows.drop_duplicates("sheet")[["sheet", "sheet_need"]].assign(
        purpose=[schema.TABLES[s.replace(".csv", "")][1] for s in rows.drop_duplicates("sheet").sheet])
    sheets["sheet_need"] = sheets.sheet_need.map(need)
    st.markdown("**Minimum to start (4 sheets):** stores, items, sales, stock_snapshot. **Recommended:** stock_weekly (catches hidden "
                "stockouts), inbound, lanes (real freight costs), cost_price in items. Everything else switches on extra features. "
                "CSV files, one Excel workbook with one tab per sheet, or a zip of either. Column names are not case-sensitive; "
                "common alternatives (state→region, bills+bill_lines→sales…) are understood. Customer and staff columns are dropped on load.")
    st.dataframe(sheets, hide_index=True, width="stretch",
                 column_config={"sheet_need": "need", "purpose": st.column_config.TextColumn(width="large")})
    with st.expander("Every column"):
        st.dataframe(rows.assign(column_need=rows.column_need.map(need)), hide_index=True, width="stretch", height=420)
    st.download_button("⬇️ Download blank templates + data guide (zip)", template_zip(), "stockflow_templates.zip", key=f"tpl{id(rows)}")


def run_engine(source, cfg):
    return E.run(source, Path(tempfile.mkdtemp(prefix="stockflow_out_")), cfg)


def data_panel():
    c1, c2 = st.columns([3, 2])
    ups = c1.file_uploader("Upload your data pack: a zip, an Excel workbook or the CSV files (add config.json for brand settings)",
                           type=["zip", "csv", "xlsx", "xlsm", "json"], accept_multiple_files=True, key="ups")
    brand = c2.selectbox("Settings profile", ["Standard settings"] + [b.stem for b in BRANDS], key="brand_pick")
    base = schema.load_config(HERE / "brands" / f"{brand}.json" if brand != "Standard settings" else None, fb.load_settings())
    with st.expander("⚙️ Settings" + ("" if auth.can(ROLE, "admin") else " (admin only)")):
        dis = not auth.can(ROLE, "admin")
        a, b, c = st.columns(3)
        base["brand"]["name"] = a.text_input("Brand name", base["brand"]["name"], disabled=dis)
        base["brand"]["number_style"] = b.selectbox("Number style", ["indian", "international"],
                                                    index=0 if base["brand"]["number_style"] == "indian" else 1, disabled=dis)
        base["brand"]["currency"] = c.text_input("Currency symbol", base["brand"]["currency"], disabled=dis)
        for (sec, k), (lo, hi, step, name) in schema.TUNABLE.items():
            base[sec][k] = st.slider(name, lo, hi, int(base[sec][k]), step=step, key=f"s_{sec}_{k}", disabled=dis)
        a, b = st.columns(2)
        base["planning"]["approval_value"] = a.number_input("Line value needing sign-off (MRP)", value=int(base["planning"]["approval_value"]),
                                                            step=10000, disabled=dis)
        base["planning"]["trip_approval_value"] = b.number_input("Trip value needing sign-off (MRP)",
                                                                 value=int(base["planning"]["trip_approval_value"]), step=50000, disabled=dis)
        a, b, c = st.columns(3)
        base["economics"]["handling_per_unit"] = a.number_input("Handling cost per piece", value=float(base["economics"]["handling_per_unit"]), disabled=dis)
        base["economics"]["default_cost_ratio"] = b.number_input("Cost as share of MRP (if no cost_price)", value=float(base["economics"]["default_cost_ratio"]),
                                                                 min_value=0.05, max_value=0.95, step=0.05, disabled=dis)
        base["economics"]["markdown_per_month"] = c.number_input("Value old stock loses per month", value=float(base["economics"]["markdown_per_month"]),
                                                                 min_value=0.0, max_value=0.3, step=0.01, disabled=dis)
        st.caption("Freight per lane comes from lanes.csv; routes it doesn't cover use the defaults in the config. Business rules never relax.")
    with st.expander("📄 What data do I need?"):
        data_guide()
    a, b = st.columns(2)
    go_btn = a.button("▶️ Plan with my data", type="primary", disabled=not ups, width="stretch")
    demo = b.button("✨ Try the demo brand", width="stretch", disabled=not DEMO.exists())
    if go_btn or demo:
        tmp = Path(tempfile.mkdtemp(prefix="stockflow_in_"))
        try:
            if go_btn:
                over = unpack(ups, tmp)
                cfg = schema.merge(base, over) if over else base
                source, label = tmp, ", ".join(f.name for f in ups)[:120]
            else:
                cfg = schema.load_config(HERE / "brands" / "demo.json", fb.load_settings())
                source, label = DEMO, "demo brand (fictional)"
            with st.spinner("Checking the data, measuring selling speed, planning and costing every move…"):
                R = run_engine(source, cfg)
            st.session_state.R, st.session_state.label = R, label
            st.session_state.run_id = fb.log_run(R, label, USER)
            st.session_state.chat = []
            st.session_state.pop("ctx", None)
            st.rerun()
        except E.DataError as e:
            st.error("**The data isn't quite what StockFlow needs.** Please fix:\n\n" + "\n".join(f"- {p}" for p in str(e).splitlines()))
        except Exception as e:  # noqa: BLE001
            st.error(f"Something went wrong while planning: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if "R" not in st.session_state:
    css("#2F5D62")
    header(schema.DEFAULTS)
    if OPEN:
        st.markdown('<div class="warn">🔓 <b>Open demo mode</b>: no sign-in is configured. Use the demo brand, or set up users '
                    '(see auth.py) before uploading real data.</div>', unsafe_allow_html=True)
    st.markdown("### Right stock, right store, right size: every morning")
    st.markdown("Upload yesterday's sales and stock. StockFlow returns a short list of **trips** to approve: refills before stores run out, "
                "broken size sets completed, old stock moved to where it sells, and slow stock brought back a few pieces at a time. "
                "Every move shows **what it earns and what it costs to move**, and moves that don't pay for themselves are left out.")
    data_panel()
    st.stop()

# ----------------------------------------------------------------------------- results
R = st.session_state.R
cfg = R["cfg"]
css(cfg["brand"].get("primary_color", "#2F5D62"))
M = lambda v: E.money(v, cfg)
pl, mv, ex, val, tr, O = R["pl"], R["moves"], R["issues"], R["checks"], R["trips"], R["opportunity"]
P, g, stores = R["P"], pl.g, pl.st
SCOPE = st.session_state.get("scope") or []
if SCOPE:  # a user limited to some stores sees only their trips
    mv = mv[mv.from_id.isin(SCOPE) | mv.to_id.isin(SCOPE)]
    tr = tr[tr.from_id.isin(SCOPE) | tr.to_id.isin(SCOPE)]
L = ins.leadership(R)
header(cfg)
if OPEN:
    st.markdown('<div class="warn">🔓 Open demo mode: anyone with the link can see this. Configure sign-in before using real data.</div>', unsafe_allow_html=True)
st.markdown(f"### Stock plan · {pl.run:%a %d %b %Y}")
st.caption(f"{st.session_state.label} · stock as of {P['today']:%d %b %Y} · {len(stores)} stores · warehouses {', '.join(pl.whs)} · "
           f"signed in as {USER} ({ROLE})")
with st.expander("📂 Load new data or change settings"):
    data_panel()

T = st.tabs(["⭐ Summary", "💰 Money", "🚚 Trips", "🔍 Problems", "🏬 Stores", "✅ Approve", "📦 Track", "💬 Ask", "📝 Feedback", "📄 Data guide", "ℹ️ Method"])
GCOL = dict(zip(E.GROUPS, ["#c0392b", "#e67e22", "#d35400", "#7f8c8d", "#2F5D62", "#8e44ad", "#9b59b6", "#34495e", "#16a085"]))

with T[0]:
    kpi_cards(L["kpis"])
    for color, text in L["insights"]:
        insight(text, color)
    st.markdown("#### Decisions needed")
    for d in L["decisions"] or ["Nothing beyond approving today's trips."]:
        st.markdown(f"- {d}")
    passed = int((val.status == "PASS").sum())
    st.caption(f"Plan health: {passed}/{len(val)} rules passed.")
    if R["xlsx"]:
        st.download_button("⬇️ Full plan (Excel)", Path(R["xlsx"]).read_bytes(), Path(R["xlsx"]).name, key="xl1")

with T[1]:
    st.markdown("#### What the plan earns, what it costs, and what is still being lost")
    kpi_cards([("Gained", M(O["benefit"]), "margin + markdown & holding avoided"), ("Moving cost", M(O["move_cost"]), "freight, handling, trips"),
               ("Net gain", M(O["net"]), f"{O['net'] / max(O['move_cost'], 1):.0f}× the cost"),
               ("Saved by skipping", M(max(O["skipped_cost"] - O["skipped_benefit"], 0)), f"{len(R['skipped']) - O['held_small']} moves not worth making")])
    by = O["by_group"]
    if len(by):
        d = by.melt("group", ["benefit", "move_cost"], var_name="", value_name="amount")
        d[""] = d[""].map({"benefit": "Gained", "move_cost": "Moving cost"})
        plot(px.bar(d, x="amount", y="group", color="", barmode="group", orientation="h", title="Gain vs cost by reason",
                    labels={"group": "", "amount": cfg["brand"]["currency"]}, color_discrete_sequence=["#2F5D62", "#c0392b"]), 360)
        st.dataframe(by.assign(**{c: by[c].map(M) for c in ("benefit", "move_cost", "net")}), hide_index=True, width="stretch")
    st.markdown("#### Opportunity cost of doing nothing")
    st.markdown(f"- If today's trips are **not** sent, the brand gives up about **{M(O['benefit'])}** (lost margin on stock-outs, markdowns "
                f"and holding cost on old stock).\n"
                f"- Even after the plan, **{M(O['still_at_risk'])}** of margin is at risk where no safe stock exists: that is the case for re-ordering.\n"
                f"- Old stock still in stores ({O['aged_left_pieces']:,} pieces) costs about **{M(O['aged_cost_per_month'])} a month** "
                f"in holding cost and lost value.")
    st.caption("How it is worked out: expected extra pieces sold (Poisson demand over lead time + cover, minus what the giving store "
               "would have sold) × margin (net price after real discount and tax − cost); old stock also earns the markdown and holding cost "
               "it avoids. Costs: freight per piece and per trip from lanes.csv, plus handling per piece.")
    sk = R["skipped"]
    if len(sk):
        with st.expander(f"Not worth moving · {len(sk)} lines"):
            st.dataframe(sk[["group", "from_id", "to_id", "style_name", "size", "qty", "benefit", "move_cost", "skip_why"]].round(0),
                         hide_index=True, width="stretch")

with T[2]:
    kpi_cards([("Trips", f"{len(tr)}", f"{(tr.from_id.isin(pl.whs)).sum()} from warehouses"), ("Lines", f"{len(mv):,}", "one size of one option"),
               ("Pieces", f"{int(mv.qty.sum()):,}", M(mv.value_mrp.sum()) + " at MRP"), ("Urgent trips", f"{int((tr.urgent > 0).sum())}", "a store runs out before a normal delivery")])
    st.markdown("Each trip has **one line** saying why it goes; the size-by-size detail is under it.")
    show = tr[["to_number", "from_id", "to_id", "pieces", "value_mrp", "net_benefit", "arrives", "reason"]].copy()
    show["value_mrp"], show["net_benefit"] = show.value_mrp.map(M), show.net_benefit.map(M)
    show["arrives"] = show.arrives.dt.strftime("%d %b")
    st.dataframe(show, hide_index=True, width="stretch", height=380, column_config={"reason": st.column_config.TextColumn("why", width="large")})
    pick = st.selectbox("Open a trip", tr.to_number, format_func=lambda t: f"{t} · " + " → ".join(tr.set_index("to_number").loc[t, ["from_id", "to_id"]]))
    if pick:
        key = tr.set_index("to_number").trip_key[pick]
        st.dataframe(mv[mv.trip_key == key][["group", "style_name", "colour", "size", "qty", "net_benefit", "reason"]].round(0),
                     hide_index=True, width="stretch")
    d = mv.groupby("group").qty.sum().reindex(E.GROUPS).dropna().reset_index()
    plot(px.bar(d, x="qty", y="group", orientation="h", color="group", color_discrete_map=GCOL, title="Pieces by reason", labels={"group": "", "qty": "pieces"}), 300, False)
    if len(tr):
        city = lambda x: x if x in pl.whs else stores.city[x]
        cc = tr.assign(fc=tr.from_id.map(city), tc=tr.to_id.map(city)).groupby(["fc", "tc"]).pieces.sum().reset_index()
        dot = ["digraph{rankdir=LR;bgcolor=transparent;node[shape=box style=\"rounded,filled\" fillcolor=\"#eef3f3\" fontname=Helvetica];"]
        dot += [f'"{w}"[fillcolor="#2F5D62" fontcolor=white];' for w in pl.whs]
        dot += [f'"{r.fc}"->"{r.tc}"[label="{r.pieces}" fontsize=10 penwidth={1 + min(r.pieces / cc.pieces.max() * 6, 6):.1f}];' for r in cc.itertuples()]
        st.graphviz_chart("\n".join(dot) + "}", width="stretch")
    if len(R["alerts"]):
        st.markdown("#### Area manager alerts")
        for name, grp in R["alerts"].groupby("manager"):
            with st.expander(name):
                for r in grp.itertuples():
                    st.markdown(f"{r.rank}. {r.alert}")

with T[3]:
    sev = {"High": "🔴", "Medium": "🟡", "Info": "🔵"}
    for kind, grp in ex.groupby("issue", sort=False):
        with st.container(border=True):
            st.markdown(f"**{sev.get(grp.severity.iloc[0], '')} {kind}** · {len(grp)} found" +
                        (f" · {M(grp.value.sum())}" if grp.value.sum() > 0 else ""))
            st.markdown(f"**What we did:** {grp.action.iloc[0]}")
            with st.expander("Details"):
                st.dataframe(grp[["store", "sku", "detail", "action"]], hide_index=True, width="stretch")
    sn = g[g.oh > 0].copy()
    if sn.age.notna().any():
        a = cfg["ageing"]["age_days"]
        sn["band"] = pd.cut(sn.age, [-1, 30, 90, a, 365, 100000], labels=["<1 mo", "1–3 mo", f"3 mo–{a}d", f"{a}d–1 yr", ">1 yr"])
        d = sn.groupby("band", observed=False).oh.sum().reset_index()
        plot(px.bar(d, x="band", y="oh", title="How long stock has been in stores", labels={"oh": "pieces", "band": ""},
                    color_discrete_sequence=["#8e44ad"]), 260, False)

with T[4]:
    allowed = [s for s in stores.index if not SCOPE or s in SCOPE]
    s = st.selectbox("Store", allowed, format_func=lambda x: f"{x} · {stores.city[x]} · {stores.tier[x]}")
    S = ins.store_summary(R).loc[s]
    kpi_cards([("Pieces now", f"{S.pieces_now:,}", "on shelf + on the way"), ("Coming in", f"{S.coming_in:,}", ""),
               ("Going out", f"{S.going_out:,}", ""), ("Shelves used after", f"{S.fill_after:.0%}" if S.fill_after == S.fill_after else "—", "")])
    for r in ex[ex.store == s].itertuples():
        st.warning(f"**{r.issue}:** {r.detail} → {r.action}")
    gs = g.loc[s]
    opt = gs.groupby("option").agg(sizes=("pos", lambda x: int((x > 0).sum())), of=("size", "count"), sells=("fd30", "sum"))
    core_miss = gs[gs.is_core & (gs.pos <= 0)].groupby("option").size()
    opt["missing core"] = core_miss.reindex(opt.index).fillna(0).astype(int)
    brk = opt[(opt.sizes >= 2) & (opt["missing core"] > 0)].sort_values("sells", ascending=False)
    st.markdown(f"**Size sets:** {len(brk)} selling options are missing a core size before the plan.")
    if len(brk):
        st.dataframe(brk.round(2).rename(columns={"sizes": "sizes in stock", "of": "sizes made", "sells": "pieces/day"}), width="stretch", height=220)
    cov = (gs.pos / gs.fd30).where(gs.fd30 > 0.01)
    band = pd.cut(cov, [-1, 7, 14, 30, 60, 1e9], labels=["<1 wk", "1–2 wk", "2–4 wk", "1–2 mo", "2+ mo"])
    d = band.value_counts().reindex(band.cat.categories).reset_index()
    d.columns = ["lasts", "sizes"]
    plot(px.bar(d, x="lasts", y="sizes", title="How long each size's stock lasts (before plan)", color_discrete_sequence=["#2F5D62"]), 260, False)
    for title, df in (("⬇️ Coming in", tr[tr.to_id == s]), ("⬆️ Going out", tr[tr.from_id == s])):
        with st.expander(f"{title} · {len(df)} trips"):
            st.dataframe(df[["to_number", "from_id", "to_id", "pieces", "reason"]], hide_index=True, width="stretch")

with T[5]:
    if not auth.can(ROLE, "approver"):
        st.info("Your role can view the plan but not approve it. Ask an approver.")
    pack = E.approval_pack(tr, cfg)
    st.markdown(f"#### The ten-minute approval\n**{len(pack)} decisions** cover all **{len(tr)} trips** ({len(mv):,} lines). "
                "Everything starts approved: untick what you don't want.")
    ok_to = set()
    for step, grp in pack.groupby("step", sort=True):
        st.markdown(f"**Step {step}** · {len(grp)} decisions · {int(grp.pieces.sum()):,} pieces · net {M(grp.net_benefit.sum())}")
        view = grp.assign(ok=True, value=grp.value_mrp.map(M), net=grp.net_benefit.map(M))[["ok", "decision", "trips", "pieces", "value", "net", "detail", "to_numbers"]]
        ed = st.data_editor(view, hide_index=True, width="stretch", key=f"pack_{step}", disabled=not auth.can(ROLE, "approver") or
                            ["decision", "trips", "pieces", "value", "net", "detail"], height=min(420, 38 + 35 * len(view)),
                            column_config={"to_numbers": None, "ok": st.column_config.CheckboxColumn("✓", width="small"),
                                           "detail": st.column_config.TextColumn("why", width="large")})
        for ids in ed[ed.ok].to_numbers:
            ok_to.update(ids.split(","))
    orders = R["orders"]
    orders = orders[orders.to_number.isin(ok_to) & orders.to_number.isin(tr.to_number)]
    st.success(f"Approved {orders.to_number.nunique()} of {len(tr)} trips · {int(orders.qty.sum()):,} pieces")
    if auth.can(ROLE, "approver"):
        a, b = st.columns(2)
        a.download_button("⬇️ Transfer orders for your ERP / WMS (CSV)", orders.to_csv(index=False).encode(),
                          f"transfer_orders_{pl.run:%Y-%m-%d}.csv", type="primary", width="stretch")
        if b.button("📤 Release to warehouse & track receipts", width="stretch", disabled=orders.empty):
            fb.release_orders(orders, USER, st.session_state.run_id)
            st.success(f"Released {orders.to_number.nunique()} transfer orders. Record receipts on the Track tab.")
        st.caption("The CSV is a generic transfer-order format (one TO per trip: to_number, line_no, from_location, to_location, sku_id, qty, "
                   "ship_date, expected_receipt). Most ERPs and WMSs import it directly or with a column mapping.")

with T[6]:
    st.markdown("#### Did the stock arrive?")
    st.caption("Released transfer orders are tracked here. Upload what stores actually received (to_number, sku_id, qty_received).")
    if auth.can(ROLE, "approver"):
        rc = st.file_uploader("Receipts file (CSV)", type=["csv"], key="rcpt")
        if rc is not None and st.button("Record receipts"):
            try:
                fb.add_receipts(pd.read_csv(rc, dtype=str), USER)
                st.success("Receipts recorded.")
            except ValueError as e:
                st.error(str(e))
    rec = fb.reconciliation()
    if rec.empty:
        st.info("No transfer orders released yet.")
    else:
        s_ = rec.groupby("status").agg(lines=("sku_id", "size"), pieces=("shipped", "sum")).reset_index()
        kpi_cards([(r.status, f"{int(r.lines)}", f"{int(r.pieces)} pieces") for r in s_.itertuples()])
        st.dataframe(rec, hide_index=True, width="stretch", height=380)
        st.download_button("⬇️ Receipt variance (CSV)", rec.to_csv(index=False).encode(), "receipt_variance.csv")

with T[7]:
    key = st.session_state.get("api_key")
    ai = chat.claude_available(key) and st.session_state.get("ai_ok", False)
    st.markdown(f"Ask about **this plan**. {'🤖 AI answers on.' if ai else '⚡ Quick answers (nothing leaves this server).'}")
    if auth.can(ROLE, "admin") and not ai:
        with st.expander("Turn on AI answers"):
            st.caption("Sends the plan report (trips, moves, issues, stock figures; never customer or staff data) to the AI provider. "
                       "Check this is covered by your data-processing agreement first.")
            k = st.text_input("Anthropic API key (blank = use server key)", type="password")
            if st.checkbox("I confirm sending the plan report to the AI provider is allowed") and st.button("Turn on"):
                if k:
                    st.session_state.api_key = k.strip()
                st.session_state.ai_ok = True
                st.rerun()
    st.session_state.setdefault("chat", [])
    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
    q = st.chat_input("Ask about stores, trips, money, problems…")
    if q:
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            if ai:
                st.session_state.setdefault("ctx", chat.build_context(R))
                a = chat.ask_claude(q, st.session_state.chat, st.session_state.ctx, cfg["brand"]["name"], key)
            else:
                a = chat.quick_answer(q, R)
            st.markdown(a)
        st.session_state.chat += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]

with T[8]:
    runs, r = fb.runs(), fb.ratings()
    with st.form("rate", clear_on_submit=True):
        st.markdown("#### Rate this plan once the stock has landed")
        stars = st.feedback("stars")
        c1, c2, c3 = st.columns(3)
        right = c1.select_slider("Were the moves right?", [1, 2, 3, 4, 5], 4)
        easy = c2.select_slider("Easy to understand?", [1, 2, 3, 4, 5], 4)
        time_ = c3.select_slider("Saved you time?", [1, 2, 3, 4, 5], 4)
        changes = st.multiselect("What should change next time?", list(fb.SUGGESTIONS))
        comment = st.text_area("Anything else?")
        if st.form_submit_button("Send feedback", type="primary"):
            if stars is None:
                st.warning("Please pick a star rating.")
            else:
                fb.add_rating(st.session_state.run_id, stars + 1, right, easy, time_, changes, comment, USER)
                st.success("Thank you!")
    if not r.empty:
        kpi_cards([("Average rating", f"{r.stars.mean():.1f} ★", f"{len(r)} reviews"), ("Moves right", f"{r.moves_right.mean():.1f}/5", ""),
                   ("Easy", f"{r.easy.mean():.1f}/5", ""), ("Saves time", f"{r.saves_time.mean():.1f}/5", "")])
    sug = fb.suggested_settings(cfg)
    if len(sug):
        st.markdown("#### Suggested settings from feedback")
        st.dataframe(sug.drop(columns="setting"), hide_index=True, width="stretch")
        if auth.can(ROLE, "admin") and st.button("✅ Use these from the next run", type="primary"):
            over = fb.load_settings()
            for row in sug.itertuples():
                sec, k = row.setting.split(".")
                over.setdefault(sec, {})[k] = int(row.suggested)
            fb.save_settings(over)
            st.success("Saved.")
        if auth.can(ROLE, "admin") and st.button("↩️ Reset to standard settings"):
            fb.reset_settings()
    if not runs.empty:
        with st.expander(f"Run history ({len(runs)})"):
            st.dataframe(runs[::-1], hide_index=True, width="stretch")

with T[9]:
    data_guide()

with T[10]:
    st.markdown("#### How StockFlow decides")
    st.graphviz_chart("""digraph{rankdir=TB;bgcolor=transparent;node[shape=box style="rounded,filled" fontname=Helvetica fillcolor="#eef3f3"];
data[label="Your sheets: sales · stock · deliveries · lanes · calendar"];clean[label="1. Check & clean (personal data dropped)"];
speed[label="2. Selling speed = sales ÷ days on the shelf"];season[label="3. Season: your peak days & events"];
need[label="4. Need: stock + on the way vs demand, size sets, season end"];plan[label="5. Plan: warehouse→store · store→store · old stock by option · capped returns · outlet"];
money[label="6. Value every line: margin, markdown, holding vs freight & handling"];batch[label="7. Group into trips; drop what doesn't pay"];
check[label="8. Re-check every rule" fillcolor="#fff3cd"];out[label="Trips to approve + transfer orders" fillcolor="#2F5D62" fontcolor=white];
data->clean->speed->need;season->need;need->plan->money->batch->check->out;}""", width="stretch")
    st.markdown("""
- **Option** = one style in one colour (and fit). Size sets are kept per option.
- **Core sizes** = the fewest sizes making up ~70% of a category's sales (or set them in size_sets.csv). An option on display with a core size missing gets that size first; a giving store never hands over its last core size.
- **Broken runs** of old stock (a few odd sizes) are moved together to one store that sells the option and lacks those sizes, or returned together.
- **Returns are capped** per store per run, so a store is never stripped in one day; the rest is queued.
- **Season end**: nothing is refilled to land after it; leftover goes to an outlet if you have one, else the warehouse.
- **Money**: every line is valued; lines and trips that cost more to move than they earn are left out (rule-driven display top-ups stay).
""")
    allp = (val.status == "PASS").all()
    (st.success if allp else st.error)("All rules passed." if allp else "Some rules failed. Don't send this plan.")
    for r_ in val.itertuples():
        st.markdown(f"{'✅' if r_.status == 'PASS' else '❌'} {r_.rule} <small>(checked {r_.checked})</small>", unsafe_allow_html=True)
    if pl.season.on and pl.season.lift:
        lift = pd.DataFrame(pl.season.lift).T.reset_index(names="region").melt("region", var_name="day", value_name="vs normal")
        plot(px.bar(lift, x="day", y="vs normal", color="region", barmode="group", title="Sales on special days (1.0 = normal)").add_hline(y=1, line_dash="dot"), 300)
