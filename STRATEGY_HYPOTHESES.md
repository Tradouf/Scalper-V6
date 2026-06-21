# Backlog d'hypothèses de stratégies (recherche Opus, 2026-06-18)

> Généré par Opus en mode chercheur quantitatif (lecture seule), après l'échec de l'AO (aucun edge robuste).
> À juger via le harnais walk-forward OOS (`backtest/evaluator.py`) — rien ne passe `enabled=true` sans PASS du
> gate + accord francois. Voir aussi `AO_STRATEGY_PLAN.md` et la mémoire `project_eval_harness`.

## Données réellement disponibles (vérifié dans le code)

| Donnée | Existe ? | Source | Backtestable aujourd'hui ? |
|---|---|---|---|
| OHLCV multi-TF (1m→1d) multi-symbole | Oui | `exchanges/hyperliquid.py:get_ohlcv`, `hyperliquid_client.py:get_candles` | **Oui, direct** |
| Funding live | Oui | `hyperliquid_client.py:359 get_funding_rate` | Live seulement |
| Funding historique | Oui mais **12 j** | `data/orderflow_hf.db` → `l2_1s.funding` (1s, 8 coins) | Via DB |
| Open Interest historique | Oui, 12 j | `l2_1s.oi`, `l2_1s.mark_px` | Via DB |
| L2 imbalance (1/5/10/20) + spread | Oui, 12 j, 1s | `l2_1s.imb1/imb5/imb10/imb20`, `spread_bps` | Via DB |
| Trades tape (CVD) | Oui, 12 j, 1s | `trades(ts_ms,coin,side∈{A,B},px,sz)` — 5,6 M lignes | Via DB |
| Features régime (no-leak) | Oui | `regime/features.py` : adx, hurst_rs, autocorr_lag1, realized_vol, vol_percentile, returns_slope_zscore, supertrend | Oui |
| News RSS / Whale Alert | Flux temps réel **non historisé** | `feeds/`, `governor/strategist.py` | **Non backtestable** |

**Limites dures :** le backtester ne consomme qu'une `Series` de signaux dérivée d'OHLCV → funding/OI/L2/CVD
ne sont PAS branchés (enrichir `df` de colonnes mergées depuis `orderflow_hf.db`, effort moyen, une fois). Gate
sévère (≥30 trades OOS, ≥80% folds positifs, PF médian ≥1,05, t-stat ≥1,5). **12 j d'orderflow = ~1 seul régime**
→ fragilité de toute conclusion HF sur la persistance.

## Hypothèses priorisées

1. **Funding fade extrême (cross-sectionnel)** ⭐ — prime de risque structurelle (funding = flux de cash
   contraint). SHORT le panier top-funding / LONG bottom-funding, market-neutral (enlève le beta BTC). 1-2
   params. Prérequis câblé 2026-06-18 : `HyperliquidReadAdapter.get_funding_history` (paginé) + `build_funding_matrix`.
   **TESTÉ → GATE REJET** (OOS −3,1%, 2/5 folds, t-stat −0,41 ; in-sample +8,8%). **MAIS le plus encourageant** :
   **tous les folds choisissent sign=−1 (FADE)** → direction théorique STABLE (le momentum, lui, flip-flope).
   Mécanisme d'échec = frais/rotation (folds positifs = faible rotation lookback 48 ; négatifs = lookback 24).
   Variante faible-rotation incompatible avec le walk-forward (trop peu de trades/fold). **Piste future légitime :
   fade seulement le funding EXTRÊME (seuil, pas le rang) → moins de trades, plus de conviction, moins de frais.**

   **VARIANTE SEUIL + CARRY testée 2026-06-18** (`backtest/funding_strategy.py`, `run_funding_fade_thr.py`) —
   corrige un défaut majeur : le 1er test ne comptait QUE le PnL prix, pas le funding REÇU pendant la détention
   (le vrai edge). Version par symbole, entrée si |funding annualisé| > seuil, sortie sur normalisation
   (hystérésis) ou max_hold, **carry crédité**. Run propre 8 symboles / 200j / 96 trades : **GATE REJET** —
   OOS −6,8%, 3/5 folds, t-stat −0,25, **in-sample +31,5% = mirage (écart overfit 38 pts)**. Diagnostic
   définitif : le **carry est réel mais insuffisant face au RISQUE DE QUEUE** — fader un funding extrême sans
   stop prix = short un actif qui peut continuer à monter (folds 2,3 : −19%, −7% noient le carry des bons folds).
   Ajouter un stop prix couperait les holds de collecte du carry → tuerait probablement l'edge.

   **VERSION FINALE — harvest MARKET-NEUTRAL + carry** (`run_funding_harvest.py`, cs_backtest carry_matrix) :
   combine market-neutral (long bottom-k / short top-k funding → pas de tail risk) ET carry crédité. Run 8 sym/
   200j : **OOS +0,14% = BREAK-EVEN** (vs −3 à −7% des versions partielles), t-stat 0,01, sign=−1 (fade) choisi
   4/5 folds. GATE REJET (in-sample +16,9% = overfit) mais **le carry annule PILE les frais taker**. Conclusion :
   l'edge funding est RÉEL mais ténu, **mangé exactement par les frais taker (0,045%/côté)** → la vraie piste
   n'est plus le signal mais l'EXÉCUTION (maker/passif = ~0 frais ou rebate au lieu de 0,045%) : ça basculerait
   le break-even en positif. Infra réutilisable. Anti-429 : retry/backoff dans `get_funding_history`.

   **TEST DE L'EXÉCUTION MAKER — hypothèse RÉFUTÉE** (`run_funding_harvest_maker.py`, courbe de sensibilité aux
   frais 0,045%→rebate) : **à TOUS les niveaux de coût, y compris frais nuls et rebate, le gate REJETTE sur le
   t-stat (~1,0–1,2 < 1,5).** Baisser les frais monte le chiffre de tête (+11 à +13% OOS) mais ne corrige pas la
   RÉGULARITÉ : l'edge du carry est **concentré sur 1 fold** (fold 0 ≈ +10%, les 4 autres ~0), pas régulier.
   Pire : deux runs quasi-identiques donnent **+0,14% puis +11,74%** (instabilité de sélection selon la
   complétude du fetch funding sous rate-limit) → OOS high-variance, **non déployable quel que soit le chiffre.**
   L'exécution maker ne sauve PAS le harvest. Cause profonde : 200j / 5 folds / ~43 trades = test SOUS-PUISSANT
   pour un edge aussi ténu. Verdict rigoureux possible seulement avec PLUS d'historique (4h → ~830j, plus de
   folds/trades) — sinon, abandon. Le gate (t-stat) a correctement empêché de se faire avoir par le +11,7%.

   **TEST PUISSANT — VERDICT DÉFINITIF : harvest MORT** (`--interval 4h --days 830 --folds 8`, carry sommé par
   barre via le fix `build_funding_matrix`, 105 trades). **OOS −8,18%, t-stat −0,45, 3/8 folds, in-sample +5,6%**,
   le signe flip-flope (+1/−1 d'un fold à l'autre). Avec une vraie puissance statistique (831j, 8 folds), l'edge
   funding **disparaît** : le +11,7% des 200j était du bruit high-variance, exactement le soupçon. **Funding
   harvest définitivement abandonné.** Le levier exécution n'avait rien à sauver.
2. **CVD / order-flow divergence** ⭐ — prix fait un plus-haut mais le flux agressif net (CVD) diverge →
   épuisement d'agresseurs → reversal. Microstructure pure. 2-3 params, beaucoup de trades (1m/5m). Données
   présentes (`trades` A/B + spread). Effort moyen-élevé (agréger trades→barres CVD).
   **TESTÉ 2026-06-18** (`backtest/orderflow.py` load_cvd_bars + `_signals_cvd_divergence`/`_signals_cvd_breakout`,
   `run_cvd.py`) : **REJET FRANC, les DEUX sens**. Divergence (reversal) BTC/ETH/SOL 1m : OOS −3 à −9%,
   **in-sample AUSSI négatif** (−8 à −18%), t-stat jusqu'à −3. Breakout (continuation, l'inverse) : pareil,
   négatif partout. ⇒ ni reversal ni continuation : le CVD n'a **pas de pouvoir prédictif** ici. Confirme la
   loi générale : signal d'entrée + barrière TP/SL sur marché ~efficient = perd contre les frais, quel que soit
   le signal. (12 j = 1 régime, mais résultat trop net pour que ce soit la cause.)
3. **Liquidation cascade fade** — |return| > k×ATR + chute brutale d'OI + spike updates → sur-réaction de
   liquidations → fade. Données présentes (`l2_1s.oi`). Risque : trades rares → peut-être < 30 OOS sur 12 j.
   **TESTÉ 2026-06-20** (`backtest/liq_cascade.py` + `run_liq_cascade.py`, walk-forward POOLÉ sur les 8 coins
   pour la puissance — barres 60s, 13 101 barres alignées ≈ 13,6 j, OI depuis `l2_1s`). **REJET FRANC, les DEUX
   sens** : FADE (reversal) OOS −3,97 %, 2/5 folds, t=−0,91, 91 trades — **et in-sample LUI-MÊME négatif (−2,16 %)**.
   CONTINUATION (l'inverse, momentum de cascade) pire : OOS −5,83 %, in-sample −6,32 %, t=−1,83. ⇒ un mouvement
   violent + chute d'OI n'a **aucun pouvoir prédictif** dans un sens ni l'autre (comme le CVD #2). L'in-sample
   négatif prouve que ce n'est pas un overfit OOS mais une absence d'edge. (13,6 j = 1 régime, mais résultat
   trop net.) **Abandonné.**
4. **Book imbalance persistance (baseline XGB L2)** — `imb10` soutenu → pression directionnelle. Énormément de
   trades MAIS edge ténu (AUC ~0,513 ≈ break-even après frais) → susceptible d'échouer au net-de-frais. Effort
   élevé (harnais 1s + frais/slippage réalistes).
5. **Cross-sectional momentum / reversal (8 coins)** ⭐ — momentum relatif OU reversal court terme, market-
   neutral. **Backtestable IMMÉDIATEMENT sur OHLCV multi-symbole, longue durée, sans DB ni funding.** 2 params.
   `autocorr_lag1`/`hurst_rs` disent quel signe domine. Effort moyen (boucle multi-symbole dans le backtester).
   **TESTÉ 2026-06-18** (`backtest/cross_sectional.py` + `run_cross_sectional.py`) :
   - **1h** → REJET franc : OOS −10,2%, 0/5 folds positifs, t-stat −3,76, in-sample lui-même négatif. 1h trop court.
   - **4h, lookbacks 1–3 semaines** → borderline : OOS +43%, PF médian 2,30, **pas d'overfit** (OOS > in-sample)
     MAIS **gate REJET** (t-stat 1,24 < 1,5 ; 3/5 folds ; +43% tiré par 1 seul fold ; 7 trades/fold ; le SIGNE
     bascule momentum↔reversal fold à fold = pas d'effet stable). Le gate tient la ligne sur un cas tentant.
   - Conclusion : pas d'edge robuste validé. Raffinement possible (signe FIXÉ par théorie au lieu de balayé,
     horizon plus long, plus de symboles) mais ne pas fishing le gate. **Non déployable en l'état.**
6. **Funding-momentum confirmation** — le funding comme filtre de confirmation du momentum existant (continuation
   si Δfunding va dans le sens du trend). Faible effort une fois funding câblé.
7. **Spread/vol regime gating** — méta-filtre conservateur (ne scalper qu'en spread serré + vol modérée, couper
   en HIGH_VOL). Pas un signal d'entrée seul ; à combiner avec 1/2/5.

8. **Scalper adaptatif « Le Danseur » (MA×RSI ré-optimisé en continu)** ⭐ idée francois — croisement de
   moyennes confirmé par RSI, dont un LLM ré-optimiserait les paramètres toutes les 15 min pour épouser les
   « rythmes successifs » du marché. **TESTÉ 2026-06-18** (`backtest/adaptive_scalper.py` walk-forward
   GLISSANT + `run_adaptive_scalper.py` + `tests/test_adaptive_scalper.py`) — le LLM chorégraphe n'a PAS
   été branché : on a d'abord validé l'edge BRUT de l'adaptation (sélection θ\* sur train passé, jugement
   sur test futur, roulé tous les ~8h, vs θ FIXE consensus sur les mêmes fenêtres). **DÉMENTI PROPRE,
   3 symboles / 3 :**
   - BTC : adaptatif **−1,17 %** OOS vs fixe **+0,64 %** (−1,81 pt) ; ETH : **−1,04 %** vs **+2,53 %** (−3,57 pt) ;
     SOL : **−4,94 %** vs **+3,15 %** (−8,09 pt). Gate REJET partout (OOS ≤ 0, t-stat négatif).
   - Relation **monotone** : plus le système change de θ (15-27 switches/40 pas), plus il perd. La limite
     « ne jamais re-optimiser » (= figé) est la MEILLEURE. ⇒ ré-optimiser en continu = **chasser le bruit**,
     confirme la loi générale du sprint.
   - **Limite dure découverte** : HL ne sert PAS de 5m au-delà de ~17 j (pagination calée à ~5000 bougies).
     Or la thèse des rythmes a besoin de PLUSIEURS régimes → 17 j ≈ 1 régime = test sous-puissant *pour la
     thèse*. Le verdict honnête sur la danse exigerait du 1h (historique long, multi-régime), mais 1h ≠ scalping.
   - **LEAD inattendu** : la version FIGÉE est POSITIVE sur ETH/SOL (+2,5/+3,2 % net 17j). La valeur n'est
     peut-être pas dans la danse mais dans un **scalper MA×RSI simple et FIXE**. À confirmer par un walk-forward
     à paramètres fixes PROPRE (le θ consensus a un léger biais rétrospectif). C'est le seul fil à tirer.
   - **CONFIRMÉ 2026-06-20 — LE LEAD ÉTAIT UN MIRAGE.** `backtest/run_ma_rsi_fixed.py` passe le MÊME signal
     MA×RSI (`Backtester._signals_ma_rsi`, dispatch `ma_rsi`) dans le harnais STANDARD `WalkForwardEvaluator`
     (params choisis sur le TRAIN de chaque fold, jugés OOS, gate calibré — zéro biais rétrospectif). 5m, 17,4 j,
     5 folds, 144 combos. **REJET sur les 3 symboles** : BTC OOS −0,91 % (t=−0,30, 4/5 folds mais 1 fold
     −2,55 % coule le tout), **ETH −6,41 %** (t=−2,02, 1/5), SOL −0,49 % (t=−0,58, 3/5). Le +2,5/+3,2 % venait
     du θ « consensus » de l'adaptatif (choix du θ le plus fréquent sur TOUT l'historique = look-ahead). Une fois
     le biais retiré, l'edge disparaît. **Le scalper MA×RSI fixe n'a pas d'edge OOS net de frais. Fil clos.**

9. **Stat-arb par cointégration (pairs trading)** ⭐⭐ — VALEUR RELATIVE, market-neutral. Mécanisme :
   deux actifs à facteur commun ont un spread log(pa)−β·log(pb) qui revient à l'équilibre ; celui qui paie
   est l'agent pressé qui creuse l'écart. Pas le tail risk directionnel (≠ funding fade). ≠ cross-sectional
   momentum (#5). `backtest/pairs_statarb.py` (Engle-Granger maison : β OLS + ADF t-stat 0-lag, z CAUSAL,
   filtre demi-vie) + `run_pairs_statarb.py` + `tests/test_pairs_statarb.py` (7 tests, gate rejette les marches
   aléatoires). Sélection paire+β+seuils SUR LE TRAIN par fold, jugement OOS, **gate RELEVÉ** (t-stat ≥ 2,0,
   PF ≥ 1,10 ; multi-testing C(n,2)). **TESTÉ 2026-06-18 (1h, 200j, 9 coins) — LE MEILLEUR RÉSULTAT DU SPRINT :**
   - **OOS +4,10 %** (positif net de frais — premier du sprint), **5/6 folds positifs**, **PF médian 9,13**,
     33 trades. **PASSE 4 critères du gate sur 5.** Rejet UNIQUEMENT sur t-stat 0,52 < 2,0.
   - Cause unique du rejet : **fold 5 = −5,54 %** (AAVE/LINK, 3 trades, 0 % gagnants) = une paire cointégrée
     au train a DÉCOHÉRÉ au test (rupture de cointégration, risque classique du stat-arb). Les 5 autres folds
     solidement positifs. In-sample +23,3 % (overfit 19 pts) mais OOS reste POSITIF (≠ tous les autres).
   - Paires cointégrées récurrentes (historique complet, indicatif) : SOL/XRP (ADF −5,1), DOGE/SUI, DOGE/LINK,
     SUI/XRP, SOL/SUI, LINK/SUI… LINK très présent.
   - **REFINEMENT PRINCIPIÉ À TESTER (cible la variance = le seul critère raté, PAS du fishing)** : (a) trader
     un **BOOK de N paires** équipondéré au lieu de LA meilleure par fold → diversifie le risque idiosyncratique
     de décohérence → lisse l'OOS → monte le t-stat (construction stat-arb standard) ; (b) **stop de divergence**
     (|z| > stop_z → coupe) pour borner la queue type fold 5. Chacun justifié AVANT le test par un argument
     mécanique/portefeuille, pas par l'envie de passer le gate. Si ça ne tient toujours pas → on n'insiste pas.
   - **RÉSULTAT DU BOOK (testé 2026-06-18) — RENVERSE le single-pair : REJET FRANC.** Book de 5 paires
     (`PairsWalkForward.evaluate_book`, `--book 5`), 114 trades : **OOS −1,37 %, 2/6 folds, PF médian 0,85,
     t-stat −0,35**. La diversification n'a PAS sauvé la stratégie — elle a RÉVÉLÉ que le +4,10 % single-pair
     était un mirage : le single-best sélectionnait LA paire la plus RENTABLE sur le train (skill de sélection
     = overfit), folds ultra-fins (1-2 trades, PF 999/808). Le book sélectionne les paires les plus COINTÉGRÉES
     (le mécanisme, sans regarder le PnL) et les trade sur 114 trades = test plus honnête → **négatif net de
     frais**, in-sample +5,1 % (overfit 6,5 pts). **VERDICT : mécanisme réel mais edge mangé par les frais
     (comme funding). Le call le plus proche du sprint, mais le test rigoureux le tue. ABANDONNÉ — ne pas
     fisher book=3/8/seuil ADF pour récupérer le +4 % (gate-fishing). Le book est la réponse de principe.**

10. **Time-series momentum / trend following (timeframe LONG)** ⭐⭐⭐ — **LE PREMIER EDGE QUI GAGNE
    (2026-06-20).** Insight : tous les tests #1-9 portaient sur du SCALPING court terme (1m→1h) où le frais
    round-trip (~0,09 %) écrase un edge brut minuscule (confirmé par les fills LIVE : brut ~à plat, frais =
    121 % du brut absolu). **Changer d'HORIZON casse la malédiction** : sur 1d un trade capture des mouvements
    multi-jours → le frais devient négligeable. TSMOM (Moskowitz/Ooi/Pedersen) = l'anomalie futures la plus
    documentée, jamais testée au sprint. HL sert ~2100 bougies 1d (≈5,8 ans, MULTI-RÉGIME) = la puissance qui
    manquait. `backtest/backtester.py::_signals_tsmom` (état persistant = signe du rendement trailing sur
    `lookback`, `band` = zone morte) + `_signals_donchian`, sortie `reverse` (stop-and-reverse). Runner
    `run_tsmom.py`, tests `tests/test_tsmom.py`.
    - **Walk-forward par symbole (1d, 6 folds, gate standard)** : **9/12 coins OOS net POSITIF**, moyenne
      +160 %, **3 PASS le gate strict** (AAVE t=3,16 ; SUI t=2,02 ; ETH t=1,89).
    - **Param FIXE unique (lookback 30, partout, toute la série = zéro overfit)** : le côté **SHORT positif sur
      les 12 coins** (+2557 % total) → **PAS du beta long déguisé** ; bat le buy&hold sur tous les coins
      non-mooners (LINK +208 % vs −50 % hold, etc.).
    - **Walk-forward POOLÉ honnête** (lookback choisi/fold, `run_tsmom_pooled.py`) : **1d t=+2,48, bootstrap
      P(moy>0)=99,8%, +2,47 %/trade = 27× le frais → EDGE SIGNIFICATIF** ; 4h t=+1,67 (positif, sous seuil).
      4h confirme aussi en breadth : **12/12 coins positifs** (2 timeframes, 2 fenêtres). Win ~35 % = signature
      saine du TF. ⇒ **réponse définitive du programme : OUI un signal bat les frais, à l'HORIZON 1d.**
    - **DURCISSEMENT vol-targeting (`run_tsmom_portfolio.py`, francois « durcir avant déploiement »)** : au
      niveau PORTEFEUILLE vol-targeted (20 %/an, equal-risk), **Sharpe ~0,85 robuste (0,81–0,88 sur lookback
      20/50/80), maxDD ~18 %** vs **buy&hold Sharpe 1,61, maxDD 56 %**. Le Sharpe 1,47 à lookback=30 = point
      chanceux non robuste. **TSMOM n'améliore PAS le Sharpe vs hold** sur cet échantillon (bull séculaire = le
      plus hostile au TF) ; son apport STABLE = **drawdown 4-5× plus petit**.
    - **VERDICT** : PAS un alpha qui écrase le marché. C'EST une exposition à espérance positive nette de frais
      RÉELLE (1ʳᵉ du programme) + contrôle de drawdown excellent. Valeur de déploiement crédible = sleeve
      drawdown-maîtrisé OU **overlay de régime/filtre de risque**, pas un remplaçant standalone du buy&hold en
      bull. Caveats : biais de survie ; TF 1d ≠ bot scalping 30s (nouveau module, décision archi/francois).
      RIEN en enabled=true, 0 impact live. **CONCLUSION DU PROGRAMME : le levier était l'HORIZON, pas le signal.**

## Sortie ≠ coupable (testé 2026-06-18, hypothèse francois « la sortie TP n'est peut-être pas la bonne »)

Tous les signaux du sprint avaient été jugés avec UNE sortie (barrière TP/SL). Ajout de 3 sorties au
backtester (`_simulate exit_mode` : reverse / time / trail, tp_sl numériquement inchangé) +
`backtest/run_exit_variation.py` + `tests/test_exit_modes.py`. Chaque mode jugé SÉPARÉMENT au gate
(pas de sélection du meilleur = pas de multi-testing caché). Résultat sur AO et EMA-cross (BTC/ETH, 1h, 200j) :
**aucune sortie ne passe le gate, et le TP/SL était presque toujours la MOINS MAUVAISE.** reverse/time =
pareil ou pire ; trail = destructeur (OOS −23 % sur AO, t-stat jusqu'à −4 : sorti par le bruit). **VERDICT :
le problème n'est pas la sortie, c'est l'absence de pouvoir prédictif de l'ENTRÉE. La barrière TP/SL n'était
pas le coupable.** L'instinct était bon à tester, le test le réfute.

Écartées : news/whales (pas d'archive → non backtestable).

## Top-3 à tester en premier (recommandation Opus)

1. **Funding fade cross-sectionnel** — seule prime de risque structurelle indiscutable ; prérequis = câbler
   `fundingHistory` (débloque aussi #6).
2. **Cross-sectional momentum/reversal** — backtestable tout de suite sur OHLCV, longue durée, le moins exposé au
   biais « 12 j = 1 régime » ; idéal pour valider le harnais multi-symbole.
3. **CVD divergence** — l'edge microstructure le plus crédible ; volume de trades large, mais 12 j / 1 régime,
   frais à modéliser sérieusement.
