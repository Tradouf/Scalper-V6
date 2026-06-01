# Handoff V6 → V7 : fixes grille depuis l'arrêt du scalping

**Rédigé le 2026-06-01 par la session Claude Code qui gère la V6.**

## Contexte / topologie

| | Chemin | Entrypoint | État |
|---|---|---|---|
| **V6 (source des fixes)** | `/home/francois/SalleDesMarches_fixed` | `main_v6.py` | dépôt git, fixes commités sur branche `fix/grid-oid-cache-orphan` |
| **V7 (live)** | `/home/francois/SalleDesMarches_v7` | `main.py` (PID live) | **tourne en prod**, a ENCORE les bugs de grille (doublons / ordres manquants) |

- Le **scalp est OFF** depuis le 26/05 (`SCALP_ENABLED=False`, commit `c4c9062`, raison : scalp −$32 all-time = 92% du déficit). Le bot ne fait plus que **grid + MR**. Donc les fixes ci-dessous concernent **uniquement la grille et l'infra HL** — c'est ce qui tourne réellement.
- Les fixes vivent dans la **V6**. La V7 doit les **porter**. Tout est consultable depuis la V6 :
  ```bash
  cd /home/francois/SalleDesMarches_fixed
  git show <hash>            # diff exact d'un fix
  git log --oneline --since="2026-05-26"
  ```
- Doc d'origine déjà présente côté V6 : `V7_PORTING_NOTES_2026-05-29.md` (Fixes 1-5) et `V7_PORTING_NOTES_2026-05-30.md` (Fixes 6-10, avec ancrages). Ce handoff complète avec **toute la série grille du 26/05 au 31/05**.

---

## A. Fixes grille — à porter en priorité (bugs doublons / manquants)

Ordre chronologique. Ancrages = **positions actuelles** dans la V6 (les n° du diff sont historiques).

### 1. `981cf84` (30/05) — **CAUSE RACINE des doublons/orphelins** ⭐
`fix(grid+infra): traiter fill immédiat, back-off cache HL, grâce orphan-exit` (= Fixes 6-7-8 du porting note 30/05)
- **Fix 7 (racine)** : `agents/grid_manager.py:_place_limit` (~l.878) traitait un `order_id` **vide** comme un échec. Or `exchanges/hyperliquid.py:place_order` renvoie `order_id="" status="filled"` quand un limit GTC **croise le book = fill immédiat**. → fill non tracké → position orpheline → **re-pose en boucle = ORDRES EN DOUBLON**. Fix : inspecter `result.status` (voir `PlaceResult`, `grid_manager.py:58`) au lieu de la présence de l'oid.
- **Fix 6** : `main_v6.py:_assert_hl_cache_fresh` re-forçait un sync à chaque décision sans back-off (~10 800 WARNING). Fix : cooldown (`HL_FORCED_SYNC_COOLDOWN_SEC`) + throttle log (`HL_CACHE_WARN_THROTTLE_SEC`).
- **Fix 8** : grâce avant force-close d'une position grid/orpheline. `main_v6.py:ORPHAN_GRACE_SEC` (l.168), `_orphan_emergency_since` (l.337). À porter **APRÈS** Fix 7.
- **Ordre de portage : 7 → 6 → 8.**

### 2. `5316822` (29/05) — **anti-doublon explicite** ⭐
`3 fixes : dashboard registry, dust gating, ladder anti-doublon`
- **Ladder anti-doublon** : `agents/grid_manager.py` (zone l.286+) + nouveau **`memory/order_registry.py`** (`OrderRegistry.register`/`lookup`, l.123/159) = registre persistant d'OID pour **dédupliquer** les poses. C'est LE mécanisme anti-doublon. Câblé dans `main_v6.py` (~l.1226).
- **Dust gating** : ignore les positions résiduelles < `DUST_POSITION_NOTIONAL_USD` (settings l.287) pour ne pas les traiter comme orphelines.

### 3. `88eb62f` (26/05) — **ordres MANQUANTS (rejet HL silencieux)** ⭐
`fix(grid): _round_px tick decimals par symbole`
- `agents/grid_manager.py:_round_px` (l.853) : mauvais arrondi de prix → **HL rejette l'ordre silencieusement** → niveau jamais posé = **ORDRE MANQUANT**. Fix : décimales de tick par symbole.

### 4. `e110dc1` (28/05) — boucle re-fill (doublons)
`grid: fix boucle re-fill au même prix sur G2 skip (frozen state)`
- `agents/grid_manager.py` : un niveau G2 « skippé » se re-remplissait **au même prix en boucle**. Ajoute un état `frozen` (`GridLevel`, l.35) + timeout `GRID_FROZEN_TIMEOUT_SEC` (settings l.280).

### 5. `6b14ec1` (26/05) — niveaux perdus définitivement (manquants)
`fix(grid G2): recycler en pending au lieu de désarmer définitivement`
- `agents/grid_manager.py` (~l.435) : un niveau désarmé restait mort → **niveau manquant permanent**. Fix : recycler en `pending`.

### 6. `29a6a14` (26/05) — auto-réparation
`feat(grid): ladder health check toutes les 5 min`
- `agents/grid_manager.py:_ladder_health_check` (l.372) + `GRID_HEALTH_CHECK_SEC` (settings l.273) : détecte et **répare** niveaux manquants/en trop périodiquement. Filet de sécurité au-dessus des fixes 1-5.

### 7. `d52bf55` (27/05) — nettoyage au boot
`feat(grid): cleanup des reliquats grid tagués 'unknown' au boot`
- `agents/grid_manager.py:cleanup_unknown_grid_orphans` (l.479) + `cleanup_dangling_orders` (l.526), appelé au boot (`main_v6.py` ~l.2078) : purge les **ordres doublons/orphelins laissés par un crash précédent**.

### 8. `d59107e` (28/05) — garde infra (faux ordres manquants)
`hl_sync: garde anti-réponse-vide-fantôme sur open_orders`
- `main_v6.py` (~l.994, dans `_hl_sync_loop`) : une réponse **vide fantôme** de l'API `open_orders` faisait croire que tous les ordres avaient disparu → re-pose massive. Fix : ignorer la réponse vide suspecte.

---

## B. Prototypes OFF par défaut — secondaires pour le bug grille

À porter pour parité, mais **flags à False** (n'affectent rien tant qu'inactifs) :

- `f8f9abc` (30/05) — **Fix 9** trail régime-gaté. `REGIME_GATED_TRAIL=False`, helper `_classify_entry_regime`, `guard["regime_mode"]`.
- `8994264` (31/05) — **Fix 10** haut levier. Knob A `HIGH_LEV_EMERGENCY_EXEMPT`, Knob B `LEVERAGE_CAP_ENABLED`, helper `main_v6.py:_emergency_threshold`. ⚠️ Côté V6, **Knob B vient d'être passé à `True`** (cap levier 5x) après analyse PnL HYPE (NET −8,16 USDC à 10x, dont 10,58 de frais) — non encore redémarré. Décider côté V7 si on reproduit.

Détail complet de Fix 9/10 dans `/home/francois/SalleDesMarches_fixed/V7_PORTING_NOTES_2026-05-30.md` (sections l.214 et l.257).

---

## C. Récap mapping symptôme → fix

| Symptôme V7 | Fixes V6 à porter |
|---|---|
| **Ordres en doublon** | `981cf84` (Fix 7, racine) → `5316822` (order_registry) → `e110dc1` (re-fill loop) → `d52bf55` (cleanup boot) |
| **Ordres manquants** | `88eb62f` (rejet HL silencieux) → `6b14ec1` (recycle pending) → `d59107e` (anti-fantôme) |
| **Position orpheline / emergency cascade** | `981cf84` (Fix 8 grâce) + `29a6a14` (health check) |

**Note** : ⚠️ ces fixes ont été écrits sur `main_v6.py` + `agents/grid_manager.py`. La V7 (`main.py`) a une structure différente — vérifier que `grid_manager.py` / `order_registry.py` / la couche `exchanges/hyperliquid.py:place_order` (renvoi `status="filled"` sur fill immédiat) existent à l'identique côté V7 avant de transposer les ancrages.
