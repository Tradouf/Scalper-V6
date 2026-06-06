#!/usr/bin/env python3
"""
Transformer prédictif L2 — challenger du baseline XGB (2026-06-06).

Architecture PatchTST-lite :
  - séquence d'entrée : 120 barres 30s (1h) × 8 canaux orderflow
  - RevIN par fenêtre (instance norm) → robustesse à la non-stationnarité
  - patching temporel : 12 barres (6 min) par patch, 10 patches
  - encoder Transformer 3 couches, d_model=128, 4 têtes (~600k params)
  - tête binaire : direction du mid à +15 min (mêmes label/deadzone que XGB)

Protocole IDENTIQUE à train_xgb_l2.py : mêmes données (orderflow.db 30s),
mêmes 5 folds walk-forward expanding purgés (gap 15 min), même métrique.
Règle de la maison : si l'AUC moyenne ne bat pas le baseline (0.513),
le modèle ne rentre pas en prod.

Usage :
  python3 scripts/train_transformer_l2.py            # rapport
  python3 scripts/train_transformer_l2.py --save     # + artifact
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import roc_auc_score
from torch.utils.data import DataLoader, TensorDataset

REPO = Path(__file__).resolve().parent.parent
DB_30S = Path("/home/francois/SalleDesMarches_fixed/memory/orderflow.db")

BAR_SEC = 30
SEQ_LEN = 120             # 1h d'historique
HORIZON_MIN = 15
DEADZONE_BPS = 2.0
N_FOLDS = 5
PATCH_LEN = 12            # 6 min par patch
D_MODEL = 128
N_HEADS = 4
N_LAYERS = 3
BATCH = 1024
MAX_EPOCHS = 12
PATIENCE = 3
LR = 3e-4

CHANNELS = ["ret_bps", "imb1", "imb5", "imb20", "spread_bps", "basis_bps",
            "funding", "dvol"]

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


# ── Données ───────────────────────────────────────────────────────────────────
def load_sequences() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """→ X[n, SEQ_LEN, C], y[n], ts[n], coin_id[n]"""
    conn = sqlite3.connect(DB_30S)
    df = pd.read_sql(
        "SELECT ts, coin, mid_px, spread_bps, imb1, imb5, imb20, funding, mark_px "
        "FROM orderflow ORDER BY coin, ts", conn)
    conn.close()
    df["ts"] = df["ts"] // 1000

    h_bars = HORIZON_MIN * 60 // BAR_SEC
    Xs, ys, tss, cids = [], [], [], []
    coins = sorted(df["coin"].unique())
    for cid, coin in enumerate(coins):
        g = df[df["coin"] == coin].drop_duplicates("ts").set_index("ts").sort_index()
        idx = np.arange(g.index.min(), g.index.max() + 1, BAR_SEC)
        g = g.reindex(idx, method="ffill", limit=4)
        mid = g["mid_px"].values
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.where(mid[:-1] > 0, (mid[1:] / mid[:-1] - 1) * 1e4, np.nan)
        ret = np.concatenate([[np.nan], ret])
        chans = np.column_stack([
            ret,
            g["imb1"].values, g["imb5"].values, g["imb20"].values,
            g["spread_bps"].values,
            (g["mark_px"].values - mid) / mid * 1e4,
            g["funding"].values * 1e6,         # échelle lisible
            pd.Series(ret).rolling(10).std().values,  # vol locale 5 min
        ]).astype(np.float32)

        fwd = np.full(len(mid), np.nan, dtype=np.float32)
        fwd[:-h_bars] = (mid[h_bars:] / mid[:-h_bars] - 1) * 1e4

        # fenêtres glissantes valides
        ok = ~np.isnan(chans).any(axis=1)
        ok_cum = np.cumsum(ok)
        for t in range(SEQ_LEN, len(mid) - h_bars):
            # fenêtre 100% valide + label hors deadzone
            if ok_cum[t - 1] - (ok_cum[t - SEQ_LEN - 1] if t > SEQ_LEN else 0) != SEQ_LEN:
                continue
            f = fwd[t - 1]
            if np.isnan(f) or abs(f) < DEADZONE_BPS:
                continue
            Xs.append(chans[t - SEQ_LEN:t])
            ys.append(1.0 if f > 0 else 0.0)
            tss.append(idx[t - 1])
            cids.append(cid)

    X = np.stack(Xs)            # [n, L, C]
    return (X, np.array(ys, dtype=np.float32),
            np.array(tss, dtype=np.int64), np.array(cids, dtype=np.int64))


# ── Modèle ────────────────────────────────────────────────────────────────────
class PatchTSTLite(nn.Module):
    def __init__(self, n_coins: int, n_chan: int = len(CHANNELS)):
        super().__init__()
        n_patches = SEQ_LEN // PATCH_LEN
        self.patch_embed = nn.Linear(PATCH_LEN * n_chan, D_MODEL)
        self.pos = nn.Parameter(torch.zeros(1, n_patches, D_MODEL))
        self.coin_embed = nn.Embedding(n_coins, D_MODEL)
        enc = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS, dim_feedforward=D_MODEL * 4,
            dropout=0.1, activation="gelu", batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(enc, num_layers=N_LAYERS)
        self.head = nn.Sequential(
            nn.LayerNorm(D_MODEL), nn.Linear(D_MODEL, 1))

    def forward(self, x: torch.Tensor, coin_id: torch.Tensor) -> torch.Tensor:
        # x: [B, L, C] — RevIN par fenêtre et par canal
        mu = x.mean(dim=1, keepdim=True)
        sd = x.std(dim=1, keepdim=True).clamp_min(1e-6)
        x = (x - mu) / sd
        B, L, C = x.shape
        x = x.reshape(B, L // PATCH_LEN, PATCH_LEN * C)   # [B, P, patch*C]
        z = self.patch_embed(x) + self.pos
        z = z + self.coin_embed(coin_id).unsqueeze(1)
        z = self.encoder(z)
        return self.head(z.mean(dim=1)).squeeze(-1)


# ── Entraînement walk-forward ────────────────────────────────────────────────
def run_fold(Xtr, ytr, ctr, Xva, yva, cva, Xte, yte, cte, n_coins) -> tuple[float, float, nn.Module]:
    model = PatchTSTLite(n_coins).to(DEVICE)
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
    lossf = nn.BCEWithLogitsLoss()
    dl = DataLoader(
        TensorDataset(torch.from_numpy(Xtr), torch.from_numpy(ytr), torch.from_numpy(ctr)),
        batch_size=BATCH, shuffle=True, drop_last=True)

    def predict(X, c) -> np.ndarray:
        model.eval()
        out = []
        with torch.no_grad():
            for i in range(0, len(X), 4096):
                xb = torch.from_numpy(X[i:i + 4096]).to(DEVICE)
                cb = torch.from_numpy(c[i:i + 4096]).to(DEVICE)
                out.append(torch.sigmoid(model(xb, cb)).cpu().numpy())
        return np.concatenate(out)

    best_auc, best_state, bad = 0.0, None, 0
    for epoch in range(MAX_EPOCHS):
        model.train()
        for xb, yb, cb in dl:
            xb, yb, cb = xb.to(DEVICE), yb.to(DEVICE), cb.to(DEVICE)
            opt.zero_grad()
            loss = lossf(model(xb, cb), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
        va_auc = roc_auc_score(yva, predict(Xva, cva))
        if va_auc > best_auc + 1e-4:
            best_auc, bad = va_auc, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= PATIENCE:
                break
    if best_state:
        model.load_state_dict(best_state)
    p = predict(Xte, cte)
    return roc_auc_score(yte, p), ((p > 0.5) == yte.astype(bool)).mean(), model


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    if not DB_30S.exists():
        sys.exit(f"DB introuvable : {DB_30S}")

    print(f"device={DEVICE} | chargement + fenêtrage…")
    X, y, ts, cid = load_sequences()
    n_coins = int(cid.max()) + 1
    print(f"{len(X):,} fenêtres [{SEQ_LEN}×{len(CHANNELS)}] | {n_coins} coins | "
          f"{dt.datetime.fromtimestamp(ts.min()):%m-%d} → {dt.datetime.fromtimestamp(ts.max()):%m-%d} "
          f"| base rate {y.mean():.3f} | RAM X = {X.nbytes / 1e9:.2f} Go")

    bounds = np.quantile(ts, np.linspace(0, 1, N_FOLDS + 2))
    gap = HORIZON_MIN * 60
    aucs, accs, model = [], [], None
    print(f"\n{'fold':<6}{'train':>9}{'test':>9}{'AUC':>8}{'acc':>8}")
    for k in range(1, N_FOLDS + 1):
        tr = ts < bounds[k] - gap
        te = (ts >= bounds[k]) & (ts < bounds[k + 1])
        if tr.sum() < 5000 or te.sum() < 1000:
            continue
        # val = dernier 10% du train (purgé du gap), pour l'early stopping
        v_start = np.quantile(ts[tr], 0.9)
        va = tr & (ts >= v_start)
        tr2 = tr & (ts < v_start - gap)
        auc, acc, model = run_fold(
            X[tr2], y[tr2], cid[tr2], X[va], y[va], cid[va],
            X[te], y[te], cid[te], n_coins)
        aucs.append(auc); accs.append(acc)
        print(f"{k:<6}{tr2.sum():>9,}{te.sum():>9,}{auc:>8.4f}{acc:>8.4f}")

    print(f"\n→ AUC walk-forward : {np.mean(aucs):.4f} ± {np.std(aucs):.4f} "
          f"| acc {np.mean(accs):.4f}")
    print("  Baseline XGB à battre : 0.5129 (cf. memory/xgb_l2_20260606.pkl)")

    if args.save and model is not None:
        out = REPO / "memory" / f"transformer_l2_{dt.date.today():%Y%m%d}.pt"
        torch.save({
            "state_dict": model.state_dict(),
            "config": dict(seq_len=SEQ_LEN, patch_len=PATCH_LEN, d_model=D_MODEL,
                           n_heads=N_HEADS, n_layers=N_LAYERS, channels=CHANNELS,
                           horizon_min=HORIZON_MIN, deadzone_bps=DEADZONE_BPS,
                           n_coins=n_coins),
            "wf_auc_mean": float(np.mean(aucs)), "wf_auc_std": float(np.std(aucs)),
            "trained_at": dt.datetime.now().isoformat(),
        }, out)
        print(f"Artifact : {out}")


if __name__ == "__main__":
    main()
