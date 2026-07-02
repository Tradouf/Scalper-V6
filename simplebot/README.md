# SimpleBot — algo paramétrique auto-optimisé

Bot de trading **indépendant de la V6**, volontairement simple :

- **Stratégie** : croisement EMA rapide/lente + filtre RSI anti-surextension.
  TP et SL posés en multiples d'ATR, **natifs sur l'exchange** dès l'entrée
  (un crash du bot ne laisse jamais une position sans protection).
- **Agent optimiseur** : toutes les 6 h, backteste une grille de 72 jeux de
  paramètres par symbole sur 45 jours de bougies 15 m, en walk-forward
  (classement sur 70 % train, confirmation obligatoire sur les 30 % de
  validation jamais vus). La validation est un **filtre binaire** : le premier
  set du classement train qui confirme (PF ≥ 1.2, PnL > 0, ≥ 5 trades) est
  publié dans `simplebot/state/best_params.json` — jamais le meilleur PnL de
  validation, ce qui réintroduirait de l'overfit. Sinon le symbole passe
  `inactive` et le live n'ouvre plus rien dessus.
- **Trader live** : recharge à chaud les paramètres publiés, agit une seule
  fois par bougie clôturée, trade sur un **second wallet** Hyperliquid
  (`HL2_PRIVATE_KEY`), jamais celui de la V6 (refus de démarrer si identique).

## Sécurités live

- **Réconciliation au démarrage** : toute position trouvée sans SL natif
  (crash entre l'ouverture et la pose du TP/SL) est re-protégée à partir des
  paramètres courants et d'un ATR frais, ou fermée si c'est impossible.
- **Kill-switch** : si l'account value du wallet perd `SIMPLEBOT_KILL_LOSS_PCT`
  (défaut 5 %) par rapport à son pic sur `SIMPLEBOT_KILL_WINDOW_SEC` (défaut
  24 h), toutes les positions sont fermées et le trading est mis en pause
  `SIMPLEBOT_KILL_PAUSE_SEC` (défaut 24 h).
- **Dry-run avec positions papier** : en dry-run, les entrées, TP/SL et flips
  sont simulés sur les bougies suivantes et le PnL cumulé est logué
  (`[PAPER] … cumul: N trades, +X%, winrate Y%`) — un vrai verdict chiffré
  avant de passer live.

## Paramètres optimisés

| Paramètre | Grille | Rôle |
|---|---|---|
| `ema_fast` | 9, 12, 21 | EMA rapide (signal) |
| `ema_slow` | 26, 50, 100 | EMA lente (tendance) — contrainte : ≥ 2 × fast |
| `tp_atr` | 1.5, 2.5, 3.5 | Take-profit = entry ± tp_atr × ATR(14) |
| `sl_atr` | 1.0, 1.5, 2.0 | Stop-loss = entry ∓ sl_atr × ATR(14) |

## Configuration

```bash
# .env — wallet DÉDIÉ (ne pas réutiliser HL_PRIVATE_KEY)
HL2_PRIVATE_KEY=0x...
HL2_ACCOUNT_ADDRESS=0x...   # optionnel (wallet API/agent signant pour un compte maître)
```

Réglages surchargeables par env (`simplebot/config.py`) : `SIMPLEBOT_SYMBOLS`
(défaut `BTC,ETH,SOL`), `SIMPLEBOT_INTERVAL` (`15m`), `SIMPLEBOT_LEVERAGE` (3),
`SIMPLEBOT_MARGIN_PCT` (0.05 = 5 % de l'account par trade),
`SIMPLEBOT_MAX_OPEN_POSITIONS` (3), `SIMPLEBOT_OPTIMIZE_INTERVAL_SEC` (21600),
`SIMPLEBOT_MIN_VALID_PF` (1.2), `SIMPLEBOT_MIN_VALID_TRADES` (5),
`SIMPLEBOT_BACKTEST_DAYS` (45, plafond API ~52 j en 15m),
`SIMPLEBOT_KILL_LOSS_PCT` (0.05), `SIMPLEBOT_KILL_WINDOW_SEC` (86400),
`SIMPLEBOT_KILL_PAUSE_SEC` (86400)…

## Lancement

```bash
# Dry-run (défaut) : optimiseur + signaux logués, AUCUN ordre envoyé
python -m simplebot.run

# Une optimisation puis exit (cron-friendly)
python -m simplebot.run --optimize-once

# Live réel — exige le wallet HL2 et l'opt-in explicite
SIMPLEBOT_DRY_RUN=0 python -m simplebot.run

# ou
bash start_simplebot.sh
```

## Tests

```bash
python -m pytest tests/test_simplebot.py -v
```

## Fichiers d'état (`simplebot/state/`, non versionnés)

- `best_params.json` — meilleur set par symbole + métriques train/valid (écrit
  atomiquement, rechargé à chaud par le live)
- `optimizer_history.jsonl` — historique de toutes les optimisations
- `live_state.json` — dernière bougie traitée par symbole (pas de double ordre
  après restart), positions et trades papier du dry-run, historique d'equity
  et état du kill-switch
