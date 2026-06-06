#!/usr/bin/env python3
"""
Baseline XGB L2 — benchmark prédictif (fondations RL/Transformer, 2026-06-06).

Objectif : établir l'AUC de référence qu'un futur modèle Transformer devra
battre en walk-forward AVANT toute intégration au bot. Cf. plan XGB retrain
(seuil ≥14j d'orderflow.db atteint : données du 2026-05-18 → aujourd'hui).

Données  : orderflow.db du collecteur 30s (_fixed) — 10 coins, imb1/5/20,
           spread, funding, mark/mid.
Features : imbalances (niveau + moyennes mobiles), spread, funding, basis
           mark-mid, returns passés multi-horizons, vol réalisée, z-score du mid.
Label    : direction du mid à +15 min (binaire, mouvements < 2 bps exclus).
Split    : walk-forward 5 folds expanding window (aucune fuite temporelle),
           gap de 15 min entre train et test (purge de l'horizon du label).

Usage :
  python3 scripts/train_xgb_l2.py                  # rapport seul
  python3 scripts/train_xgb_l2.py --save           # + artifact memory/xgb_l2_<date>.pkl
"""
from __future__ import annotations

import argparse
import datetime as dt
import pickle
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DB_30S = Path("/home/francois/SalleDesMarches_fixed/memory/orderflow.db")

HORIZON_MIN = 15          # horizon du label
DEADZONE_BPS = 2.0        # |ret fwd| < 2 bps → échantillon exclu (bruit)
N_FOLDS = 5
BAR_SEC = 30              # cadence nominale du collecteur source


def load_raw() -> pd.DataFrame:
    conn = sqlite3.connect(DB_30S)
    df = pd.read_sql(
        "SELECT ts, coin, mid_px, spread_bps, imb1, imb5, imb20, funding, mark_px "
        "FROM orderflow ORDER BY coin, ts", conn)
    conn.close()
    df["ts"] = df["ts"] // 1000  # ms → s
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    bars_per_min = 60 // BAR_SEC
    h_bars = HORIZON_MIN * bars_per_min
    for coin, g in df.groupby("coin"):
        g = g.drop_duplicates("ts").set_index("ts").sort_index()
        # Ré-échantillonne sur une grille 30s régulière (trous → ffill court)
        idx = np.arange(g.index.min(), g.index.max() + 1, BAR_SEC)
        g = g.reindex(idx, method="ffill", limit=4)
        mid = g["mid_px"]

        f = pd.DataFrame(index=g.index)
        f["coin"] = coin
        f["spread_bps"] = g["spread_bps"]
        f["funding"] = g["funding"]
        f["basis_bps"] = (g["mark_px"] - mid) / mid * 1e4
        for c in ("imb1", "imb5", "imb20"):
            f[c] = g[c]
            f[f"{c}_ma10"] = g[c].rolling(10).mean()      # 5 min
            f[f"{c}_ma60"] = g[c].rolling(60).mean()      # 30 min
        for mins in (1, 5, 15, 60):
            n = mins * bars_per_min
            f[f"ret_{mins}m_bps"] = (mid / mid.shift(n) - 1) * 1e4
        ret1 = mid.pct_change()
        f["vol_15m_bps"] = ret1.rolling(15 * bars_per_min).std() * 1e4
        f["vol_60m_bps"] = ret1.rolling(60 * bars_per_min).std() * 1e4
        ma = mid.rolling(60 * bars_per_min).mean()
        sd = mid.rolling(60 * bars_per_min).std()
        f["zscore_60m"] = (mid - ma) / sd.replace(0, np.nan)
        f["hour_utc"] = pd.to_datetime(f.index, unit="s").hour

        # Label : direction du mid à +15 min
        fwd = (mid.shift(-h_bars) / mid - 1) * 1e4
        f["fwd_bps"] = fwd
        f["y"] = (fwd > 0).astype(int)
        out.append(f.reset_index(names="ts"))

    feats = pd.concat(out, ignore_index=True)
    feats = feats.dropna(subset=["fwd_bps", "ret_60m_bps", "vol_60m_bps",
                                 "imb1_ma60", "zscore_60m", "spread_bps"])
    feats = feats[feats["fwd_bps"].abs() >= DEADZONE_BPS]
    return feats


def walk_forward(feats: pd.DataFrame, save: bool) -> None:
    from sklearn.metrics import roc_auc_score
    from xgboost import XGBClassifier

    feature_cols = [c for c in feats.columns
                    if c not in ("ts", "coin", "fwd_bps", "y")]
    # one-hot coin (parité avec un déploiement multi-symbole)
    X_full = pd.get_dummies(feats[feature_cols + ["coin"]], columns=["coin"], prefix="coin")
    y_full = feats["y"].values
    ts = feats["ts"].values

    bounds = np.quantile(ts, np.linspace(0, 1, N_FOLDS + 2))
    gap = HORIZON_MIN * 60
    aucs, accs = [], []
    print(f"\n{len(feats):,} échantillons | {feats['coin'].nunique()} coins | "
          f"{dt.datetime.fromtimestamp(ts.min()):%m-%d} → {dt.datetime.fromtimestamp(ts.max()):%m-%d} "
          f"| base rate y=1 : {y_full.mean():.3f}\n")
    print(f"{'fold':<6}{'train':>9}{'test':>9}{'AUC':>8}{'acc':>8}")

    model = None
    for k in range(1, N_FOLDS + 1):
        tr = ts < bounds[k] - gap
        te = (ts >= bounds[k]) & (ts < bounds[k + 1])
        if tr.sum() < 1000 or te.sum() < 500:
            continue
        model = XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, eval_metric="auc",
            tree_method="hist", device="cuda", verbosity=0,
        )
        model.fit(X_full[tr], y_full[tr])
        p = model.predict_proba(X_full[te])[:, 1]
        auc = roc_auc_score(y_full[te], p)
        acc = ((p > 0.5).astype(int) == y_full[te]).mean()
        aucs.append(auc); accs.append(acc)
        print(f"{k:<6}{tr.sum():>9,}{te.sum():>9,}{auc:>8.4f}{acc:>8.4f}")

    print(f"\n→ AUC walk-forward : {np.mean(aucs):.4f} ± {np.std(aucs):.4f} "
          f"| acc {np.mean(accs):.4f}")
    print("  (référence à battre par le Transformer ; 0.50 = hasard)")

    if model is not None:
        imp = pd.Series(model.feature_importances_, index=X_full.columns)
        print("\nTop 10 features (dernier fold) :")
        for name, v in imp.nlargest(10).items():
            print(f"  {name:<18} {v:.3f}")

    if save and model is not None:
        out = REPO / "memory" / f"xgb_l2_{dt.date.today():%Y%m%d}.pkl"
        out.parent.mkdir(exist_ok=True)
        with open(out, "wb") as fh:
            pickle.dump({
                "model": model, "feature_cols": list(X_full.columns),
                "trained_at": dt.datetime.now().isoformat(),
                "horizon_min": HORIZON_MIN, "deadzone_bps": DEADZONE_BPS,
                "wf_auc_mean": float(np.mean(aucs)), "wf_auc_std": float(np.std(aucs)),
            }, fh)
        print(f"\nArtifact : {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true", help="sauve l'artifact du dernier fold")
    args = ap.parse_args()
    if not DB_30S.exists():
        sys.exit(f"DB introuvable : {DB_30S}")
    print("Chargement orderflow.db (30s)…")
    raw = load_raw()
    print(f"{len(raw):,} lignes brutes")
    feats = build_features(raw)
    walk_forward(feats, save=args.save)


if __name__ == "__main__":
    main()
