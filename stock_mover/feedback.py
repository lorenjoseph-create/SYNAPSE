"""Run history, ratings, suggested settings, released transfer orders and receipts: simple files in ./workspace."""
import json
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

import schema

DIR = Path(__file__).resolve().parent / "workspace"
RUNS, RATINGS, SETTINGS = DIR / "runs.csv", DIR / "ratings.csv", DIR / "settings.json"
ORDERS, RECEIPTS = DIR / "released_transfer_orders.csv", DIR / "receipts.csv"

SUGGESTIONS = {
    "Send more stock to stores (shelves ran empty)": (("planning", "cover_days"), +1),
    "Send less stock (shelves got too full)": (("planning", "cover_days"), -1),
    "Treat stock as old sooner": (("ageing", "age_days"), -1),
    "Treat stock as old later": (("ageing", "age_days"), +1),
    "Move old stock only to faster-selling stores": (("ageing", "sell_within"), -1),
    "Move old stock to more stores": (("ageing", "sell_within"), +1),
    "Bring back fewer pieces to the warehouse": (("ageing", "pullback_max_units_per_store"), -1),
    "Bring back more slow pieces per run": (("ageing", "pullback_max_units_per_store"), +1),
    "Too many small trips": (("economics", "min_trip_units"), +1),
    "Small urgent transfers were missed": (("economics", "min_trip_units"), -1),
}


def _append(path, rows):
    DIR.mkdir(exist_ok=True)
    df = pd.DataFrame(rows if isinstance(rows, list) else [rows])
    df.to_csv(path, mode="a", header=not path.exists(), index=False)


def _read(path):
    return pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame()


def key(sec_k):
    return f"{sec_k[0]}.{sec_k[1]}"


def load_settings():
    """Feedback-accepted overrides, as a nested dict to merge over the brand config."""
    if SETTINGS.exists():
        try:
            return json.loads(SETTINGS.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def save_settings(over):
    DIR.mkdir(exist_ok=True)
    SETTINGS.write_text(json.dumps(over, indent=2))


def reset_settings():
    if SETTINGS.exists():
        SETTINGS.unlink()


def log_run(R, label, user):
    mv, val = R["moves"], R["checks"]
    run_id = f"{R['pl'].run:%Y%m%d}-{uuid.uuid4().hex[:6]}"
    O = R["opportunity"]
    _append(RUNS, dict(run_id=run_id, run_at=datetime.now().strftime("%Y-%m-%d %H:%M"), user=user,
                       plan_date=f"{R['pl'].run:%Y-%m-%d}", data=label, trips=len(R["trips"]), lines=len(mv),
                       pieces=int(mv.qty.sum()), net_benefit=round(O["net"]), move_cost=round(O["move_cost"]),
                       rules_passed=int((val.status == "PASS").sum()), rules_total=len(val)))
    return run_id


def runs():
    return _read(RUNS)


def add_rating(run_id, stars, right, easy, time_, changes, comment, name):
    _append(RATINGS, dict(rated_at=datetime.now().strftime("%Y-%m-%d %H:%M"), run_id=run_id, stars=stars, moves_right=right,
                          easy=easy, saves_time=time_, changes="; ".join(changes), comment=(comment or "").strip(),
                          name=(name or "").strip()))


def ratings():
    r = _read(RATINGS)
    for c in ("stars", "moves_right", "easy", "saves_time"):
        if c in r:
            r[c] = pd.to_numeric(r[c], errors="coerce")
    return r


def suggested_settings(cfg):
    r = ratings()
    rows = []
    if r.empty:
        return pd.DataFrame(columns=["setting", "what it controls", "now", "suggested", "votes for", "votes against"])
    votes = {k: [0, 0] for k in schema.TUNABLE}
    for cell in r.changes.fillna(""):
        for label in [c.strip() for c in str(cell).split(";") if c.strip()]:
            if label in SUGGESTIONS:
                k, d = SUGGESTIONS[label]
                votes[k][0 if d > 0 else 1] += 1
    for k, (up, down) in votes.items():
        if up == down == 0:
            continue
        lo, hi, step, name = schema.TUNABLE[k]
        now = cfg[k[0]][k[1]]
        net = up - down
        new = min(hi, max(lo, now + (step if net > 0 else -step if net < 0 else 0)))
        rows.append({"setting": key(k), "what it controls": name, "now": now, "suggested": new,
                     "votes for": max(up, down), "votes against": min(up, down)})
    return pd.DataFrame(rows)


# ---- transfer orders released to the warehouse / ERP, and what was received
def release_orders(orders, user, run_id):
    o = orders.assign(released_at=datetime.now().strftime("%Y-%m-%d %H:%M"), released_by=user, run_id=run_id)
    _append(ORDERS, o.to_dict("records"))


def released():
    o = _read(ORDERS)
    if len(o):
        o["qty"] = pd.to_numeric(o.qty, errors="coerce").fillna(0).astype(int)
    return o


def add_receipts(df, user):
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"to_number", "sku_id", "qty_received"}
    if not need <= set(df.columns):
        raise ValueError(f"Receipts file needs columns: {', '.join(sorted(need))}")
    df["recorded_by"] = user
    df["recorded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _append(RECEIPTS, df.to_dict("records"))


def reconciliation():
    o, r = released(), _read(RECEIPTS)
    if o.empty:
        return pd.DataFrame()
    sent = o.groupby(["to_number", "from_location", "to_location", "sku_id", "expected_receipt"]).qty.sum().rename("shipped").reset_index()
    if r.empty:
        sent["received"] = 0
    else:
        r["qty_received"] = pd.to_numeric(r.qty_received, errors="coerce").fillna(0)
        got = r.groupby(["to_number", "sku_id"]).qty_received.sum().rename("received")
        sent = sent.merge(got, on=["to_number", "sku_id"], how="left").fillna({"received": 0})
    sent["variance"] = sent.received - sent.shipped
    late = pd.to_datetime(sent.expected_receipt) < pd.Timestamp.now().normalize()
    sent["status"] = ["Received" if v == 0 else "Over-received" if v > 0 else ("Late / short" if l else "In transit")
                      for v, l in zip(sent.variance, late)]
    return sent
