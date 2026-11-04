"""Generate synthetic 3PL data: true ledger, faulty system copies, answer key."""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
START, END = pd.Timestamp("2026-07-01"), pd.Timestamp("2026-09-30")
FAULT_RATE = 0.03  # overall share of transactions faulted
AFFECTED_SHARE = 0.4  # faults cluster in 40% of lots; the rest stay clean
FAULT_MIX = {"MISSING": 0.33, "DELAYED": 0.33, "DUPLICATE": 0.17, "INCORRECT": 0.17}
WAREHOUSES = {"WH-A": "WMS-A", "WH-B": "WMS-B", "WH-C": "WMS-C"}
OUT = Path("data/raw")
rng = np.random.default_rng(SEED)


def make_master():
    n = 200
    skus = pd.DataFrame({
        "sku_id": [f"SKU{i:04d}" for i in range(1, n + 1)],
        "customer_id": rng.choice([f"C{i:03d}" for i in range(1, 6)], n),
        "units_per_case": rng.choice([6, 12, 24], n),
    })
    perishable = rng.random(n) < 0.4
    skus["shelf_life_days"] = np.where(perishable, rng.integers(30, 181, n), np.nan)
    lots = skus.loc[skus.index.repeat(3), ["sku_id", "customer_id", "shelf_life_days"]]
    lots = lots.reset_index(drop=True)
    m = len(lots)
    lots["lot_id"] = [f"LOT{i:05d}" for i in range(1, m + 1)]
    lots["warehouse_id"] = rng.choice(list(WAREHOUSES), m)
    lots["received_date"] = START + pd.to_timedelta(rng.integers(0, 45, m), unit="D")
    lots["expiry_date"] = lots.received_date + pd.to_timedelta(lots.shelf_life_days, unit="D")
    return skus, lots


def make_truth(lots):
    """What physically happened: one receipt per lot, many shipments, a few silent losses."""
    rows = []
    for lot in lots.itertuples():
        receipt = int(rng.integers(50, 201)) * 100
        rows.append((lot.lot_id, "RECEIPT", receipt, lot.received_date + pd.Timedelta(hours=8)))
        n = int(rng.integers(150, 351))
        qtys = rng.multinomial(int(receipt * rng.uniform(0.7, 0.95)), np.ones(n) / n)
        span = int((END - lot.received_date).total_seconds()) - 86400
        for q, s in zip(qtys, np.sort(rng.integers(86400, span, n))):
            if q > 0:
                rows.append((lot.lot_id, "SHIP", -int(q), lot.received_date + pd.Timedelta(seconds=int(s))))
    t = pd.DataFrame(rows, columns=["lot_id", "txn_type", "qty", "event_ts"])
    t = t.merge(lots[["lot_id", "sku_id", "customer_id", "warehouse_id"]], on="lot_id")
    # Physical loss: stock vanishes but no system records it
    rec = t[t.txn_type == "RECEIPT"].set_index("lot_id")
    loss = rec.loc[rng.choice(lots.lot_id, 15, replace=False)].reset_index()
    loss["txn_type"] = "LOSS"
    loss["qty"] = -(loss.qty * rng.uniform(0.01, 0.04, len(loss))).astype(int)
    loss["event_ts"] = END - pd.Timedelta(days=1)
    t = pd.concat([t, loss]).sort_values("event_ts").reset_index(drop=True)
    t.insert(0, "txn_id", [f"T{i:06d}" for i in range(1, len(t) + 1)])
    return t


def copy_with_faults(part, system, sku_lots, affected):
    """A system's view of the truth, with ~3% of rows faulted. Returns (data, fault rows)."""
    df = part[part.txn_type != "LOSS"].copy()
    df["ref_no"] = df.pop("txn_id")
    df["recorded_ts"] = df.event_ts + pd.to_timedelta(rng.integers(1, 120, len(df)), unit="s")
    hit = (rng.random(len(df)) < FAULT_RATE / AFFECTED_SHARE) & df.lot_id.isin(affected)
    idx = df.index[hit]
    kind = pd.Series(rng.choice(list(FAULT_MIX), len(idx), p=list(FAULT_MIX.values())), index=idx)
    base = df.loc[idx, ["ref_no", "lot_id"]].reset_index(drop=True)

    late = kind.index[kind == "DELAYED"]
    df.loc[late, "recorded_ts"] = df.loc[late, "recorded_ts"] + pd.to_timedelta(
        rng.integers(2, 11, len(late)), unit="D")

    wrong = {}
    for i in kind.index[kind == "INCORRECT"]:
        others = [l for l in sku_lots[df.at[i, "sku_id"]]
                  if l != df.at[i, "lot_id"] and l in affected]  # wrong lot stays within affected lots
        if rng.random() < 0.5 or not others:
            df.at[i, "qty"] = df.at[i, "qty"] * 10  # typo
        else:
            wrong[i] = str(rng.choice(others))
            df.at[i, "lot_id"] = wrong[i]  # posted to the wrong lot

    dup = df.loc[kind.index[kind == "DUPLICATE"]]
    df = df.drop(kind.index[kind == "MISSING"])
    df = pd.concat([df, dup]).sort_values("recorded_ts").reset_index(drop=True)
    df.insert(0, "sys_txn_id", [f"{system}-{i:06d}" for i in range(1, len(df) + 1)])
    faults = base.assign(system=system, fault_type=kind.values,
                         wrong_lot_id=[wrong.get(i) for i in idx])
    return df, faults


def snapshot(df):
    """On-hand per lot as of period end, counting only rows already posted."""
    return df[df.recorded_ts <= END].groupby("lot_id").qty.sum()


def validate(systems, fault_log, count, lots):
    bad = set(fault_log.lot_id) | set(fault_log.wrong_lot_id.dropna())
    clean = count.index.difference(list(bad))
    owner = lots.set_index("lot_id").warehouse_id
    for name, df in systems.items():
        mine = [l for l in clean if name == "OMS" or WAREHOUSES[owner[l]] == name]
        assert (snapshot(df).reindex(mine) == count[mine]).all(), f"{name} mismatch on clean lots"
    assert len(clean) >= 0.3 * len(count), "too few clean lots to validate against"
    print(fault_log.groupby(["system", "fault_type"]).size().unstack(fill_value=0))
    print(f"Verified {len(clean)} fault-free lots of {len(count)}: all systems match physical count")


def to_b(df):  # WMS-B: different column names, local (WAT) time strings
    out = df.copy()
    for c in ("event_ts", "recorded_ts"):
        out[c] = (out[c] + pd.Timedelta(hours=1)).dt.strftime("%d/%m/%Y %H:%M:%S")
    return out.rename(columns={
        "sys_txn_id": "transaction_no", "ref_no": "reference", "txn_type": "movement_type",
        "warehouse_id": "site", "customer_id": "cust_code", "sku_id": "item_code",
        "lot_id": "batch_no", "qty": "quantity", "uom": "unit",
        "event_ts": "txn_datetime", "recorded_ts": "posted_datetime"})


def main():
    skus, lots = make_master()
    sku_lots = lots.groupby("sku_id").lot_id.apply(list).to_dict()
    truth = make_truth(lots)
    affected = set(rng.choice(lots.lot_id, int(len(lots) * AFFECTED_SHARE), replace=False))

    systems, faults = {}, []
    for wh, name in WAREHOUSES.items():
        systems[name], f = copy_with_faults(truth[truth.warehouse_id == wh], name, sku_lots, affected)
        faults.append(f)
    systems["OMS"], f = copy_with_faults(truth, "OMS", sku_lots, affected)
    faults.append(f)
    loss = truth[truth.txn_type == "LOSS"]
    faults.append(pd.DataFrame({"system": "NONE", "ref_no": loss.txn_id, "lot_id": loss.lot_id,
                                "fault_type": "PHYSICAL_LOSS", "wrong_lot_id": None}))
    fault_log = pd.concat(faults, ignore_index=True)
    fault_log.insert(0, "fault_id", [f"F{i:05d}" for i in range(1, len(fault_log) + 1)])
    count = truth.groupby("lot_id").qty.sum().rename("counted_qty")
    validate(systems, fault_log, count, lots)

    OUT.mkdir(parents=True, exist_ok=True)
    write = lambda df, name: df.to_csv(OUT / f"{name}.csv", index=False)
    write(pd.DataFrame({"customer_id": sorted(skus.customer_id.unique())}), "customers")
    write(pd.DataFrame({"warehouse_id": list(WAREHOUSES), "system": list(WAREHOUSES.values())}), "warehouses")
    write(skus, "skus"); write(lots, "lots")
    write(truth, "truth_ledger"); write(fault_log, "fault_log")
    write(count.reset_index(), "cycle_count")

    upc = skus.set_index("sku_id").units_per_case
    lot_upc = lots.set_index("lot_id").sku_id.map(upc)
    for name, df in systems.items():
        df = df.assign(uom="EA")
        snap = snapshot(df).rename("on_hand").reset_index()
        if name == "WMS-C":  # counts in cases, not units
            df["qty"] = (df.qty / df.sku_id.map(upc)).round(4)
            df["uom"] = "CS"
            snap["on_hand"] = (snap.on_hand / snap.lot_id.map(lot_upc)).round(4)
        if name == "WMS-B":
            df = to_b(df)
        key = name.lower().replace("-", "_")
        write(df, f"{key}_{'event' if name == 'OMS' else 'txn'}")
        write(snap, f"{key}_snapshot")
    print(f"Done. {len(truth):,} true transactions. Files in {OUT}")


if __name__ == "__main__":
    main()